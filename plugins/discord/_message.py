#!/usr/bin/env python3
"""The raw-Discord message shape. Extra Discord fields are ignored and modeled fields carry
fallback defaults, so an unmodeled or absent field can never fail a whole message; only a missing
`id` raises ValidationError, which both callers (gate.py, history.py) treat as "drop"."""

from pydantic import BaseModel, ConfigDict


class Author(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    username: str = ""
    bot: bool = False


class Attachment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filename: str = ""


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    channel_id: str = ""
    content: str = ""
    timestamp: str = ""
    author: Author = Author()
    attachments: list[Attachment] = []
