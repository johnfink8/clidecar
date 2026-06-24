#!/usr/bin/env python3
"""The Discord messaging adapter: one persistent discord.py client that owns BOTH inbound (the
Gateway WS → on_message) and outbound (send/edit/react/latest/fetch via the live connection). The
clidecar gateway daemon loads this via the plugin.json `client` entrypoint and drives it; the client
knows nothing about Claude or the broker — it speaks discord.py on one side and the small adapter
contract (the `ChannelClient` Protocol in bridge/channel.py) on the other.

Threading: discord.py is asyncio, the daemon is thread-based. The client runs its own event loop on
a dedicated thread. Outbound is reached by `dispatch()` coroutines scheduled onto that loop from the
daemon's threads (run_coroutine_threadsafe). Inbound flows the other way: on_message (on the loop
thread) hands each gate-shaped line to `on_inbound` — which the daemon makes non-blocking — so the
loop is never blocked and the broker's re-entrant outbound (the ❌ no-Claude react) can't deadlock.

Uses the discord-specific helpers beside it: gate.py (the security boundary — allowlist + bot-drop,
fail-closed), history.py (the read-back render), _message.py (the wire shape). discord.py owns
rate-limit backoff and WS reconnect.
"""

import asyncio
import os
import sys
import threading
import time
from collections.abc import Callable, Coroutine

import discord
import gate
import history

CREDS_FILE = os.path.expanduser("~/.claude/channels/discord/.env")

# Link down (pre-connect or a reconnect) longer than this ⇒ the client goes fatal so the daemon
# alerts + the supervisor relaunches it. Covers a sustained outage discord.py reconnects through
# forever, which would otherwise leave the client alive-but-silent.
DOWN_GRACE = 180.0
WATCHDOG_TICK = 15.0
READY_WAIT_S = 20.0  # bound an outbound's wait for the connection (the daemon caps the whole call)


def _read_creds() -> "tuple[str, str]":
    """(token, channel_id) from the Discord channel's .env. Empty strings if unset/unreadable; the
    caller fails loud on that."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "")
    try:
        with open(CREDS_FILE, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    token = token or line.split("=", 1)[1].strip().strip("\"'")
                elif line.startswith("DISCORD_CHANNEL_ID="):
                    channel_id = channel_id or line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return token, channel_id


def _raw(m: discord.Message) -> "dict[str, object]":
    """gate.shape / history.render consume raw-Discord field names (snake_case, string ids), not
    discord.py's typed accessors — so map a Message back to that shape."""
    return {
        "id": str(m.id),
        "channel_id": str(m.channel.id),
        "content": m.content,
        "timestamp": m.created_at.isoformat(),
        "author": {"id": str(m.author.id), "username": m.author.name, "bot": m.author.bot},
        "attachments": [{"filename": a.filename} for a in m.attachments],
    }


def _explain(runner: "asyncio.Future[None]") -> str:
    """Name why the client's run ended, for the daemon to surface cross-process."""
    exc = None if runner.cancelled() else runner.exception()
    if isinstance(exc, discord.LoginFailure):
        return f"login failure: {exc}"
    if isinstance(exc, discord.PrivilegedIntentsRequired):
        return "enable the MESSAGE CONTENT privileged intent in the Discord dev portal"
    if exc is not None:
        return f"connection ended: {exc!r}"
    return "connection closed"


class _Link:
    """Connection liveness shared between the event handlers and the watchdog. down_since is the
    monotonic time the link went (or started) down, None while connected."""

    def __init__(self) -> None:
        self.connected = False
        self.down_since: float | None = time.monotonic()


class _Listener(discord.Client):
    """discord.py dispatches to the on_* overrides. Pushes each allowed inbound, gate-shaped, to the
    injected sink; drives the _Link liveness; signals READY."""

    def __init__(
        self,
        channel_id: str,
        allow: "set[str]",
        on_inbound: "Callable[[str], None]",
        ready: threading.Event,
        link: _Link,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True  # privileged; without it the gateway sends empty content
        super().__init__(intents=intents)
        self._channel_id = channel_id
        self._allow = allow
        self._on_inbound = on_inbound
        self._ready_evt = (
            ready  # NOT `_ready` — discord.Client already owns that (an asyncio.Event)
        )
        self._link = link

    async def on_connect(self) -> None:
        self._link.connected = True
        self._link.down_since = None

    async def on_disconnect(self) -> None:
        if self._link.connected:
            self._link.connected = False
            self._link.down_since = time.monotonic()

    async def on_resumed(self) -> None:
        # discord.py RESUMEs routinely drop the WS (on_disconnect) and resume it WITHOUT a fresh
        # on_connect — so without this, down_since stays stuck from the disconnect and the watchdog
        # falsely fatals a healthy link after DOWN_GRACE. Clear liveness exactly like on_connect.
        self._link.connected = True
        self._link.down_since = None

    async def on_ready(self) -> None:
        self._ready_evt.set()

    async def on_message(self, message: discord.Message) -> None:
        if str(message.channel.id) != self._channel_id:
            return
        line = gate.shape(_raw(message), self._allow)
        if line:
            self._on_inbound(line)  # non-blocking by contract — must not block the event loop


class DiscordClient:
    """The adapter the daemon loads. Satisfies bridge/channel.ChannelClient structurally:
    `loop`, `fatal`/`fatal_reason`, `start()`, `shutdown()`, `dispatch(verb, *args) -> (code, str)`.
    """

    def __init__(self, on_inbound: "Callable[[str], None]") -> None:
        self._token, self._channel_id = _read_creds()
        self._ready = threading.Event()
        self.fatal = threading.Event()
        self.fatal_reason = ""
        self._stopping = False
        self._link = _Link()
        self._ch: discord.abc.Messageable | None = None
        self.loop = asyncio.new_event_loop()
        self._client = _Listener(
            self._channel_id, gate.allowed_senders(), on_inbound, self._ready, self._link
        )
        self._thread = threading.Thread(target=self._loop_main, name="discord-loop", daemon=True)

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self._token or not self._channel_id:
            self._fail("DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID not set")
            return
        try:
            int(self._channel_id)
        except ValueError:
            self._fail(f"DISCORD_CHANNEL_ID is not numeric: {self._channel_id!r}")
            return
        self._thread.start()

    def _fail(self, reason: str) -> None:
        """Mark the connection unrecoverable so the daemon alerts + the supervisor relaunches."""
        self.fatal_reason = reason
        self.fatal.set()

    def _loop_main(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._arun())
        finally:
            self.loop.close()

    async def _arun(self) -> None:
        watch = asyncio.ensure_future(self._watchdog())
        runner = asyncio.ensure_future(self._client.start(self._token))
        await asyncio.wait({runner, watch}, return_when=asyncio.FIRST_COMPLETED)
        if self._stopping:
            watch.cancel()
            return
        if not runner.done():  # watchdog tripped: link down past the grace
            self.fatal_reason = f"link down >{DOWN_GRACE:.0f}s"
            await self._client.close()
            runner.cancel()
        else:
            watch.cancel()
            self.fatal_reason = _explain(runner)
        self.fatal.set()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK)
            down = self._link.down_since
            if down is not None and time.monotonic() - down > DOWN_GRACE:
                return

    def shutdown(self, timeout: float = 5.0) -> None:
        """Tear down the WS before the daemon exits so a recycle doesn't leave a live double-connect.
        Idempotent and bounded — best-effort, never raises into the signal handler."""
        self._stopping = True
        if self._thread.is_alive() and self.loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._client.close(), self.loop)
                fut.result(timeout)
            except Exception:
                pass
        self._thread.join(timeout)

    # --- outbound ----------------------------------------------------------

    async def _channel(self) -> discord.abc.Messageable:
        if self._ch is not None:
            return self._ch
        cid = int(self._channel_id)
        ch = self._client.get_channel(cid) or await self._client.fetch_channel(cid)
        if not isinstance(ch, discord.abc.Messageable):
            # A wrong DISCORD_CHANNEL_ID is a permanent misconfig, not a transient blip — go fatal so
            # the daemon alerts + relaunches loudly, rather than silently 1-out every outbound forever.
            self._fail(
                f"DISCORD_CHANNEL_ID {cid} is not a messageable channel ({type(ch).__name__})"
            )
            raise RuntimeError(self.fatal_reason)
        self._ch = ch
        return ch

    def dispatch(self, verb: str, *args: str) -> "Coroutine[object, object, tuple[int, str]]":
        return self._dispatch(verb, *args)

    async def _dispatch(self, verb: str, *args: str) -> "tuple[int, str]":
        """Map an adapter verb to discord.py. Returns (0, stdout) on success — stdout carries the
        new message id for `send`/`latest` and the rendered lines for `fetch` — else (1, "")."""
        try:
            await asyncio.wait_for(self._client.wait_until_ready(), timeout=READY_WAIT_S)
            ch = await self._channel()
            if verb == "send":
                text = args[0] if args else ""
                reply_to = args[1] if len(args) > 1 else ""
                if reply_to:
                    ref = discord.MessageReference(
                        message_id=int(reply_to), channel_id=int(self._channel_id)
                    )
                    msg = await ch.send(text, reference=ref)
                else:
                    msg = await ch.send(text)
                return 0, str(msg.id)
            if verb == "edit":
                msg = await ch.fetch_message(int(args[0]))
                await msg.edit(content=args[1])
                return 0, ""
            if verb in ("react", "unreact"):
                msg = await ch.fetch_message(int(args[0]))
                if verb == "react":
                    await msg.add_reaction(args[1])
                    return 0, ""
                me = self._client.user
                if me is None:
                    return 1, ""  # ready but no bot user — invariant violation, don't fake success
                await msg.remove_reaction(args[1], me)
                return 0, ""
            if verb == "latest":
                async for msg in ch.history(limit=1):
                    return 0, str(msg.id)
                return 0, ""
            if verb == "fetch":
                n = int(args[0]) if args else 25
                rendered = [history.render(_raw(m)) async for m in ch.history(limit=n)]
                lines = [ln for ln in reversed(rendered) if ln]  # history() is newest-first
                return 0, ("\n".join(lines) + "\n" if lines else "")
            return 1, ""
        except (discord.HTTPException, discord.NotFound, ValueError, RuntimeError, OSError) as e:
            sys.stderr.write(f"discord dispatch {verb!r} failed: {e!r}\n")
            return 1, ""
