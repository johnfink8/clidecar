#!/usr/bin/env python3
"""PostToolUse + MessageDisplay hook — maintain the turn's live status message.

Re-renders the status message from the transcript on every trigger. PostToolUse catches
tool calls; MessageDisplay catches narration as it's displayed, so a narration surfaces
immediately instead of only when the next tool runs. Opened lazily by the first real
content (narration or a non-Discord tool), so an empty turn posts nothing — the 👀
reaction is the only acknowledgement. Re-homes (freezes the current message and starts a
fresh one) when a newer message has landed below it — John writing mid-turn — carrying
only the lines since the freeze so the new message never duplicates the old one.

Everything is rendered at line granularity (split_units) and SPILLS append-only: when the
shown tail no longer fits one message it freezes on a line boundary and continues in a fresh
one, so a long answer — streamed or committed — lays itself out across messages and nothing
already shown is ever removed. The Stop hook just caps the last message.
"""

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h
import transcript as t


def main() -> None:
    event = h.read_event("PostToolUse")
    sid = event.session_id
    is_md = event.hook_event_name == "MessageDisplay"
    turn = h.turn_from_path(event.transcript_path)
    blocks = h.turn_lines(turn)
    full = h.split_units(blocks)
    tid = t.turn_id(turn)

    # Locked so a concurrent PostToolUse + MessageDisplay can't both lazy-create the
    # status message; the second to enter sees the message_id and edits instead.
    with h.turn_lock(sid):
        state = h.load_turn(sid) or h.TurnState()
        if tid is not None and state.done == tid:
            return  # THIS turn already finalized by the Stop hook — don't resurrect it

        # Drop the live narration once the transcript commits it (block-level identity), so the
        # committed line-units aren't also re-appended from the live buffer.
        live = h.track_live(state, event) if is_md else h.live_text(state.live)
        if live and ("text", live) in blocks:
            state.live = None
            live = ""

        base = state.base
        mid = state.message_id

        if mid and not is_md and h.can("latest"):
            latest = h.channel_latest()  # re-home on tool boundaries, not per narration segment
            if latest and latest != mid:
                base, mid = (
                    min(state.shown, len(full)),
                    None,
                )  # freeze old message; new one starts here

        # The live narration's last line is still being typed; spill holds it unsealable so a seal
        # always lands on a completed line. Spill runs on streaming too (is_md) — that's what makes
        # a long pure-output answer lay itself out append-only instead of overflowing one message.
        live_units = h.split_units([("text", live)]) if live else []

        base, mid = h.spill(
            full, base, mid, live_units, h.WORKING_FOOTER, h.make_persist(sid, state)
        )

        combined = full + live_units
        tail = combined[base:]
        if not tail:
            return  # nothing new to show yet — never post a content-less status message

        body = h.render(tail, footer=h.WORKING_FOOTER, open_lang=h.fence_state(combined[:base]))
        if mid:
            if body == state.last_body:
                return  # unchanged — MessageDisplay can fire repeatedly mid-block; skip the redundant edit
            h.channel_edit(mid, body)
        else:
            mid = h.channel_send(body)
            if not mid:
                h.log_event("PostToolUse", {"outcome": "send_failed"})
                return
        state.base = base
        state.shown = len(full)
        state.last_body = body
        state.message_id = mid
        h.save_turn(sid, state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # This now runs on PreToolUse — a nonzero exit there can DENY the tool. Progress is
        # best-effort, so swallow any render-path crash and exit clean; never block a real call.
        h.log_event("progress_crash", {"error": repr(e)})
        sys.exit(0)
