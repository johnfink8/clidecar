#!/usr/bin/env python3
"""Render recent Discord channel history into readable lines, oldest-first, for the gateway's
read-back tool (`msg.sh fetch` -> `clidecar_fetch`).

Distinct from gate.py on purpose: that path GATES inbound (drops bots + non-allowlisted senders so
untrusted input can't drive Claude). This is a deliberate READ of what's in the channel, so it
keeps every message — the bot's own replies included, since verifying how output rendered is the
whole point. Each line names its author so untrusted content stays visibly so.

stdin: the raw JSON array from GET /channels/{id}/messages (newest-first, as Discord returns it).
stdout: one readable line per message, oldest-first.
"""

import json
import sys


def render(msg: object) -> "str | None":
    if not isinstance(msg, dict):
        return None
    mid = msg.get("id")
    if not isinstance(mid, str):
        return None
    author = msg.get("author")
    author = author if isinstance(author, dict) else {}
    name = author.get("username") if isinstance(author.get("username"), str) else "?"
    if author.get("bot"):
        name = f"{name} [bot]"
    ts = msg.get("timestamp") if isinstance(msg.get("timestamp"), str) else ""
    content = msg.get("content") if isinstance(msg.get("content"), str) else ""
    attachments = msg.get("attachments")
    if isinstance(attachments, list):
        names = [
            fn
            for a in attachments
            if isinstance(a, dict) and isinstance(fn := a.get("filename"), str)
        ]
        if names:
            content = f"{content} [attachments: {', '.join(names)}]".strip()
    content = content.replace("\n", " ⏎ ")
    return f"[{ts}] {name}: {content} (id: {mid})"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"discord fetch: unparseable messages payload: {e}\n")
        return 1
    if not isinstance(payload, list):
        sys.stderr.write("discord fetch: expected a JSON array of messages\n")
        return 1
    lines = [line for msg in reversed(payload) if (line := render(msg))]
    if len(lines) < len(payload):
        sys.stderr.write(
            f"discord fetch: dropped {len(payload) - len(lines)} unrenderable message(s)\n"
        )
    sys.stdout.write("\n".join(lines))
    if lines:
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
