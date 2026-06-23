#!/usr/bin/env python3
"""Render recent Discord channel history into readable lines, oldest-first, for the gateway's
read-back tool (the client's `fetch` dispatch -> `clidecar_fetch`).

Distinct from gate.py on purpose: that path GATES inbound (drops bots + non-allowlisted senders so
untrusted input can't drive Claude). This is a deliberate READ of what's in the channel, so it
keeps every message — the bot's own replies included, since verifying how output rendered is the
whole point. Each line names its author so untrusted content stays visibly so.
"""

from _message import Message
from pydantic import ValidationError


def render(msg: object) -> "str | None":
    """None drops a message that won't validate (e.g. no id); the caller filters those out."""
    try:
        m = Message.model_validate(msg)
    except ValidationError:
        return None
    name = m.author.username or "?"
    if m.author.bot:
        name = f"{name} [bot]"
    content = m.content
    names = [a.filename for a in m.attachments if a.filename]
    if names:
        content = f"{content} [attachments: {', '.join(names)}]".strip()
    content = content.replace("\n", " ⏎ ")
    return f"[{m.timestamp}] {name}: {content} (id: {m.id})"
