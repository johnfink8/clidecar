#!/usr/bin/env python3
"""UserPromptSubmit hook — acknowledge a Discord-delivered prompt with a 👀 reaction.

A channel-delivered prompt carries its source `<channel chat_id=… message_id=…>`
wrapper, so we react to John's own message rather than posting a placeholder. Launch
or TUI prompts have no source message, so there is nothing to react to.
"""
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h


def envelope_attr(prompt: str | None, attr: str) -> str | None:
    if not prompt or "<channel" not in prompt:
        return None
    m = re.search(rf'{attr}="([^"]+)"', prompt)
    return m.group(1) if m else None


def source_message_id(prompt: str | None) -> str | None:
    return envelope_attr(prompt, "message_id")


def main() -> None:
    event = h.read_event("UserPromptSubmit")
    sid = event.session_id
    mid = source_message_id(event.prompt)
    # Reset per-turn state every turn (clearing the prior turn's done tombstone), whether
    # or not this prompt carries a source message to react to. chat_id is stashed so a mid-turn
    # AskUserQuestion can open an Exchange back on the same channel (see hook-question).
    with h.turn_lock(sid):
        h.save_turn(sid, h.TurnState(source_message_id=mid, chat_id=envelope_attr(event.prompt, "chat_id")))
    if not mid:
        h.log_event("UserPromptSubmit", {"outcome": "no_source_message"})
        return
    if not h.can("react"):
        # Channel can't react (e.g. Telegram); the status message itself is the visible ack.
        h.log_event("UserPromptSubmit", {"outcome": "no_react_capability", "source": mid})
        return
    ok = h.channel_react(mid, h.SEEN)
    h.log_event("UserPromptSubmit", {"outcome": "react" if ok else "react_failed", "source": mid})


if __name__ == "__main__":
    main()
