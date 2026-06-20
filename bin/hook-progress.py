#!/usr/bin/env python3
"""PostToolUse hook — maintain the turn's live status message.

Re-homes (posts a fresh status message) when a newer message has landed below the
current one — John writing mid-turn — so we never keep editing a message that has
scrolled out of view. The first tool call opens it; UserPromptSubmit only reacts.
"""
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h


def main():
    event = h.read_event("PostToolUse")
    sid = event.get("session_id")
    body = h.render_transcript(event.get("transcript_path"))

    state = h.load_turn(sid) or {}
    mid = state.get("message_id")
    latest = h.discord_latest()
    if not mid or (latest and latest != mid):
        new = h.discord_send(body)
        if not new:
            return
        state["message_id"] = new
        h.save_turn(sid, state)
    else:
        h.discord_edit(mid, body)


if __name__ == "__main__":
    main()
