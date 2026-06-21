#!/usr/bin/env python3
"""Filter a Discord channel-messages REST payload into deliverable inbound lines, oldest-first.

`msg.sh poll`: stdin is the raw JSON array from GET /channels/{id}/messages (newest-first, as
Discord returns it); stdout is one compact JSON line per deliverable message, oldest-first.
Gating + shaping live in gate.py, shared with the WS listener (listen.py).
"""
import json
import sys

import gate


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"discord poll: unparseable messages payload: {e}\n")
        return 1
    if not isinstance(payload, list):
        sys.stderr.write("discord poll: expected a JSON array of messages\n")
        return 1
    allow = gate.allowed_senders()
    lines = [line for msg in reversed(payload) if (line := gate.shape(msg, allow))]
    sys.stdout.write("\n".join(lines))
    if lines:
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
