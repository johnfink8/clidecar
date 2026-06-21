"""Shared helpers for the clidecar Discord hooks (ack / progress / final).

Per-turn state is one JSON file keyed by session id so the three hooks — which run
as separate processes — can find the same ack message. Discord I/O is delegated to
bin/discord-msg.sh (single source of the bot-API call).
"""
import datetime
import json
import os
import subprocess
import sys

BIN = os.path.dirname(os.path.abspath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import transcript as t

STATE_DIR = os.path.expanduser("~/.clidecar/state")
LOG_DIR = os.path.join(STATE_DIR, "hooklog")
UNDELIVERED_DIR = os.path.join(STATE_DIR, "undelivered")
DISCORD_MSG = os.path.join(BIN, "discord-msg.sh")

SEEN = "👀"
DONE = "✅"
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
    """Ordered (kind, text) items for the turn: ("text", full narration) and
    ("tool", summary). Narrations are kept WHOLE — they carry intent and reasoning, so
    they're never truncated; only tool summaries are length-capped (at render). Discord
    replies are skipped — they're their own visible messages, not work worth a line."""
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


def lines_from_path(path):
    """All progress lines for the transcript's current turn; [] if unreadable —
    progress is best-effort and must never crash a turn."""
    try:
        turn = t.current_turn(t.load_rows(path)) if path else []
    except (OSError, ValueError):
        return []
    return turn_lines(turn)


def read_event(event_name=None):
    """Parse the hook's stdin JSON; {} if unparseable so a hook never crashes a turn.
    When event_name is set, raw stdin is logged first (diagnostic, kept during the
    bridge proving phase)."""
    raw = sys.stdin.read()
    if event_name is not None:
        log_event(event_name, {"stdin": raw})
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


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


def discord_send(text, reply_to=None):
    args = [DISCORD_MSG, "send", text]
    if reply_to:
        args.append(reply_to)
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return None
    return out.stdout.strip() or None


def discord_edit(message_id, text):
    out = subprocess.run(
        [DISCORD_MSG, "edit", message_id, text], capture_output=True, text=True
    )
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return False
    return True


def discord_react(message_id, emoji, add=True):
    out = subprocess.run(
        [DISCORD_MSG, "react" if add else "unreact", message_id, emoji],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return False
    return True


def discord_latest():
    """The channel's most recent message id, or None. Lets the progress hook tell when
    a new message (John's) has landed below its status message so it can re-home a fresh
    one rather than keep editing a message that has scrolled out of view."""
    out = subprocess.run([DISCORD_MSG, "latest"], capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return None
    return out.stdout.strip() or None
