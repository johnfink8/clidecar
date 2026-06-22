#!/usr/bin/env python3
"""Stream inbound Discord messages over the Gateway WebSocket as deliverable JSON lines (push).

`msg.sh listen` holds a Discord Gateway connection (via discord.py) and writes one gate.shape()
line per new allowed message in the configured channel to stdout, flushed, as it arrives — the
sole inbound path. discord.py owns the Gateway lifecycle (heartbeat, RESUME, reconnect with
backoff).

gate.py is the single gating + line-shaping authority, so the security boundary lives in ONE place.

Fail-loud: anything the WS can't recover from — a bad token, the MESSAGE_CONTENT privileged intent
disabled, or the link staying down past DOWN_GRACE (never connecting, or a post-READY outage
discord.py reconnects through forever without success) — exits FATAL_EXIT. The core
(gateway.LISTEN_FATAL_EXIT) then alerts and relaunches the WS. Exiting is how a silently-dead link
becomes loud, not how we abandon the WS.
"""

import asyncio
import os
import signal
import sys
import time

import discord
import gate

# link down (pre-connect, or a reconnect) longer than this ⇒ exit so the core alerts + relaunches
DOWN_GRACE = 180.0
WATCHDOG_TICK = 15.0  # how often the liveness watchdog re-checks the down-timer
FATAL_EXIT = 4  # the core (gateway.LISTEN_FATAL_EXIT) reads this as "alert + relaunch the WS"


class _Link:
    """Connection-liveness shared between the gateway event handlers and the watchdog. down_since
    is the monotonic time the link went (or started) down, None while connected."""

    def __init__(self) -> None:
        self.connected = False
        self.down_since: float | None = time.monotonic()  # down until the first on_connect


def _raw(m: discord.Message) -> dict[str, object]:
    """gate.shape consumes raw-Discord field names (snake_case keys, string ids), not discord.py's
    typed accessors."""
    return {
        "id": str(m.id),
        "channel_id": str(m.channel.id),
        "content": m.content,
        "timestamp": m.created_at.isoformat(),
        "author": {"id": str(m.author.id), "username": m.author.name, "bot": m.author.bot},
        "attachments": [{"filename": a.filename} for a in m.attachments],
    }


def _explain(runner: asyncio.Future[None]) -> int:
    """Name the end reason on stderr so the core can surface it cross-process."""
    exc = None if runner.cancelled() else runner.exception()
    if isinstance(exc, discord.LoginFailure):
        sys.stderr.write(f"discord listen: FATAL login failure: {exc}\n")
    elif isinstance(exc, discord.PrivilegedIntentsRequired):
        sys.stderr.write(
            "discord listen: FATAL — enable the MESSAGE CONTENT privileged intent in the Discord dev portal\n"
        )
    elif exc is not None:
        sys.stderr.write(f"discord listen: connection ended: {exc!r}\n")
    else:
        sys.stderr.write("discord listen: connection closed — the core will relaunch the WS\n")
    return FATAL_EXIT


async def _watchdog(link: _Link) -> None:
    """Return once the link has been down longer than DOWN_GRACE. Covers what discord.py's own
    keepalive can't: a sustained outage it reconnects through forever, which would otherwise leave
    this process alive-but-silent while the core counts it healthy."""
    while True:
        await asyncio.sleep(WATCHDOG_TICK)
        if link.down_since is not None and time.monotonic() - link.down_since > DOWN_GRACE:
            return


async def _run(token: str, channel_id: str) -> int:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.message_content = True  # privileged; without it the gateway sends empty content
    client = discord.Client(intents=intents)
    allow = gate.allowed_senders()
    link = _Link()

    @client.event
    async def on_connect() -> None:
        link.connected = True
        link.down_since = None

    @client.event
    async def on_disconnect() -> None:
        if link.connected:
            link.connected = False
            link.down_since = time.monotonic()

    @client.event
    async def on_ready() -> None:
        sys.stderr.write("discord listen: READY\n")
        sys.stderr.flush()

    @client.event
    async def on_message(message: discord.Message) -> None:
        if str(message.channel.id) != channel_id:
            return
        line = gate.shape(_raw(message), allow)
        if line:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    loop = asyncio.get_running_loop()
    stopping = False

    def _stop() -> None:
        nonlocal stopping
        stopping = True
        loop.create_task(client.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop)

    runner = asyncio.ensure_future(client.start(token))
    watch = asyncio.ensure_future(_watchdog(link))
    done, _pending = await asyncio.wait({runner, watch}, return_when=asyncio.FIRST_COMPLETED)

    if runner in done:
        watch.cancel()
        # A signal-driven close is a deliberate stop, not an impairment: exit 0 so the core
        # relaunches the listener cleanly (or, if this was its own shutdown, ignores us).
        return 0 if stopping else _explain(runner)

    # watchdog tripped: link down past the grace — exit so the core alerts + relaunches the WS.
    sys.stderr.write(f"discord listen: link down >{DOWN_GRACE:.0f}s — exiting for relaunch\n")
    await client.close()
    await asyncio.wait({runner})
    return FATAL_EXIT


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        sys.stderr.write("discord listen: DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID not set\n")
        return FATAL_EXIT
    return asyncio.run(_run(token, channel_id))


if __name__ == "__main__":
    sys.exit(main())
