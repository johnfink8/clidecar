#!/usr/bin/env python3
"""Stop hook — post the turn's closing answer to Discord as a new (pinging) message.

The deterministic fix for the dropped-reply problem: it fires every turn, regardless of
whether the model called the `reply` tool. Every failure edge is loud — if extraction
breaks, the answer is empty, the closing never flushed, or Discord refuses the post,
John gets a ⚠️ ping and/or the answer is persisted to disk; the status message is only
marked ✅ when the answer actually landed.

The Stop hook can fire before the closing message is flushed to the transcript, so we
bounded-poll for it; on timeout we fail loud rather than forward the stale intermediate.
"""
import os
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h
import transcript as t

CHUNK = 1900
POLL_TRIES = 50
POLL_INTERVAL = 0.1


def chunks(text):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > CHUNK and cur:
            out.append(cur)
            cur = ""
        cur += (line + "\n") if cur else line
    if cur:
        out.append(cur)
    return out or [text]


def await_closing(transcript):
    """Poll until the closing text is flushed; return (turn_rows, flushed)."""
    turn = []
    for _ in range(POLL_TRIES):
        turn = t.current_turn(t.load_rows(transcript))
        if t.closing_flushed(turn):
            return turn, True
        time.sleep(POLL_INTERVAL)
    return turn, False


def fail(sid, message, **log):
    """Surface a bridge failure loudly and stop — never mark the turn done."""
    h.log_event("Stop", {"outcome": "fail", "detail": message, **log})
    if not h.discord_send(f"⚠️ bridge: {message}"):
        # The warning ping itself couldn't land (Discord down). Leave a file trace so the
        # failure isn't invisible to the one person who could fix it.
        h.persist_undelivered(sid, f"⚠️ bridge failure (Discord unreachable): {message}")
    h.clear_turn(sid)


def deliver(sid, text):
    """Post the closing answer (chunked) to Discord; True on success. On refusal, log +
    persist + ⚠️ ping and return False so the caller bails — the answer is never lost
    silently."""
    sent_ids = [h.discord_send(part) for part in chunks(text)]
    if any(mid is None for mid in sent_ids):
        # Record the answer to the OSError-guarded log FIRST, so a durable trace exists
        # even if the file persist below also fails.
        h.log_event("Stop", {"outcome": "undelivered", "answer": text, "sent_ids": sent_ids})
        path = h.persist_undelivered(sid, text)
        where = f"saved to {path}" if path else "could NOT be saved to disk"
        fail(sid, f"Discord refused the final post — answer {where}; check console.")
        return False
    h.log_event("Stop", {"outcome": "sent", "parts": len(sent_ids), "ids": sent_ids})
    return True


def finalize(state, turn, answer=None):
    """Freeze the status message and swap the 👀 ack to ✅. With `answer` (a pure-output
    turn) the status block IS the whole answer, so render it in place rather than wiping it
    to an empty work block and reposting. Returns False only when an in-place answer
    couldn't be edited in (status message gone) so the caller delivers it normally and never
    drops it; a work-turn freeze is cosmetic, so its edit failing is non-fatal (the closing
    was already posted separately)."""
    mid = state.get("message_id")
    if answer is not None:
        if not mid or not h.discord_edit(mid, h.render([("text", answer)], footer=f"{h.DONE} *done*")):
            return False
    elif mid:
        lines = h.work_lines(turn)[state.get("base", 0):]
        h.discord_edit(mid, h.render(lines, footer=f"{h.DONE} *done*"))
    src = state.get("source_message_id")
    if src:
        h.discord_react(src, h.SEEN, add=False)
        h.discord_react(src, h.DONE)
    return True


def main():
    event = h.read_event("Stop")
    sid = event.get("session_id")
    transcript = event.get("transcript_path")
    state = h.load_turn(sid) or {}

    if not transcript or not os.path.exists(transcript):
        return fail(sid, "Stop hook got no transcript — answer may be lost; check console.")

    try:
        turn, flushed = await_closing(transcript)
    except ValueError as e:
        return fail(sid, f"couldn't parse transcript (check console). {str(e)[:200]}")

    tid = t.turn_id(turn)
    if tid is not None and state.get("done") == tid:
        # THIS exact turn's closing was already posted — a re-entrant Stop.
        h.log_event("Stop", {"outcome": "skip", "reason": "already_finalized", "turn": tid})
        return

    text, already_sent = t.extract_closing(turn)

    if not flushed:
        # The closing answer never reached the transcript in time. Do NOT forward `text`:
        # it would be a stale intermediate line, not the answer.
        h.log_event("Stop", {"outcome": "unflushed", "intermediate": text[:300]})
        return fail(sid, "closing answer never flushed to the transcript; check console.")

    if not already_sent and not text:
        # Empty extraction is NOT success: distinguish it from already-delivered so a turn
        # whose closing answer we couldn't locate pings instead of going silent.
        return fail(sid, "couldn't locate the closing answer in the transcript; check console.")

    # re-read: hook-progress may have created the status message during the poll.
    state = h.load_turn(sid) or state
    # Pure-output turn (no tools, no intermediate narration): the live status block already
    # shows the whole answer, so finalize THAT in place rather than wiping it to an empty
    # work block and reposting — the wipe is a jarring disappear-while-reading. Needs a
    # status message and a one-message answer; otherwise post the answer separately.
    fits_one_message = bool(text) and len(text) <= h.BODY_CAP - 40
    in_place = bool(state.get("message_id")) and not h.work_lines(turn) and not already_sent and fits_one_message

    if already_sent:
        h.log_event("Stop", {"outcome": "skip", "reason": "already_sent"})
    elif in_place:
        h.log_event("Stop", {"outcome": "in_place", "chars": len(text)})
    elif not deliver(sid, text):
        return

    # Locked + re-read for the freshest message_id, then tombstone the turn so a straggler
    # render can't resurrect it (the freeze and a trailing MessageDisplay can land in either
    # order). An in-place finalize that can't land falls back to a normal post — never drop.
    with h.turn_lock(sid):
        state = h.load_turn(sid) or state
        if not finalize(state, turn, answer=text if in_place else None) and not deliver(sid, text):
            return
        if tid is not None:
            state["done"] = tid
            h.save_turn(sid, state)
        else:
            h.clear_turn(sid)


if __name__ == "__main__":
    main()
