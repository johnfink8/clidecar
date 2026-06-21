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


def source_message_id(prompt):
    if "<channel" not in (prompt or ""):
        return None
    m = re.search(r'message_id="([^"]+)"', prompt)
    return m.group(1) if m else None


def main():
    event = h.read_event("UserPromptSubmit")
    sid = event.get("session_id")
    mid = source_message_id(event.get("prompt"))
    if not mid:
        h.log_event("UserPromptSubmit", {"outcome": "no_source_message"})
        return
    ok = h.discord_react(mid, h.SEEN)
    h.log_event("UserPromptSubmit", {"outcome": "react" if ok else "react_failed", "source": mid})
    h.save_turn(sid, {"source_message_id": mid})


if __name__ == "__main__":
    main()
