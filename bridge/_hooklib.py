"""Shared helpers for the clidecar Discord hooks (ack / progress / final).

Per-turn state is one JSON file keyed by session id so the three hooks — which run
as separate processes — can find the same ack message. Discord I/O is delegated to
bin/discord-msg.sh (single source of the bot-API call).
"""
import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import sys

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


def summarize_tool(tool_name, tool_input):
    ti = tool_input if isinstance(tool_input, dict) else {}
    base = lambda p: os.path.basename(p) if isinstance(p, str) else ""
    first = lambda s: (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    if tool_name == "Bash":
        return f"⚙️ {first(ti.get('command'))}"
    if tool_name == "Read":
        return f"📖 {base(ti.get('file_path'))}"
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return f"✏️ {base(ti.get('file_path'))}"
    if tool_name in ("Grep", "Glob"):
        return f"🔎 {ti.get('pattern') or ti.get('query') or ''}"
    if tool_name in ("Task", "Agent"):
        return f"🤖 {ti.get('description') or 'subagent'}"
    return f"🔧 {tool_name}"


def turn_lines(turn):
    """Narrations are kept WHOLE — they carry intent and reasoning, so they're never
    truncated; only tool summaries are length-capped (at render). Discord replies are
    skipped — they're their own visible messages, not work worth a line."""
    items = []
    for o in turn:
        for b in t.assistant_blocks(o) or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                txt = b.get("text", "").strip()
                if txt:
                    items.append(("text", txt))
            elif b.get("type") == "tool_use" and not t.is_discord_reply(b.get("name")):
                items.append(("tool", summarize_tool(b.get("name", ""), b.get("input"))))
    return items


def work_lines(turn):
    """turn_lines minus the closing answer — the trailing narration that follows the
    last tool. The live status block never shows it (it's flushed only after the final
    tool's PostToolUse) and the Stop hook posts it as its own message, so including it
    when freezing the block would duplicate the closing. A no-tool turn trims to empty,
    which is correct: such turns open no status block at all."""
    items = turn_lines(turn)
    while items and items[-1][0] == "text":
        items.pop()
    return items


def track_live(state, event):
    """MessageDisplay fires as each text segment is displayed, BEFORE the assistant
    message is committed to the transcript — so accumulating its deltas (keyed by index,
    in state["live"]) is the only way to surface a narration before the next tool
    re-renders. Keyed by the display message_id; a new message resets the buffer."""
    md_id = event.get("message_id")
    live = state.get("live") or {}
    if live.get("message_id") != md_id:
        live = {"message_id": md_id, "segments": {}}
    delta = event.get("delta")
    if isinstance(delta, str):
        live["segments"][str(event.get("index", 0))] = delta
    state["live"] = live
    return live_text(live)


def live_text(live):
    """The join of the segments reproduces the committed text exactly, so callers can drop
    the live narration by identity once it lands in the transcript. "" if no buffer."""
    if not live:
        return ""
    segs = live.get("segments") or {}
    return "".join(segs[k] for k in sorted(segs, key=int)).strip()


def render(items, footer=None):
    """Newest-first fit of items into one Discord message: narrations whole, tool
    summaries one-line-capped. Oldest WHOLE items roll off only if the block would
    exceed Discord's size limit — a narration is never cut mid-text (a lone narration
    over the cap is the only exception, hard-sliced as a last resort)."""
    lines = [text if kind == "text" else f"`{text[:TOOL_CAP]}`" for kind, text in items]
    if footer:
        lines.append(footer)
    kept, total = [], 0
    for line in reversed(lines):
        if kept and total + len(line) + 1 > BODY_CAP:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    return "\n".join(kept)[:1990]


def turn_from_path(path):
    """The transcript's current-turn rows; [] if unreadable — progress is best-effort
    and must never crash a turn."""
    try:
        return t.current_turn(t.load_rows(path)) if path else []
    except (OSError, ValueError):
        return []


def lines_from_path(path):
    """All progress lines for the transcript's current turn."""
    return turn_lines(turn_from_path(path))


def read_event(event_name=None):
    """Parse the hook's stdin JSON; {} if unparseable so a hook never crashes a turn.
    When event_name is set, raw stdin is logged under the payload's real
    hook_event_name — so a script shared across events, like the progress renderer on
    both PostToolUse and MessageDisplay, logs each under its true name rather than a
    hard-coded label."""
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        parsed = {}
    if event_name is not None:
        log_event(parsed.get("hook_event_name") or event_name, {"stdin": raw})
    return parsed


def log_event(event_name, fields):
    """Append a diagnostic record to hooklog/events.jsonl (best-effort, never fatal)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record = {"ts": ts, "event": event_name, **fields}
        with open(os.path.join(LOG_DIR, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def persist_undelivered(session_id, text):
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


def turn_state_path(session_id):
    sid = session_id or "default"
    return os.path.join(STATE_DIR, f"turn-{sid}.json")


def load_turn(session_id):
    try:
        with open(turn_state_path(session_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_turn(session_id, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(turn_state_path(session_id), "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def clear_turn(session_id):
    try:
        os.remove(turn_state_path(session_id))
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def turn_lock(session_id):
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


def _transport(*args):
    """Run the active channel's transport script; (returncode, stdout) or (1, "") if no
    channel is configured. The single shell-out point for all channel I/O."""
    script = channel.transport()
    if not script:
        return 1, ""
    out = subprocess.run([script, *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
    return out.returncode, out.stdout


def channel_send(text, reply_to=None):
    args = ["send", text] + ([reply_to] if reply_to else [])
    code, stdout = _transport(*args)
    return stdout.strip() or None if code == 0 else None


def channel_edit(message_id, text):
    return _transport("edit", message_id, text)[0] == 0


def channel_react(message_id, emoji, add=True):
    return _transport("react" if add else "unreact", message_id, emoji)[0] == 0


def channel_latest():
    """The channel's most recent message id, or None. Lets the progress hook tell when
    a new message (John's) has landed below its status message so it can re-home a fresh
    one rather than keep editing a message that has scrolled out of view."""
    code, stdout = _transport("latest")
    return stdout.strip() or None if code == 0 else None


def can(capability):
    """Whether the active channel declares a capability (edit/react/latest), so the core
    can degrade — e.g. skip the 👀 react or the re-home check on a channel that lacks them."""
    return bool(channel.capabilities().get(capability))
