#!/usr/bin/env python3
"""PermissionRequest hook for ExitPlanMode — bridges the plan-approval gate to the channel.

When the managed session enters plan mode and calls ExitPlanMode, Claude Code raises a console
permission dialog (the "approve plan / keep planning" gate) that a headless, channel-driven session
can never answer — it would wedge waiting on a TTY. This hook renders the plan markdown to the active
channel, BLOCKS for the user's reply via the gateway Exchange, then decides:

  • a bare affirmative (yes / go / approve / ship it / lgtm …) → ALLOW → plan mode exits, the session
    proceeds to implement IN THE SAME TURN;
  • anything else (substantive feedback) or a lapsed wait → DENY → the session stays in plan mode to
    revise, with the user's text fed back as the dialog message.

Verified on Claude Code 2.1.186: ExitPlanMode fires BOTH PreToolUse and PermissionRequest; the
plan markdown arrives inline under tool_input.plan; the deny `message` surfaces to the model as
revise-context (so the revise path needs no fallback); and a
PermissionRequest allow (decision.behavior:"allow" + echoed updatedInput) exits plan mode cleanly.
We hook PermissionRequest, not PreToolUse, because it is the dialog that actually gates the exit —
a PreToolUse allow does not suppress it. Fail-closed: a missing reply NEVER auto-approves.

Requires a channel chat_id, which only a turn started from an inbound message carries: plan mode
entered autonomously (heartbeat / launch prompt) has no chat_id, so the gate fail-closes to deny —
and since ExitPlanMode is the only programmatic exit, that traps the session in plan mode. Resolving
a home chat_id from the active channel is the open follow-up.
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

# A reply that is PURELY one of these (after stripping punctuation/whitespace) approves the plan.
# Anything with extra content — e.g. "yes but rename X" — is treated as a revise request, not an
# approval, so substantive feedback never gets swallowed as a yes.
AFFIRMATIVES = {
    "y",
    "yes",
    "yep",
    "yeah",
    "go",
    "go ahead",
    "ok",
    "okay",
    "approve",
    "approved",
    "ship",
    "ship it",
    "lgtm",
    "proceed",
    "do it",
    "sounds good",
    "👍",
    "✅",
}

DENY_NO_REPLY = (
    "No approval arrived from the user's channel before the wait lapsed. Stay in plan mode; do not "
    "implement. Refine the plan or wait for the user's next message."
)
DENY_UNREACHABLE = (
    "The plan could not be posted to the user's channel, so it cannot be approved here. Stay in plan "
    "mode and present the plan as plain text in your reply instead."
)


def is_approval(reply: str) -> bool:
    return reply.strip().strip(".!").casefold() in AFFIRMATIVES


def render_plan(plan: str) -> str:
    body = plan.strip() or "_(empty plan)_"
    return (
        "📋 **Plan ready for approval**\n\n"
        f"{body}\n\n"
        "_Reply **yes / go / lgtm** to approve and start, or describe changes to keep planning._"
    )


def decision(behavior: str, message: str, tool_input: dict[str, object]) -> str:
    inner: dict[str, object] = {"behavior": behavior, "message": message}
    if behavior == "allow":
        inner["updatedInput"] = tool_input
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": inner,
            }
        }
    )


def main() -> None:
    try:
        event = t.as_obj(json.loads(sys.stdin.read() or "{}"))
    except json.JSONDecodeError:
        event = {}
    sid = event.get("session_id")
    sid = sid if isinstance(sid, str) else None
    tool_input = t.as_obj(event.get("tool_input"))
    plan = tool_input.get("plan")
    plan = plan if isinstance(plan, str) else ""

    turn = h.load_turn(sid)
    chat_id = turn.chat_id if turn else None
    # Newest message id BEFORE posting, so the Exchange only claims a genuinely new reply.
    since_id = h.channel_latest() if chat_id else None

    posted = bool(chat_id) and h.channel_send(render_plan(plan))

    reply = None
    if posted and chat_id and since_id is not None:
        ans = ex.ask(chat_id, None, since_id=since_id, timeout=ANSWER_TIMEOUT, label="plan")
        reply = ans.content if ans else None

    if reply is not None and is_approval(reply):
        behavior, message, outcome = "allow", f"User approved the plan: {reply}", "approved"
    elif reply is not None:
        behavior, outcome = "deny", "revise"
        message = (
            f"The user reviewed the plan on their channel and asked for changes: {reply}\n"
            "Stay in plan mode, revise the plan accordingly, then call ExitPlanMode again."
        )
    elif posted:
        behavior, message, outcome = "deny", DENY_NO_REPLY, "no_reply"
    else:
        behavior, message, outcome = "deny", DENY_UNREACHABLE, "unreachable"

    h.log_event("PermissionRequest", {"tool": "ExitPlanMode", "outcome": outcome})
    print(decision(behavior, message, tool_input))


if __name__ == "__main__":
    main()
