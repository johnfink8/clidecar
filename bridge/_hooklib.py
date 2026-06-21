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
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Generator

BIN = os.path.dirname(os.path.abspath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import channel
import exchange as ex
import transcript as t

STATE_DIR = os.path.expanduser("~/.clidecar/state")
LOG_DIR = os.path.join(STATE_DIR, "hooklog")
UNDELIVERED_DIR = os.path.join(STATE_DIR, "undelivered")

SEEN = "👀"
DONE = "✅"
WORKING = "⏳"
BODY_CAP = 1900  # headroom under Discord's 2000-char message limit
TOOL_CAP = 120   # tool summaries stay one tidy line; narrations are never capped

WORKING_FOOTER = f"{WORKING} *working…*"
DONE_FOOTER = f"{DONE} *done*"
DONE_PING = DONE_FOOTER  # the tiny end-of-turn message whose only job is a completion push; mirrors the cap
# A sealed (spilled) message's footer. Kept to ONE char so sealing can never have to drop a
# line the message already showed: a frozen prefix carrying this footer fits at least as many
# lines as the live block did with the longer working footer. The fresh message below it is
# the real "continued" cue.
SPILL_FOOTER = "⏬"

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
    chat_id: str | None = None
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
            chat_id=_str_field(d, "chat_id"),
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


def split_units(items: list[Item]) -> list[Item]:
    """Flatten block items to line-granular UNITS: each narration line its own ("text", line)
    unit, each tool summary one unit. Spilling seals whole units, so a long block breaks across
    messages on line boundaries (never mid-line) — and the unit sequence is identical whether a
    narration is mid-stream (live) or committed, so a unit index stays valid across that
    transition (the basis for append-only streaming)."""
    units: list[Item] = []
    for kind, text in items:
        if kind == "text":
            units.extend(("text", ln) for ln in text.split("\n"))
        else:
            units.append((kind, text))
    return units


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


def _line(item: Item) -> str:
    kind, text = item
    return text if kind == "text" else f"`{text[:TOOL_CAP]}`"


def fence_state(items: list[Item]) -> str | None:
    """The open ``` code-fence language after these items, or None if balanced/closed. A spill
    boundary inside a fence needs this to reopen the block on the continuation message."""
    inside = False
    lang = ""
    for kind, text in items:
        if kind != "text":
            continue
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                inside = not inside
                lang = line.lstrip()[3:].strip() if inside else ""
    return lang if inside else None


def _balance_fences(lines: list[str], open_lang: str | None) -> list[str]:
    """Make a message's content lines an independently-valid Discord block: reopen a ``` fence the
    previous (sealed) message left open, and close one this message leaves open — so a code block
    spanning a spill boundary renders correctly in BOTH messages instead of breaking."""
    out = ([f"```{open_lang}"] if open_lang is not None else []) + lines
    if sum(1 for ln in out if ln.lstrip().startswith("```")) % 2:
        out.append("```")
    return out


def render(items: list[Item], footer: str | None = None, open_lang: str | None = None) -> str:
    """Newest-first fit of items into one Discord message: narrations whole, tool
    summaries one-line-capped. Oldest WHOLE items roll off only if the block would
    exceed Discord's size limit — a narration is never cut mid-text (a lone narration
    over the cap is the only exception, hard-sliced as a last resort). open_lang reopens a
    code fence the previous message left open; the footer stays outside the fence."""
    budget = BODY_CAP - ((len(footer) + 1) if footer else 0)
    content = [_line(it) for it in items]
    kept: list[str] = []
    total = 0
    for line in reversed(content):
        if kept and total + len(line) + 1 > budget:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    out = _balance_fences(kept, open_lang)
    if footer:
        out.append(footer)
    return "\n".join(out)[:1990]


def fits(items: list[Item], footer: str | None = None) -> bool:
    """Whether render() would drop nothing — every item + footer fits one message."""
    total = (len(footer) + 1) if footer else 0
    return total + sum(len(_line(it)) + 1 for it in items) <= BODY_CAP


def head_fit(items: list[Item], footer: str | None = None) -> int:
    """How many LEADING items fit one message (oldest-first), at least 1. Used to seal the
    fullest possible prefix into a frozen spill message; a lone over-cap item returns 1 and
    is hard-sliced by render_head as the documented last resort."""
    total = (len(footer) + 1) if footer else 0
    n = 0
    for it in items:
        width = len(_line(it)) + 1
        if n and total + width > BODY_CAP:
            break
        total += width
        n += 1
    return max(n, 1)


def render_head(items: list[Item], footer: str | None = None, open_lang: str | None = None) -> str:
    """Oldest-first render of a known-fitting prefix — the frozen body of a spilled message
    (render() fits newest-first, which is wrong for a sealed head). open_lang reopens a code fence
    the previous message left open; a fence this message leaves open is closed at its end."""
    out = _balance_fences([_line(it) for it in items], open_lang)
    if footer:
        out.append(footer)
    return "\n".join(out)[:1990]


def make_persist(session_id: str | None, state: TurnState) -> Callable[[int, str | None], None]:
    """spill()'s checkpoint: record base+message_id after each seal so a later failure can't
    re-seal (duplicate) or strand. Lives next to spill() so both hooks share the one contract."""
    def persist(base: int, mid: str | None) -> None:
        state.base, state.message_id = base, mid
        save_turn(session_id, state)
    return persist


def spill(
    units: list[Item],
    base: int,
    mid: str | None,
    live_units: list[Item],
    footer: str,
    persist: Callable[[int, str | None], None],
) -> tuple[int, str | None]:
    """Append-only seal: while the shown tail (units[base:] + live_units) overflows one message,
    freeze the fullest sealable prefix into a frozen (⏬) message and advance base — never drop a
    line already shown. The LAST live unit is held unsealable: it's the still-streaming line, so a
    seal always lands on a completed line boundary. The first seal edits the live message into its
    frozen form (mid→None); later seals open fresh frozen messages. On a send/edit failure it stops
    WITHOUT advancing base past unposted lines (those stay in the live tail, retried/guaranteed by
    the caller). persist(base, mid) records each successful seal immediately, so a later failure
    can't re-seal (duplicate) or strand. Returns the new (base, mid); units[base:] then fits, unless
    a lone over-cap unit remains (render hard-slices it, the documented last resort)."""
    show = units + live_units
    sealable = show[:-1]  # the LAST shown unit is the live tail (a still-streaming line) — never seal it
    while not fits(show[base:], footer):
        head = sealable[base:]
        if not head:
            break  # only the live tail remains and it alone exceeds the cap — render hard-slices it below
        k = head_fit(head, SPILL_FOOTER)
        sealed = render_head(head[:k], SPILL_FOOTER, fence_state(show[:base]))
        if mid:
            if not channel_edit(mid, sealed):
                log_event("spill", {"outcome": "seal_edit_failed"})
                break
            mid = None
        elif not channel_send(sealed):
            log_event("spill", {"outcome": "seal_send_failed"})
            break
        base += k
        persist(base, mid)
    return base, mid


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


def channel_send(text: str, reply_to: str | None = None) -> str | None:
    """Send one message through the gateway broker and return its new id. STRICT LANES: the hook
    never touches the adapter — if the gateway is unreachable, fail LOUD (log + stderr) and return
    None so the caller persists/pings, NEVER a reach-around to the transport."""
    mid = ex.emit(ex.Outbound(text=text, kind="message", source="claude", reply_to=reply_to))
    if mid is None:
        log_event("gateway_send_failed", {"chars": len(text)})
        sys.stderr.write("clidecar bridge: gateway unreachable on send (strict lanes — no adapter fallback)\n")
    return mid


def channel_edit(message_id: str, text: str) -> bool:
    ok = ex.edit(message_id, text)
    if not ok:
        log_event("gateway_edit_failed", {"message_id": message_id})
    return ok


def channel_react(message_id: str, emoji: str, add: bool = True) -> bool:
    ok = ex.react(message_id, emoji, add)
    if not ok:
        log_event("gateway_react_failed", {"message_id": message_id, "emoji": emoji})
    return ok


def channel_latest() -> str | None:
    """The channel's most recent message id, or None. Lets the progress hook tell when a new
    message (John's) has landed below its status message so it can re-home a fresh one rather than
    keep editing a message that has scrolled out of view."""
    return ex.latest()


def can(capability: str) -> bool:
    """Whether the active channel declares a capability (edit/react/latest), so the core
    can degrade — e.g. skip the 👀 react or the re-home check on a channel that lacks them."""
    return bool(channel.capabilities().get(capability))
