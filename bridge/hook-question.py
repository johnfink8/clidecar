#!/usr/bin/env python3
"""PreToolUse hook for AskUserQuestion.

AskUserQuestion is a TUI-only tool: in a headless, channel-driven session it would block the turn
waiting for a TTY answer that never comes, and its question/options never reach the channel. This
hook renders the question(s) + options to the active channel, then BLOCKS for the user's reply via
the gateway Exchange and feeds it back as the tool's denial reason — so the model gets the answer in
the SAME turn. If the wait lapses or the gateway is unreachable, it degrades to the two-turn path
(deny, await the user's next message). Fail-loud: if the channel post can't land, the deny reason
tells the model to ask in plain prose itself, so the question always reaches the user either way.
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h
import exchange as ex
import transcript as t

# Seconds to block for the reply. MUST stay under the hook's settings.json `timeout` (600) so the
# Exchange lapses and we deny gracefully before Claude Code kills the hook mid-wait.
ANSWER_TIMEOUT = 570

POSTED_REASON = (
    "Your question and its options were posted to the user's channel by the bridge — they see the "
    "options and will reply in a normal message. Do NOT repeat the question or call AskUserQuestion "
    "again; end your turn and treat the user's next message as the answer."
)
PROSE_REASON = (
    "AskUserQuestion can't be shown on the user's channel. Ask your question(s) and options as plain "
    "text in your reply instead, then end your turn and treat the user's next message as the answer."
)


def answered_reason(reply: str) -> str:
    return (
        f"The user answered on their channel: {reply}\n"
        "Use this as the answer to your question(s). Do NOT call AskUserQuestion again or repeat the "
        "question; continue your turn using this answer."
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
    sid = event.get("session_id")
    sid = sid if isinstance(sid, str) else None
    tool_input = t.as_obj(event.get("tool_input"))
    questions = [o for o in (t.as_obj(q) for q in t.as_list(tool_input.get("questions"))) if o]

    turn = h.load_turn(sid)
    chat_id = turn.chat_id if turn else None
    # The newest message id BEFORE posting, so the Exchange only claims a genuinely new reply.
    since_id = h.channel_latest() if chat_id else None

    posted = sum(1 for q in questions if h.channel_send(format_question(q)))
    ok = bool(questions) and posted == len(questions)

    reply = None
    if ok and chat_id and since_id is not None:
        ans = ex.ask(chat_id, None, since_id=since_id, timeout=ANSWER_TIMEOUT, label="question")
        reply = ans.content if ans else None

    if reply is not None:
        reason, outcome = answered_reason(reply), "answered"
    elif ok:
        reason, outcome = POSTED_REASON, "posted"
    else:
        reason, outcome = PROSE_REASON, "prose_fallback"
    h.log_event("PreToolUse", {"tool": "AskUserQuestion", "questions": len(questions),
                               "posted": posted, "outcome": outcome})

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()
