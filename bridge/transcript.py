"""Shared parsing of the Claude Code transcript JSONL for the bridge hooks.

The transcript format is owned by Claude Code, not us — every row arrives as untyped JSON.
The honest model for that is `dict[str, object]`: `as_obj`/`as_list` narrow a parsed value
to a JSON object/array (a sound cast — JSON object keys are always strings, values
arbitrary), and each field is `isinstance`-checked at the point of use. So the typing
reflects real runtime validation, not an unchecked assertion about the shape.
"""

import json
import sys
from typing import cast

Row = dict[str, object]
Block = dict[str, object]


def as_obj(x: object) -> dict[str, object]:
    """A parsed JSON value as a string-keyed dict, or {} if it isn't a JSON object. JSON
    object keys are always strings and values arbitrary, so this narrowing is sound."""
    return cast("dict[str, object]", x) if isinstance(x, dict) else {}


def as_list(x: object) -> list[object]:
    """A parsed JSON value as a list of arbitrary elements, or [] if it isn't an array."""
    return cast("list[object]", x) if isinstance(x, list) else []


def load_rows(path: str) -> list[Row]:
    """Parse the JSONL into row objects.

    The transcript is being appended to live, so the LAST line can be a half-written
    record when we read mid-turn — that partial line is tolerated (skipped). Any
    *earlier* undecodable line, or any line that isn't a JSON object, is genuine
    corruption and raised loudly, since silently dropping it would hide a broken
    transcript.
    """
    fh = sys.stdin if path == "-" else open(path, encoding="utf-8")
    with fh:
        raw = fh.readlines()
    rows: list[Row] = []
    for i, line in enumerate(raw):
        line = line.strip()
        if not line:
            continue
        is_last = i == len(raw) - 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if is_last:
                continue
            raise
        if not isinstance(obj, dict):
            if is_last:
                continue
            raise ValueError(f"transcript row {i} is not a JSON object")
        rows.append(cast("dict[str, object]", obj))  # validated dict above
    return rows


def is_discord_reply(name: str | None) -> bool:
    """Whether a tool_use name is the Discord reply MCP tool — the one predicate both
    extract_closing (dedup against an already-sent answer) and the progress renderer
    rely on, so the two can't drift apart."""
    name = name or ""
    return name.startswith("mcp__") and "discord" in name and "reply" in name


def is_human_prompt(o: Row) -> bool:
    msg = as_obj(o.get("message"))
    if o.get("type") != "user" or msg.get("role") != "user":
        return False
    # A human prompt's content is a string or a block list with no tool_result; a list
    # carrying tool_result is a tool-call continuation, not a fresh prompt.
    return not any(as_obj(b).get("type") == "tool_result" for b in as_list(msg.get("content")))


def assistant_blocks(o: Row) -> list[Block] | None:
    msg = as_obj(o.get("message"))
    if msg.get("role") != "assistant":
        return None
    return [as_obj(b) for b in as_list(msg.get("content"))]


def has_tool_result(o: Row) -> bool:
    content = as_obj(o.get("message")).get("content")
    return any(as_obj(b).get("type") == "tool_result" for b in as_list(content))


def block_text(blocks: list[Block]) -> str:
    parts: list[str] = []
    for b in blocks:
        if b.get("type") == "text":
            txt = b.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "\n".join(parts).strip()


def current_turn(rows: list[Row]) -> list[Row]:
    """Rows from the last human prompt onward — the in-progress or just-ended turn."""
    start = 0
    for i, o in enumerate(rows):
        if is_human_prompt(o):
            start = i
    return rows[start:]


def turn_id(turn: list[Row]) -> str | None:
    """A stable identifier for the turn — the uuid of its starting human prompt. Used to
    scope the Stop hook's 'done' tombstone to the exact turn it finalized, so a stale
    tombstone (e.g. a --resume into a session whose UserPromptSubmit never re-fired) can't
    suppress a real turn's status or silently drop its closing answer. None if no id can
    be derived, in which case callers fall back to a non-tombstone (delete) path."""
    if not turn:
        return None
    first = turn[0]
    for key in ("uuid", "promptId", "timestamp"):
        v = first.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def closing_flushed(turn: list[Row]) -> bool:
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


def tool_result_errors(turn: list[Row]) -> dict[str | None, bool]:
    """Map tool_use_id -> is_error for every tool_result in the turn."""
    errors: dict[str | None, bool] = {}
    for o in turn:
        content = as_obj(o.get("message")).get("content")
        for b in as_list(content):
            blk = as_obj(b)
            if blk.get("type") == "tool_result":
                tuid = blk.get("tool_use_id")
                errors[tuid if isinstance(tuid, str) else None] = bool(blk.get("is_error"))
    return errors


def extract_closing(turn: list[Row]) -> tuple[str, bool]:
    """Return (closing_text, already_sent) for the turn.

    closing_text is the last assistant text block. already_sent is true only when that
    text was already delivered via a `reply` whose tool_result did NOT error — an
    attempted-but-failed reply (or one still in flight) does not count, so the Stop
    hook re-sends rather than dropping the answer.
    """
    errors = tool_result_errors(turn)
    delivered: list[str] = []
    closing = ""
    for o in turn:
        blocks = assistant_blocks(o)
        if not blocks:
            continue
        for b in blocks:
            if b.get("type") != "tool_use":
                continue
            name = b.get("name")
            if is_discord_reply(name if isinstance(name, str) else None):
                sent = as_obj(b.get("input")).get("text")
                bid = b.get("id")
                bid_key = bid if isinstance(bid, str) else None
                if isinstance(sent, str) and errors.get(bid_key) is False:
                    delivered.append(sent.strip())
        text = block_text(blocks)
        if text:
            closing = text
    already_sent = bool(closing) and closing in delivered
    return closing, already_sent
