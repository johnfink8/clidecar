#!/usr/bin/env python3
"""Filter a Discord channel-messages payload down to deliverable inbound messages, oldest-first.

Pure transport helper for `msg.sh poll`: stdin is the raw JSON array from
GET /channels/{id}/messages (newest-first, as Discord returns it); stdout is one compact JSON
object per deliverable message, oldest-first, shaped for the gateway:
  {"id","chat_id","user","user_id","content","ts"}

Gating is the channel's security boundary, so it lives here (the Discord-aware adapter), not in
the Claude-facing gateway: drop bot authors and any sender not in access.json allowFrom. Knows
nothing about Claude — this is pure Discord-transport policy.
"""
import json
import os
import sys

ACCESS_FILE = os.path.expanduser("~/.claude/channels/discord/access.json")


def allowed_senders() -> set[str]:
    """A missing/broken access file means nothing is allowed — fail closed: an open inbound
    channel is a prompt-injection vector."""
    try:
        with open(ACCESS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return set()
    allow = data.get("allowFrom") if isinstance(data, dict) else None
    return {s for s in allow if isinstance(s, str)} if isinstance(allow, list) else set()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"discord poll: unparseable messages payload: {e}\n")
        return 1
    if not isinstance(payload, list):
        sys.stderr.write("discord poll: expected a JSON array of messages\n")
        return 1

    allow = allowed_senders()
    out: list[str] = []
    for msg in reversed(payload):  # Discord returns newest-first; deliver oldest-first.
        if not isinstance(msg, dict):
            continue
        author = msg.get("author")
        author = author if isinstance(author, dict) else {}
        if author.get("bot"):
            continue
        user_id = author.get("id")
        if not isinstance(user_id, str) or user_id not in allow:
            continue
        mid = msg.get("id")
        if not isinstance(mid, str):
            continue
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        attachments = msg.get("attachments")
        names: list[str] = []
        if isinstance(attachments, list):
            for a in attachments:
                fn = a.get("filename") if isinstance(a, dict) else None
                if isinstance(fn, str):
                    names.append(fn)
        if names:
            note = f"[{', '.join(names)} — attachments aren't supported on this channel yet]"
            content = f"{content}\n{note}" if content else note
        if not content:
            # No text and nothing else deliverable (sticker/embed/system msg). Don't push a hollow
            # envelope — Claude would see a blank message from the user and act on nothing.
            sys.stderr.write(f"discord poll: skipping message {mid} with no deliverable content\n")
            continue
        out.append(json.dumps({
            "id": mid,
            "chat_id": msg.get("channel_id") if isinstance(msg.get("channel_id"), str) else "",
            "user": author.get("username") if isinstance(author.get("username"), str) else "",
            "user_id": user_id,
            "content": content,
            "ts": msg.get("timestamp") if isinstance(msg.get("timestamp"), str) else "",
        }))
    sys.stdout.write("\n".join(out))
    if out:
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
