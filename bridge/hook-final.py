#!/usr/bin/env python3
"""Stop hook — post the turn's closing answer to Discord as a new (pinging) message.

The deterministic fix for the dropped-reply problem: it fires every turn, regardless of
whether the model called the `reply` tool. Every failure edge is loud — if extraction
breaks, the answer is empty, the closing never flushed, or Discord refuses the post,
the owner gets a ⚠️ ping and/or the answer is persisted to disk; the status message is only
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


def chunks(text: str) -> list[str]:
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > CHUNK and cur:
            out.append(cur)
            cur = ""
        cur += (line + "\n") if cur else line
    if cur:
        out.append(cur)
    return out or [text]


def await_closing(transcript: str) -> tuple[list[t.Row], bool]:
    """Poll until the closing text is flushed; return (turn_rows, flushed)."""
    turn: list[t.Row] = []
    for _ in range(POLL_TRIES):
        turn = t.current_turn(t.load_rows(transcript))
        if t.closing_flushed(turn):
            return turn, True
        time.sleep(POLL_INTERVAL)
    return turn, False


def fail(sid: str | None, message: str, **log: object) -> None:
    """Surface a bridge failure loudly and stop — never mark the turn done."""
    h.log_event("Stop", {"outcome": "fail", "detail": message, **log})
    if not h.channel_send(f"⚠️ bridge: {message}"):
        # The warning ping itself couldn't land (Discord down). Leave a file trace so the
        # failure isn't invisible to the one person who could fix it.
        h.persist_undelivered(sid, f"⚠️ bridge failure (Discord unreachable): {message}")
    h.clear_turn(sid)


def deliver(sid: str | None, text: str) -> bool:
    """Post the closing answer (chunked) to Discord; True on success. On refusal, log +
    persist + ⚠️ ping and return False so the caller bails — the answer is never lost
    silently."""
    sent_ids = [h.channel_send(part) for part in chunks(text)]
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


def lay_out(sid: str | None, state: h.TurnState, units: list[h.Item]) -> bool:
    """Lay the turn's content out append-only across the status messages and cap the last in place
    — never wipe-and-repost. The cap carries NO footer: the ⏳ working marker just drops, and the
    separate ✅ DONE_PING is the sole done marker (so the cap can't duplicate it). Re-runs the spill
    (the closing may have committed after the last progress render). Returns whether the full answer
    is now shown across the messages; False (a single over-cap line, or a failed cap) tells the
    caller to guarantee it via deliver()."""
    base, mid = h.spill(
        units, state.base, state.message_id, [], h.DONE_FOOTER, h.make_persist(sid, state)
    )
    state.base, state.message_id = base, mid
    tail = units[base:]
    if not h.fits(tail, footer=h.DONE_FOOTER):
        return False  # a single over-cap line can't be shown intact — deliver() guarantees it
    body = h.render(tail, open_lang=h.fence_state(units[:base]))
    if mid:
        return h.channel_edit(mid, body)
    mid = h.channel_send(
        body
    )  # spill nulled mid sealing the prior chunk: this fresh cap continues it
    state.message_id = mid
    return bool(mid)


def decorate_source(state: h.TurnState) -> None:
    src = state.source_message_id
    if src and h.can("react"):
        h.channel_react(src, h.DONE)  # add ✅ first so the reaction row never empties
        h.channel_react(src, h.SEEN, add=False)  # then drop 👀 — avoids a vertical-size flicker


def main() -> None:
    event = h.read_event("Stop")
    sid = event.session_id
    transcript = event.transcript_path
    state = h.load_turn(sid) or h.TurnState()
    # Closing answer lands in this agent's channel: the turn's chat_id, else its own channel (an
    # autonomous turn carries no inbound chat_id). Set BEFORE any channel_send (fail() sends).
    h.set_target(state.chat_id or h.channel_home())

    if not transcript or not os.path.exists(transcript):
        return fail(sid, "Stop hook got no transcript — answer may be lost; check console.")

    try:
        turn, flushed = await_closing(transcript)
    except ValueError as e:
        return fail(sid, f"couldn't parse transcript (check console). {str(e)[:200]}")

    tid = t.turn_id(turn)
    if tid is not None and state.done == tid:
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

    # Locked + re-read for the freshest message_id (hook-progress may have created/advanced the
    # status messages during the poll), then tombstone so a straggler render can't resurrect the
    # turn (the cap and a trailing MessageDisplay can land in either order).
    with h.turn_lock(sid):
        state = h.load_turn(sid) or state
        # The answer already lives across the append-only status messages (the live narration
        # streamed it there, spilling onto fresh messages as it grew). Cap the last in place —
        # never wipe the text out and repost it. work_lines drops the trailing closing only when
        # the reply tool already posted it as its own message (already_sent), to avoid a double.
        units = h.split_units(h.work_lines(turn) if already_sent else h.turn_lines(turn))
        covered = lay_out(sid, state, units)

        if already_sent:
            # The reply tool already posted the answer as its own message — it IS the done signal.
            h.log_event("Stop", {"outcome": "skip", "reason": "already_sent"})
        else:
            if not covered and not deliver(sid, text):  # over-cap line / failed cap — guarantee it
                return
            h.log_event(
                "Stop", {"outcome": "append_only" if covered else "delivered", "chars": len(text)}
            )

        decorate_source(state)
        if not already_sent:
            h.channel_send(h.DONE_PING)  # one tiny ✅ message — the sole done marker + push

        if tid is not None:
            state.done = tid
            h.save_turn(sid, state)
        else:
            h.clear_turn(sid)


if __name__ == "__main__":
    main()
