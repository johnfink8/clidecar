"""Shared parsing of the Claude Code transcript JSONL for the Discord hooks."""
import json
import sys


def load_rows(path):
    """Parse the JSONL into row dicts.

    The transcript is being appended to live, so the LAST line can be a half-written
    record when we read mid-turn — that partial line is tolerated (skipped). Any
    *earlier* undecodable line is genuine corruption and raised loudly, since silently
    dropping it would hide a broken transcript.
    """
    fh = sys.stdin if path == "-" else open(path, encoding="utf-8")
    with fh:
        raw = fh.readlines()
    rows = []
    for i, line in enumerate(raw):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(raw) - 1:
                continue
            raise
    return rows


def is_discord_reply(name):
    """Whether a tool_use name is the Discord reply MCP tool — the one predicate both
    extract_closing (dedup against an already-sent answer) and the progress renderer
    rely on, so the two can't drift apart."""
    name = name or ""
    return name.startswith("mcp__") and "discord" in name and "reply" in name


def is_human_prompt(o):
    msg = o.get("message") or {}
    if o.get("type") != "user" or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return True


def assistant_blocks(o):
    msg = o.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    content = msg.get("content")
    return content if isinstance(content, list) else None


def has_tool_result(o):
    content = (o.get("message") or {}).get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def block_text(blocks):
    return "\n".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def current_turn(rows):
    """Rows from the last human prompt onward — the in-progress or just-ended turn."""
    start = 0
    for i, o in enumerate(rows):
        if is_human_prompt(o):
            start = i
    return rows[start:]


def closing_flushed(turn):
    """True once the turn's closing assistant text is durably in the transcript.

    The Stop hook can fire before the closing message is flushed to the JSONL, so the
    answer would still be missing (the previous, intermediate text would be read
    instead). The closing is flushed when the last assistant message carrying text
    sits AFTER the last tool_result row — i.e. the model emitted its answer after the
    final tool. A turn with no tools is flushed as soon as any assistant text exists.
    """
    last_text = -1
    last_result = -1
    for i, o in enumerate(turn):
        if has_tool_result(o):
            last_result = i
        blocks = assistant_blocks(o)
        if blocks and block_text(blocks):
            last_text = i
    return last_text > last_result


def tool_result_errors(turn):
    """Map tool_use_id -> is_error for every tool_result in the turn."""
    errors = {}
    for o in turn:
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                errors[b.get("tool_use_id")] = bool(b.get("is_error"))
    return errors


def extract_closing(turn):
    """Return (closing_text, already_sent) for the turn.

    closing_text is the last assistant text block. already_sent is true only when that
    text was already delivered via a `reply` whose tool_result did NOT error — an
    attempted-but-failed reply (or one still in flight) does not count, so the Stop
    hook re-sends rather than dropping the answer.
    """
    errors = tool_result_errors(turn)
    delivered = []
    closing = ""
    for o in turn:
        blocks = assistant_blocks(o)
        if not blocks:
            continue
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if is_discord_reply(b.get("name")):
                sent = (b.get("input") or {}).get("text")
                if isinstance(sent, str) and errors.get(b.get("id")) is False:
                    delivered.append(sent.strip())
        text = block_text(blocks)
        if text:
            closing = text
    already_sent = bool(closing) and closing in delivered
    return closing, already_sent
