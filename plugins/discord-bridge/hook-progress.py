#!/usr/bin/env python3
"""PostToolUse + MessageDisplay hook — maintain the turn's live status message.

Re-renders the status message from the transcript on every trigger. PostToolUse catches
tool calls; MessageDisplay catches narration as it's displayed, so a narration surfaces
immediately instead of only when the next tool runs. Opened lazily by the first real
content (narration or a non-Discord tool), so an empty turn posts nothing — the 👀
reaction is the only acknowledgement. Re-homes (freezes the current message and starts a
fresh one) when a newer message has landed below it — John writing mid-turn — carrying
only the lines since the freeze so the new message never duplicates the old one.
"""
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h


def main():
    event = h.read_event("PostToolUse")
    sid = event.get("session_id")
    full = h.lines_from_path(event.get("transcript_path"))

    state = h.load_turn(sid) or {}
    base = state.get("base", 0)
    shown = state.get("shown", 0)
    mid = state.get("message_id")

    if mid:
        latest = h.discord_latest()
        if latest and latest != mid:
            base, mid = shown, None  # freeze the old message; the new one starts here

    lines = full[base:]
    if not lines:
        return  # nothing new to show yet — never post a content-less status message

    body = h.render(lines)
    if mid:
        if body == state.get("last_body"):
            return  # unchanged — MessageDisplay can fire repeatedly mid-block; skip the redundant edit
        h.discord_edit(mid, body)
    else:
        mid = h.discord_send(body)
        if not mid:
            return
        state["message_id"] = mid
    state.update(base=base, shown=len(full), last_body=body)
    h.save_turn(sid, state)


if __name__ == "__main__":
    main()
