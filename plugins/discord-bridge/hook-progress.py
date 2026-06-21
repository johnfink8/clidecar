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
import transcript as t


def main():
    event = h.read_event("PostToolUse")
    sid = event.get("session_id")
    is_md = event.get("hook_event_name") == "MessageDisplay"
    turn = h.turn_from_path(event.get("transcript_path"))
    full = h.turn_lines(turn)
    tid = t.turn_id(turn)

    # Locked so a concurrent PostToolUse + MessageDisplay can't both lazy-create the
    # status message; the second to enter sees the message_id and edits instead.
    with h.turn_lock(sid):
        state = h.load_turn(sid) or {}
        if tid is not None and state.get("done") == tid:
            return  # THIS turn already finalized by the Stop hook — don't resurrect it

        # Drop the live narration once the transcript commits it, so it never doubles.
        live = h.track_live(state, event) if is_md else h.live_text(state.get("live"))
        if live and ("text", live) in full:
            state.pop("live", None)
            live = ""

        base = state.get("base", 0)
        shown = state.get("shown", 0)
        mid = state.get("message_id")

        if mid and not is_md and h.can("latest"):
            latest = h.channel_latest()  # re-home on tool boundaries, not per narration segment
            if latest and latest != mid:
                base, mid = shown, None  # freeze the old message; the new one starts here

        lines = full[base:]
        if live:
            lines = lines + [("text", live)]
        if not lines:
            return  # nothing new to show yet — never post a content-less status message

        body = h.render(lines, footer=f"{h.WORKING} *working…*")
        if mid:
            if body == state.get("last_body"):
                return  # unchanged — MessageDisplay can fire repeatedly mid-block; skip the redundant edit
            h.channel_edit(mid, body)
        else:
            mid = h.channel_send(body)
            if not mid:
                return
        state.update(base=base, shown=len(full), last_body=body, message_id=mid)
        h.save_turn(sid, state)


if __name__ == "__main__":
    main()
