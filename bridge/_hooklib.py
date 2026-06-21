"""Shared helpers for the clidecar bridge hooks (ack / progress / final).

Per-turn state is one JSON file keyed by session id so the three hooks — which run
as separate processes — can find the same status message. Channel I/O is delegated to
the active channel's transport script, resolved by channel.py (provider-agnostic).

Every JSON boundary (hook stdin, the per-turn state file) is validated, not asserted:
each dataclass's from_obj classmethod constructs it from isinstance-checked fields, so a
wrong-typed value is dropped rather than trusted.
"""
import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Generator

BIN = os.path.dirname(os.path.abspath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import channel
import transcript as t

STATE_DIR = os.path.expanduser("~/.clidecar/state")
LOG_DIR = os.path.join(STATE_DIR, "hooklog")
UNDELIVERED_DIR = os.path.join(STATE_DIR, "undelivered")

SEEN = "👀"
DONE = "✅"
WORKING = "⏳"
BODY_CAP = 1900  # headroom under Discord's 2000-char message limit
TOOL_CAP = 120   # tool summaries stay one tidy line; narrations are never capped

# A rendered status line: ("text", narration) kept whole, or ("tool", summary) one-line-capped.
Item = tuple[str, str]


def _str_field(d: dict[str, object], key: str) -> str | None:
    v = d.get(key)
    return v if isinstance(v, str) else None


def _int_field(d: dict[str, object], key: str) -> int:
    v = d.get(key)
    return v if isinstance(v, int) else 0


@dataclass(frozen=True)
class HookEvent:
    """The hook's stdin JSON — fields vary by event, hence all optional."""
    session_id: str | None = None
    transcript_path: str | None = None
    prompt: str | None = None
    hook_event_name: str | None = None
    message_id: str | None = None
    delta: str | None = None
    index: int = 0

    @classmethod
    def from_obj(cls, obj: object) -> "HookEvent":
        d = t.as_obj(obj)
        return cls(
            session_id=_str_field(d, "session_id"),
            transcript_path=_str_field(d, "transcript_path"),
            prompt=_str_field(d, "prompt"),
            hook_event_name=_str_field(d, "hook_event_name"),
            message_id=_str_field(d, "message_id"),
            delta=_str_field(d, "delta"),
            index=_int_field(d, "index"),
        )


@dataclass
class LiveState:
    message_id: str | None = None
    segments: dict[str, str] = field(default_factory=dict[str, str])

    @classmethod
    def from_obj(cls, obj: object) -> "LiveState":
        d = t.as_obj(obj)
        segs = t.as_obj(d.get("segments"))
        return cls(
            message_id=_str_field(d, "message_id"),
            segments={k: v for k, v in segs.items() if isinstance(v, str)},
        )


@dataclass
class TurnState:
    source_message_id: str | None = None
    done: str | None = None
    base: int = 0
    shown: int = 0
    message_id: str | None = None
    last_body: str | None = None
    live: LiveState | None = None

    @classmethod
    def from_obj(cls, obj: object) -> "TurnState":
        d = t.as_obj(obj)
        live = d.get("live")
        return cls(
            source_message_id=_str_field(d, "source_message_id"),
            done=_str_field(d, "done"),
            base=_int_field(d, "base"),
            shown=_int_field(d, "shown"),
            message_id=_str_field(d, "message_id"),
            last_body=_str_field(d, "last_body"),
            live=LiveState.from_obj(live) if live is not None else None,
        )


def summarize_tool(tool_name: str, tool_input: object) -> str:
    ti = t.as_obj(tool_input)

    def base(p: object) -> str:
        return os.path.basename(p) if isinstance(p, str) else ""

    def first(s: object) -> str:
        return s.strip().splitlines()[0] if isinstance(s, str) and s.strip() else ""

    if tool_name == "Bash":
        return f"⚙️ {first(ti.get('command'))}"
    if tool_name == "Read":
        return f"📖 {base(ti.get('file_path'))}"
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return f"✏️ {base(ti.get('file_path'))}"
    if tool_name in ("Grep", "Glob"):
        pattern = ti.get("pattern") or ti.get("query")
        return f"🔎 {pattern if isinstance(pattern, str) else ''}"
    if tool_name in ("Task", "Agent"):
        description = ti.get("description")
        return f"🤖 {description if isinstance(description, str) else 'subagent'}"
    return f"🔧 {tool_name}"


def turn_lines(turn: list[t.Row]) -> list[Item]:
    """Narrations are kept WHOLE — they carry intent and reasoning, so they're never
    truncated; only tool summaries are length-capped (at render). Discord replies are
    skipped — they're their own visible messages, not work worth a line."""
    items: list[Item] = []
    for o in turn:
        for b in t.assistant_blocks(o) or []:
            if b.get("type") == "text":
                txt = b.get("text")
                if isinstance(txt, str) and txt.strip():
                    items.append(("text", txt.strip()))
            elif b.get("type") == "tool_use":
                name = b.get("name")
                name = name if isinstance(name, str) else ""
                if not t.is_discord_reply(name):
                    items.append(("tool", summarize_tool(name, b.get("input"))))
    return items


def work_lines(turn: list[t.Row]) -> list[Item]:
    """turn_lines minus the closing answer — the trailing narration that follows the
    last tool. The live status block never shows it (it's flushed only after the last
    tool's PostToolUse) and the Stop hook posts it as its own message, so including it
    when freezing the block would duplicate the closing. A no-tool turn trims to empty,
    which is correct: such turns open no status block at all."""
    items = turn_lines(turn)
    while items and items[-1][0] == "text":
        items.pop()
    return items


def track_live(state: TurnState, event: HookEvent) -> str:
    """MessageDisplay fires as each text segment is displayed, BEFORE the assistant
    message is committed to the transcript — so accumulating its deltas (keyed by index,
    in state["live"]) is the only way to surface a narration before the next tool
    re-renders. Keyed by the display message_id; a new message resets the buffer."""
    md_id = event.message_id
    live = state.live or LiveState()
    if live.message_id != md_id:
        live = LiveState(message_id=md_id)
    if event.delta is not None:
        live.segments[str(event.index)] = event.delta
    state.live = live
    return live_text(live)


def live_text(live: LiveState | None) -> str:
    """The join of the segments reproduces the committed text exactly, so callers can drop
    the live narration by identity once it lands in the transcript."""
    if live is None:
        return ""
    segs = live.segments
    return "".join(segs[k] for k in sorted(segs, key=int)).strip()


def render(items: list[Item], footer: str | None = None) -> str:
    """Newest-first fit of items into one Discord message: narrations whole, tool
    summaries one-line-capped. Oldest WHOLE items roll off only if the block would
    exceed Discord's size limit — a narration is never cut mid-text (a lone narration
    over the cap is the only exception, hard-sliced as a last resort)."""
    lines = [text if kind == "text" else f"`{text[:TOOL_CAP]}`" for kind, text in items]
    if footer:
        lines.append(footer)
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if kept and total + len(line) + 1 > BODY_CAP:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    return "\n".join(kept)[:1990]


def turn_from_path(path: str | None) -> list[t.Row]:
    """The transcript's current-turn rows; [] if unreadable — progress is best-effort
    and must never crash a turn."""
    try:
        return t.current_turn(t.load_rows(path)) if path else []
    except (OSError, ValueError):
        return []


def lines_from_path(path: str | None) -> list[Item]:
    """All progress lines for the transcript's current turn."""
    return turn_lines(turn_from_path(path))


def read_event(event_name: str | None = None) -> HookEvent:
    """Parse and validate the hook's stdin JSON into a HookEvent; an empty event if it's
    unparseable, so a hook never crashes a turn. When event_name is set, raw stdin is
    logged under the payload's real hook_event_name — so a script shared across events,
    like the progress renderer on both PostToolUse and MessageDisplay, logs each under
    its true name rather than a hard-coded label."""
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        parsed = None
    event = HookEvent.from_obj(parsed)
    if event_name is not None:
        log_event(event.hook_event_name or event_name, {"stdin": raw})
    return event


def log_event(event_name: str, fields: dict[str, object]) -> None:
    """Append a diagnostic record to hooklog/events.jsonl (best-effort, never fatal)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record = {"ts": ts, "event": event_name, **fields}
        with open(os.path.join(LOG_DIR, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def persist_undelivered(session_id: str | None, text: str) -> str | None:
    """Write an answer Discord refused to disk; return its path, or None if even that
    fails. Must never throw — it runs after the send already failed, so an exception
    here would be the final silent loss. The caller has already logged the answer text
    to events.jsonl (OSError-guarded) before calling, so a None return is still traced.
    """
    try:
        os.makedirs(UNDELIVERED_DIR, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(UNDELIVERED_DIR, f"{ts}-{session_id or 'default'}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
    except OSError:
        return None


def turn_state_path(session_id: str | None) -> str:
    sid = session_id or "default"
    return os.path.join(STATE_DIR, f"turn-{sid}.json")


def load_turn(session_id: str | None) -> TurnState | None:
    """The validated per-turn state, or None if no state file exists yet."""
    try:
        with open(turn_state_path(session_id), encoding="utf-8") as fh:
            parsed = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return TurnState.from_obj(parsed)


def save_turn(session_id: str | None, state: TurnState) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(turn_state_path(session_id), "w", encoding="utf-8") as fh:
        json.dump(asdict(state), fh)


def clear_turn(session_id: str | None) -> None:
    try:
        os.remove(turn_state_path(session_id))
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def turn_lock(session_id: str | None) -> Generator[None, None, None]:
    """PostToolUse, MessageDisplay and Stop fire as separate processes that all mutate
    the one status message; without this lock they race and double-create it. Best-effort:
    if the lock can't be acquired the body still runs — conceding a duplicate status
    message and a possible post-freeze resurrection of the cosmetic status block, but never
    a lost closing answer, which the Stop hook delivers (and persists/pings on failure)
    outside this section."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fh = None
    try:
        fh = open(turn_state_path(session_id) + ".lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError:
        fh = None
    try:
        yield
    finally:
        if fh is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def _transport(*args: str) -> tuple[int, str]:
    """Run the active channel's transport script; (returncode, stdout). The single shell-out
    point for all channel I/O. Returns (1, "") if no channel resolves — but logs that loudly
    first (events.jsonl + stderr) so a misconfigured bridge is diagnosable, not a silent
    no-op indistinguishable from a transient send failure."""
    script, reason = channel.transport()
    if not script:
        log_event("channel_unresolved", {"reason": reason, "verb": args[0] if args else None})
        sys.stderr.write(f"clidecar bridge: no messaging channel resolved — {reason}\n")
        return 1, ""
    out = subprocess.run([script, *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
    return out.returncode, out.stdout


def channel_send(text: str, reply_to: str | None = None) -> str | None:
    args = ["send", text] + ([reply_to] if reply_to else [])
    code, stdout = _transport(*args)
    return stdout.strip() or None if code == 0 else None


def channel_edit(message_id: str, text: str) -> bool:
    return _transport("edit", message_id, text)[0] == 0


def channel_react(message_id: str, emoji: str, add: bool = True) -> bool:
    return _transport("react" if add else "unreact", message_id, emoji)[0] == 0


def channel_latest() -> str | None:
    """The channel's most recent message id, or None. Lets the progress hook tell when
    a new message (John's) has landed below its status message so it can re-home a fresh
    one rather than keep editing a message that has scrolled out of view."""
    code, stdout = _transport("latest")
    return stdout.strip() or None if code == 0 else None


def can(capability: str) -> bool:
    """Whether the active channel declares a capability (edit/react/latest), so the core
    can degrade — e.g. skip the 👀 react or the re-home check on a channel that lacks them."""
    return bool(channel.capabilities().get(capability))
