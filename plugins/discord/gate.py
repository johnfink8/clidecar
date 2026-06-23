#!/usr/bin/env python3
"""Gate + shape one raw Discord message into a deliverable inbound line for the clidecar gateway.

Used by the client's inbound path (on_message, the Gateway WS stream), so the security boundary and
the line shape live in ONE place. A deliverable line is:
  {"id","chat_id","user","user_id","content","ts"}
Gating is the channel's security boundary (drop bots + anyone not in access.json allowFrom, fail
closed), so it lives here in the Discord-aware adapter, never in the Claude-facing gateway.
"""

import json
import os
import sys

from _message import Message
from pydantic import BaseModel, ConfigDict, ValidationError

ACCESS_FILE = os.path.expanduser("~/.claude/channels/discord/access.json")


class _Access(BaseModel):
    model_config = ConfigDict(extra="ignore")
    allowFrom: list[str] = []  # noqa: N815 — mirrors the access.json key the /discord:access skill writes


def allowed_senders() -> set[str]:
    """A missing/broken access file means nothing is allowed — fail closed: an open inbound
    channel is a prompt-injection vector. A missing file is a fresh-setup state (silent); a
    present-but-unparseable one is a config error that silently drops ALL inbound, so say so."""
    try:
        with open(ACCESS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return set()
    try:
        access = _Access.model_validate_json(raw)
    except ValidationError as e:
        sys.stderr.write(
            f"discord gate: access.json present but unparseable — allowing NOBODY: {e}\n"
        )
        return set()
    return set(access.allowFrom)


def shape(raw: object, allow: "set[str]") -> "str | None":
    """Attachment-only messages are annotated rather than dropped, so the user learns they didn't
    come through. Returns None to drop (unparseable, bot, not allow-listed, or no deliverable
    content). A message that won't validate is dropped, not raised — one malformed frame must not
    fell the inbound loop, and fail-closed is the safe default for the security boundary."""
    try:
        msg = Message.model_validate(raw)
    except ValidationError as e:
        # Real Discord frames always validate, so a failure here is wire-shape drift, not a normal
        # drop (bots/non-allowlisted never reach this) — surface it without breaking fail-closed.
        sys.stderr.write(f"discord gate: dropped an unparseable inbound frame: {e}\n")
        return None
    if msg.author.bot or msg.author.id not in allow:
        return None
    names = [a.filename for a in msg.attachments if a.filename]
    content = msg.content
    if names:
        note = f"[{', '.join(names)} — attachments aren't supported on this channel yet]"
        content = f"{content}\n{note}" if content else note
    if not content:
        return None
    return json.dumps(
        {
            "id": msg.id,
            "chat_id": msg.channel_id,
            "user": msg.author.username,
            "user_id": msg.author.id,
            "content": content,
            "ts": msg.timestamp,
        }
    )
