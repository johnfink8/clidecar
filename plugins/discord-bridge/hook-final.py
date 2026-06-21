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


def mark_done(state, turn):
    if state.get("message_id"):
        lines = h.work_lines(turn)[state.get("base", 0):]
        h.discord_edit(state["message_id"], h.render(lines, footer=f"{h.DONE} *done*"))
    src = state.get("source_message_id")
    if src:
        h.discord_react(src, h.SEEN, add=False)
        h.discord_react(src, h.DONE)


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

    text, already_sent = t.extract_closing(turn)

    if not flushed:
        # The closing answer never reached the transcript in time. Do NOT forward `text`:
        # it would be a stale intermediate line, not the answer.
        h.log_event("Stop", {"outcome": "unflushed", "intermediate": text[:300]})
        return fail(sid, "closing answer never flushed to the transcript; check console.")

    if already_sent:
        h.log_event("Stop", {"outcome": "skip", "reason": "already_sent"})
    elif not text:
        # Empty extraction is NOT success: distinguish it from already-delivered so a turn
        # whose closing answer we couldn't locate pings instead of going silent.
        return fail(sid, "couldn't locate the closing answer in the transcript; check console.")
    else:
        sent_ids = [h.discord_send(part) for part in chunks(text)]
        if any(mid is None for mid in sent_ids):
            # Discord refused. Record the answer to the OSError-guarded log FIRST, so a
            # durable trace exists even if the file persist below also fails.
            h.log_event("Stop", {"outcome": "undelivered", "answer": text, "sent_ids": sent_ids})
            path = h.persist_undelivered(sid, text)
            where = f"saved to {path}" if path else "could NOT be saved to disk"
            return fail(sid, f"Discord refused the final post — answer {where}; check console.")
        h.log_event("Stop", {"outcome": "sent", "parts": len(sent_ids), "ids": sent_ids})

    mark_done(state, turn)
    h.clear_turn(sid)


if __name__ == "__main__":
    main()
