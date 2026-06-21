#!/usr/bin/env python3
"""Gate + shape one raw Discord message into a deliverable inbound line for the clidecar gateway.

Shared by both inbound paths — poll.py (REST batch) and listen.py (Gateway WS stream) — so the
security boundary and the line shape live in ONE place. A deliverable line is:
  {"id","chat_id","user","user_id","content","ts"}
Gating is the channel's security boundary (drop bots + anyone not in access.json allowFrom, fail
closed), so it lives here in the Discord-aware adapter, never in the Claude-facing gateway.
"""
import json
import os

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


def shape(msg: object, allow: "set[str]") -> "str | None":
    """Attachment-only messages are annotated rather than dropped, so the user learns they didn't
    come through. Returns None to drop (bot, not allow-listed, no id, or no deliverable content)."""
    if not isinstance(msg, dict):
        return None
    author = msg.get("author")
    author = author if isinstance(author, dict) else {}
    if author.get("bot"):
        return None
    user_id = author.get("id")
    if not isinstance(user_id, str) or user_id not in allow:
        return None
    mid = msg.get("id")
    if not isinstance(mid, str):
        return None
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
        return None
    return json.dumps({
        "id": mid,
        "chat_id": msg.get("channel_id") if isinstance(msg.get("channel_id"), str) else "",
        "user": author.get("username") if isinstance(author.get("username"), str) else "",
        "user_id": user_id,
        "content": content,
        "ts": msg.get("timestamp") if isinstance(msg.get("timestamp"), str) else "",
    })
