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
APPROVAL_TIMEOUT = 600.0  # fallback View/Modal lifetime when the buttons call carries no deadline
VIEW_GRACE_S = (
    15.0  # the View outlives the hook's reply wait by this, so no claim-open-but-View-shut gap
)

# A bound (interaction, content, channel_id) -> bool: synthesize an inbound from the interaction in
# that channel and deliver it, returning True only if it passed the allowlist gate (False = a
# non-allowlisted click, dropped).
Deliver = Callable[[discord.Interaction, str, str], bool]


def _read_creds() -> "tuple[str, str]":
    """(token, legacy_channel_id) from the Discord channel's .env. The token is the real credential;
    legacy_channel_id is only a `home` fallback for a single-agent setup — multi-agent routing comes
    from the fleet via set_channels(). Empty strings if unset/unreadable; start() fails loud on a
    missing token."""
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
        is_allowed: "Callable[[str], bool]",
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
        self._is_allowed = is_allowed  # channel-id membership, read live (the fleet's listen-set)
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
        if not self._is_allowed(str(message.channel.id)):
            return  # not a fleet channel (agent channel or control) — ignore
        line = gate.shape(_raw(message), self._allow)
        if line:
            self._on_inbound(line)  # non-blocking by contract — must not block the event loop


def _deliver_interaction(
    on_inbound: "Callable[[str], None]",
    allow: "set[str]",
    channel_id: str,
    interaction: discord.Interaction,
    content: str,
) -> bool:
    """Turn a button/modal interaction into a gate-shaped inbound line and hand it to the daemon's
    sink, exactly as on_message does. The interaction's snowflake id is newer than the plan message,
    so a waiting ex.ask claim matches it. Returns True only if gate.shape passed the allowlist — a
    non-allowlisted click is dropped (fail-closed) and returns False so the caller doesn't stamp a
    success it didn't cause."""
    user = interaction.user
    raw: dict[str, object] = {
        "id": str(interaction.id),
        "channel_id": str(channel_id),
        "content": content,
        "timestamp": interaction.created_at.isoformat(),
        "author": {"id": str(user.id), "username": user.name, "bot": user.bot},
        "attachments": [],
    }
    line = gate.shape(raw, allow)
    if line is None:
        return False
    on_inbound(line)
    return True


async def _deny_click(interaction: discord.Interaction) -> None:
    """Acknowledge a non-allowlisted click without touching the plan message — Discord requires a
    response within 3s, and leaving the buttons in place keeps the gate honest (no false stamp)."""
    await interaction.response.send_message(
        "You're not on this channel's allowlist, so this isn't yours to decide.", ephemeral=True
    )


class _ReviseModal(discord.ui.Modal):
    """The Request-changes popup: the owner's typed feedback becomes the revise-context — a non-affirmative
    reply, so the plan hook DENIES and stays in plan mode."""

    def __init__(
        self,
        deliver: "Deliver",
        plan_message: "discord.Message | None",
        timeout: float,
        channel_id: str,
    ) -> None:
        super().__init__(title="Request changes", timeout=timeout)
        self._deliver = deliver
        self._plan_message = plan_message
        self._channel_id = channel_id
        self._feedback: discord.ui.TextInput[discord.ui.View] = discord.ui.TextInput(
            label="What should change?", style=discord.TextStyle.paragraph, max_length=1500
        )
        self.add_item(self._feedback)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        delivered = self._deliver(
            interaction,
            str(self._feedback.value).strip() or "(no feedback given)",
            self._channel_id,
        )
        if not delivered:
            await _deny_click(interaction)
            return
        body = self._plan_message.content if self._plan_message else ""
        await interaction.response.edit_message(
            content=f"{body}\n\n📝 Changes requested.", view=None
        )


class _ApprovalView(discord.ui.View):
    """Approve / Request-changes buttons on a plan message. Approve delivers a bare affirmative so the
    plan hook's is_approval matches and plan mode exits; Request-changes opens a modal whose text is
    the revise-context. The timeout outlives the hook's wait so a click always lands while the claim
    is open."""

    def __init__(self, deliver: "Deliver", timeout: float, channel_id: str) -> None:
        super().__init__(timeout=timeout)
        self._deliver = deliver
        self._channel_id = channel_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(
        self, interaction: discord.Interaction, _button: "discord.ui.Button[discord.ui.View]"
    ) -> None:
        if not self._deliver(interaction, "approve", self._channel_id):
            await _deny_click(interaction)
            return
        body = interaction.message.content if interaction.message else ""
        await interaction.response.edit_message(content=f"{body}\n\n✅ Approved.", view=None)

    @discord.ui.button(label="Request changes", style=discord.ButtonStyle.secondary)
    async def revise(
        self, interaction: discord.Interaction, _button: "discord.ui.Button[discord.ui.View]"
    ) -> None:
        await interaction.response.send_modal(
            _ReviseModal(
                self._deliver,
                interaction.message,
                self.timeout or APPROVAL_TIMEOUT,
                self._channel_id,
            )
        )


class DiscordClient:
    """The adapter the daemon loads. Satisfies bridge/channel.ChannelClient structurally:
    `loop`, `fatal`/`fatal_reason`, `start()`, `shutdown()`, `dispatch(verb, *args) -> (code, str)`.
    """

    def __init__(self, on_inbound: "Callable[[str], None]") -> None:
        self._token, self._home_channel_id = _read_creds()
        self._on_inbound = on_inbound
        self._allow = gate.allowed_senders()
        # The fleet's listen-set, installed by set_channels before start() and live on every change.
        # frozenset so on_message (loop thread) reads it lock-free while a refresh atomically swaps it.
        self._channels_allowed: frozenset[str] = frozenset()
        self._ready = threading.Event()
        self.fatal = threading.Event()
        self.fatal_reason = ""
        self._stopping = False
        self._link = _Link()
        self._ch_cache: dict[str, discord.abc.Messageable] = {}
        self.loop = asyncio.new_event_loop()
        self._client = _Listener(
            self._is_channel_allowed, self._allow, on_inbound, self._ready, self._link
        )
        self._thread = threading.Thread(target=self._loop_main, name="discord-loop", daemon=True)

    def set_channels(self, channel_ids: "set[str]") -> None:
        self._channels_allowed = frozenset(channel_ids)

    def _is_channel_allowed(self, channel_id: str) -> bool:
        return channel_id in self._channels_allowed

    def _deliver(self, interaction: discord.Interaction, content: str, channel_id: str) -> bool:
        return _deliver_interaction(self._on_inbound, self._allow, channel_id, interaction, content)

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self._token:
            self._fail("DISCORD_BOT_TOKEN not set")
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

    async def _channel(self, chat_id: str) -> discord.abc.Messageable:
        cached = self._ch_cache.get(chat_id)
        if cached is not None:
            return cached
        cid = int(chat_id)
        ch = self._client.get_channel(cid) or await self._client.fetch_channel(cid)
        if not isinstance(ch, discord.abc.Messageable):
            # A non-messageable channel id is a misconfigured fleet route, not a transient blip — but
            # with many channels it's per-route, so raise (caller 1-outs + logs) rather than fatal the
            # whole client and take down every other agent's channel.
            raise RuntimeError(f"channel {cid} is not messageable ({type(ch).__name__})")
        self._ch_cache[chat_id] = ch
        return ch

    async def _create_channel(self, parent_id: str, rest: "tuple[str, ...]") -> "tuple[int, str]":
        """Create a text channel as a sibling of `parent_id` (same guild + same category) and return
        its new id on stdout. The gateway uses this to give a freshly spawned agent its own channel so
        the owner never hand-creates one. Needs the bot's Manage Channels permission in that category;
        a Forbidden/HTTPException is caught by the caller and reported as a failed dispatch (1, "")."""
        name = rest[0] if rest else ""
        if not name:
            sys.stderr.write("discord create_channel: missing name\n")
            return 1, ""
        parent = self._client.get_channel(int(parent_id)) or await self._client.fetch_channel(
            int(parent_id)
        )
        if not isinstance(parent, discord.TextChannel):
            # The locator (the control channel) must be a normal text channel so it has a guild +
            # category to create a sibling under — a misconfig, so fail loud rather than guess.
            sys.stderr.write(
                f"discord create_channel: parent {parent_id} is not a text channel "
                f"({type(parent).__name__})\n"
            )
            return 1, ""
        new = await parent.guild.create_text_channel(name, category=parent.category)
        return 0, str(new.id)

    def dispatch(self, verb: str, *args: str) -> "Coroutine[object, object, tuple[int, str]]":
        return self._dispatch(verb, *args)

    async def _dispatch(self, verb: str, *args: str) -> "tuple[int, str]":
        """Map an adapter verb to discord.py. Every channel verb takes the target chat_id as its FIRST
        arg (multi-agent routing); `home` takes none. Returns (0, stdout) on success — stdout carries
        the new message id for `send`/`latest` and the rendered lines for `fetch` — else (1, "")."""
        if verb == "home":
            return 0, self._home_channel_id  # pure config — answerable even while the link is down
        if not args:
            sys.stderr.write(f"discord dispatch {verb!r}: missing chat_id\n")
            return 1, ""
        chat_id, rest = args[0], args[1:]
        try:
            await asyncio.wait_for(self._client.wait_until_ready(), timeout=READY_WAIT_S)
            if verb == "create_channel":
                return await self._create_channel(chat_id, rest)
            ch = await self._channel(chat_id)
            if verb == "send":
                text = rest[0] if rest else ""
                reply_to = rest[1] if len(rest) > 1 else ""
                if reply_to:
                    ref = discord.MessageReference(
                        message_id=int(reply_to), channel_id=int(chat_id)
                    )
                    msg = await ch.send(text, reference=ref)
                else:
                    msg = await ch.send(text)
                return 0, str(msg.id)
            if verb == "edit":
                msg = await ch.fetch_message(int(rest[0]))
                await msg.edit(content=rest[1])
                return 0, ""
            if verb in ("react", "unreact"):
                msg = await ch.fetch_message(int(rest[0]))
                if verb == "react":
                    await msg.add_reaction(rest[1])
                    return 0, ""
                me = self._client.user
                if me is None:
                    return 1, ""  # ready but no bot user — invariant violation, don't fake success
                await msg.remove_reaction(rest[1], me)
                return 0, ""
            if verb == "latest":
                async for msg in ch.history(limit=1):
                    return 0, str(msg.id)
                return 0, ""
            if verb == "fetch":
                n = int(rest[0]) if rest else 25
                rendered = [history.render(_raw(m)) async for m in ch.history(limit=n)]
                lines = [ln for ln in reversed(rendered) if ln]  # history() is newest-first
                return 0, ("\n".join(lines) + "\n" if lines else "")
            if verb == "buttons":
                # The View lifetime derives from the hook's reply wait (rest[1]) plus a grace, so
                # there's one source of truth for it — not a magic constant re-declared here.
                mid = rest[0] if rest else ""
                view_timeout = float(rest[1]) + VIEW_GRACE_S if len(rest) > 1 else APPROVAL_TIMEOUT
                msg = await ch.fetch_message(int(mid))
                await msg.edit(view=_ApprovalView(self._deliver, view_timeout, chat_id))
                return 0, ""
            return 1, ""
        except (discord.HTTPException, discord.NotFound, ValueError, RuntimeError, OSError) as e:
            sys.stderr.write(f"discord dispatch {verb!r} failed: {e!r}\n")
            return 1, ""
