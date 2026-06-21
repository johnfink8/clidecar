#!/usr/bin/env python3
"""PreToolUse hook for AskUserQuestion.

AskUserQuestion is a TUI-only tool: with no interactive terminal (a headless, channel-driven
session) it blocks the whole turn waiting for an answer that never arrives, and its question/options
never reach the channel. This hook makes that deterministic instead of a hang: on EVERY
AskUserQuestion call it renders the question + options to the active channel and DENIES the tool, so
the model is redirected rather than blocked. The user answers in a normal follow-up message, which
the model receives as its next prompt. Fail-loud: if the channel post can't land, the deny reason
tells the model to ask in plain prose itself, so the question still reaches the user either way.
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h
import transcript as t

POSTED_REASON = (
    "Your question and its options were posted to the user's channel by the bridge — they see the "
    "options and will reply in a normal message. Do NOT repeat the question or call AskUserQuestion "
    "again; end your turn and treat the user's next message as the answer."
)
PROSE_REASON = (
    "AskUserQuestion can't be shown on the user's channel. Ask your question(s) and options as plain "
    "text in your reply instead, then end your turn and treat the user's next message as the answer."
)


def _str(d: dict[str, object], key: str) -> str:
    v = d.get(key)
    return v if isinstance(v, str) else ""


def format_question(q: dict[str, object]) -> str:
    title = f"❓ **{_str(q, 'question')}**"
    header = _str(q, "header")
    if header:
        title += f"  ·  _{header}_"
    lines = [title]
    for i, raw in enumerate(t.as_list(q.get("options")), 1):
        o = t.as_obj(raw)
        line = f"{i}. **{_str(o, 'label')}**"
        desc = _str(o, "description")
        if desc:
            line += f" — {desc}"
        lines.append(line)
    multi = bool(q.get("multiSelect"))
    lines.append("_Reply with the numbers or labels (pick one or more)._" if multi
                 else "_Reply with a number or the option label._")
    return "\n".join(lines)


def main() -> None:
    try:
        event = t.as_obj(json.loads(sys.stdin.read() or "{}"))
    except json.JSONDecodeError:
        event = {}
    tool_input = t.as_obj(event.get("tool_input"))
    questions = [o for o in (t.as_obj(q) for q in t.as_list(tool_input.get("questions"))) if o]

    posted = sum(1 for q in questions if h.channel_send(format_question(q)))
    ok = bool(questions) and posted == len(questions)
    h.log_event("PreToolUse", {"tool": "AskUserQuestion", "questions": len(questions), "posted": posted, "ok": ok})

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": POSTED_REASON if ok else PROSE_REASON,
    }}))


if __name__ == "__main__":
    main()
