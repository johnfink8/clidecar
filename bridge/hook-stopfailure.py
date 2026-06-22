#!/usr/bin/env python3
"""StopFailure hook — surface a turn that died on an API error.

The Stop hook (which guarantees the closing answer) does NOT fire when a turn is aborted
by an API error — Claude Code fires StopFailure instead. Without this hook such a turn is
pure silence: the user sees neither an answer nor a reason. So this posts a short notice to
the channel, biased toward the safe action: a known-transient error says "resend and I'll
pick it up"; only a known-hard error (auth/billing/…) says "this needs your attention";
anything unclassifiable hedges toward "try resending, check logs if it sticks" rather than
confidently telling the user the wrong thing.

We can't simulate an API error, so the first real failure is the live test. The full raw
payload is logged (stdin), so the actual error schema — which the docs don't pin down — is
captured on first fire; the error kind is meanwhile extracted best-effort, descending one
level into a nested `error` object since API errors arrive as `{"error": {"type": ...}}`.
"""

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import _hooklib as h
import transcript as t

WARN = "⚠️"

# Distinctive transient strings — safe to scan the WHOLE raw payload for; they don't appear
# spuriously, so a nested/oddly-keyed "overloaded" still classifies even if extraction missed it.
TRANSIENT_WORDS = ("overloaded", "rate_limit", "server_error", "capacity")
# Transient HTTP status codes — trusted only from the EXTRACTED kind, not the raw payload
# (an id or timestamp could contain "500" by chance and false-positive a raw scan).
TRANSIENT_CODES = ("500", "502", "503", "529")
# Hard errors — matched on the extracted kind only; the raw payload carries noise like
# "permission_mode":"auto" that a substring scan would mis-read as a permission error.
HARD = ("authentication", "oauth", "billing", "invalid_request", "model_not_found", "401", "403")

# The error kind's field name isn't documented and we can't simulate it, so probe the likely
# keys (the raw payload is logged in full regardless) rather than commit to one.
ERROR_KEYS = ("error", "error_type", "reason", "matcher", "subtype", "type", "status", "message")
NESTED_KEYS = ("type", "subtype", "reason", "message")


def _scalar(v: object) -> str | None:
    if isinstance(v, str) and v:
        return v
    if isinstance(v, (int, float)):
        return str(v)
    return None


def error_kind(d: dict[str, object]) -> str:
    for k in ERROR_KEYS:
        v = d.get(k)
        s = _scalar(v)
        if s:
            return s
        nested = t.as_obj(v)  # API errors nest as {"error": {"type": ...}}; {} for non-dicts
        for nk in NESTED_KEYS:
            ns = _scalar(nested.get(nk))
            if ns:
                return ns
    return "unknown"


def classify(kind: str, raw: str) -> str:
    hay = f"{kind} {raw}".lower()
    if any(w in hay for w in TRANSIENT_WORDS) or any(c in kind for c in TRANSIENT_CODES):
        return "transient"
    if any(tok in kind.lower() for tok in HARD):
        return "hard"
    return "unknown"


def notice(kind: str, klass: str) -> str:
    tag = kind.replace("`", "").replace("\n", " ")[:80]  # keep the backtick span intact
    if klass == "transient":
        return (
            f"{WARN} that turn hit a transient API error (`{tag}`) and stopped before I could "
            "reply. The API is usually back in a moment — resend and I'll pick it up."
        )
    if klass == "hard":
        return (
            f"{WARN} that turn stopped on an API error (`{tag}`) that likely needs your "
            "attention, not just a resend. Check the console / `clidecar logs`."
        )
    return (
        f"{WARN} that turn stopped on an API error (`{tag}`). It may be transient — try "
        "resending; if it sticks, check the console / `clidecar logs`."
    )


def main() -> None:
    raw = sys.stdin.read()
    try:
        parsed: object = json.loads(raw or "{}")
    except json.JSONDecodeError:
        parsed = None
    event = h.HookEvent.from_obj(parsed)
    sid = event.session_id
    kind = error_kind(t.as_obj(parsed))
    klass = classify(kind, raw)
    h.log_event(
        event.hook_event_name or "StopFailure", {"stdin": raw, "kind": kind, "class": klass}
    )

    state = h.load_turn(sid)
    src = state.source_message_id if state else None
    msg = notice(kind, klass)
    if not h.channel_send(msg, reply_to=src):
        # Gateway unreachable AND (if path is None) the disk write also failed — the total-
        # silence case this hook exists to prevent. Trace the fallback's own outcome so even
        # that is recorded, mirroring the Stop hook's handling of the same situation.
        path = h.persist_undelivered(sid, msg)
        h.log_event("StopFailure", {"outcome": "undelivered", "persisted": path})

    # Mark the source message so its 👀 doesn't keep implying the turn succeeded — but only
    # drop the 👀 once the ⚠️ actually lands, so a failed react can't leave the row empty.
    if src and h.can("react") and h.channel_react(src, WARN):
        h.channel_react(src, h.SEEN, add=False)


if __name__ == "__main__":
    main()
