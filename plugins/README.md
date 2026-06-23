# Writing a clidecar messaging adapter

A **plugin** here is a messaging *adapter*: a transport for one chat service
(Discord, Telegram, Slack, iMessage, …). It knows how to send/receive on that
service and **nothing about Claude**. The clidecar bridge core (`bridge/`) is the only
thing that drives it.

Knowledge flows one way: the gateway knows an adapter exists and drives it; the adapter
never imports the bridge, never reasons about turns, claims, acks, or the AskUserQuestion
flow, and never talks to Claude's hooks. If you find yourself wanting Claude-awareness in
an adapter, it belongs in the gateway instead. This separation (**strict lanes**) is a hard
rule — it's what lets a new service be added without touching `bridge/`.

This document is the whole contract. You should be able to write a working adapter from it
without reading `plugins/discord/`. (That directory is a reference implementation, not the
spec.)

---

## What you ship

An adapter is a directory `plugins/<name>/` containing:

1. **`plugin.json`** — a manifest the gateway reads to discover you and learn what you can do.
2. **A Python module exposing a client class** — an in-process object the gateway imports and
   drives. It owns the provider connection (the WS/socket/poll loop) for **both inbound and
   outbound**, on its own asyncio event loop, and satisfies the small `ChannelClient` contract
   ([§4](#4-the-channelclient-contract)).

The adapter runs **in the gateway daemon's process** under its venv interpreter (`PYTHON_BIN`),
so it must be importable Python and may use the venv's deps (the discord adapter uses `discord.py`
+ `pydantic`). It is *not* a shelled-out script — the gateway imports your class and calls it
directly. This is what lets one persistent connection serve both directions and lets the library
own rate-limit backoff and reconnect.

---

## 1. The manifest — `plugin.json`

```json
{
  "name": "discord",
  "kind": "messaging",
  "description": "one line; says it's a transport that knows nothing about Claude",
  "client": "client:DiscordClient",
  "capabilities": { "edit": true, "react": true, "latest": true, "listen": true, "fetch": true }
}
```

| Field | Meaning |
|---|---|
| `name` | Adapter id. Becomes the channel `source` in the `<channel source="…">` envelope Claude sees. Must be word-chars. |
| `kind` | Must be `"messaging"`. The gateway only treats `kind:"messaging"` plugins as channels. |
| `client` | `"module:Class"` — the entrypoint the daemon imports from your plugin dir (added to `sys.path`) and constructs as the [`ChannelClient`](#4-the-channelclient-contract). |
| `capabilities` | A flat `{string: bool}` map of optional verbs your `dispatch` implements. **Declared = the gateway may call it; absent/false = the gateway degrades around it.** See [capabilities](#6-capability-negotiation). |

**How the active channel is chosen** (`bridge/channel.py`): `config.env CHANNEL` if set and
installed, else the sole installed messaging plugin. Zero plugins, an ambiguous set with no
`CHANNEL`, or a `CHANNEL` naming something that isn't an installed messaging adapter all resolve
to a **loud** no-channel (logged reason), never a silent one. A present-but-unparseable
`plugin.json` logs to stderr rather than silently demoting you to "not a channel" — so malformed
JSON fails visibly.

---

## 2. Outbound — the `dispatch` verbs

The gateway reaches outbound by scheduling `client.dispatch(verb, *args)` coroutines onto your
event loop (`run_coroutine_threadsafe`) and awaiting `(returncode, stdout)`. A verb returns `(0, …)`
on success, non-zero on failure. The gateway bounds each call by `TRANSPORT_TIMEOUT_S`, so don't
block forever inside one.

| Verb | Args | stdout on success | Notes |
|---|---|---|---|
| `send` | `text [reply_to_id]` | the created **message id** | `reply_to_id` quote-replies under an earlier message; omit for a normal post. |
| `edit` | `id text` | — | Edit a message **you** sent, in place, no push notification. |
| `react` | `id emoji` | — | Add the bot's reaction. |
| `unreact` | `id emoji` | — | Remove the bot's reaction. |
| `latest` | — | the newest message id | |
| `fetch` | `[limit]` | human-readable lines, **oldest-first** | A deliberate **read** of history, **including the bot's own messages** (unlike the gated inbound stream). Backs Claude's "read recent messages" tool — for verifying how output rendered, not for inbound dispatch. Each line names its author so untrusted content stays attributed. |

`send` returning the id is load-bearing: the gateway keeps a live status message and later `edit`s
it by that id, and `emit` returns the id up the stack. A success with no id is a failure — return
non-zero, don't return `(0, "")`.

---

## 3. Inbound — the line shape

Your client receives inbound on its own loop (e.g. discord.py's `on_message`), gates+shapes each
one, and calls the **`on_inbound(line)`** callback the gateway passed to your constructor. Each
`line` is one compact JSON object with these keys (all string-valued):

```json
{"id": "…", "chat_id": "…", "user": "…", "user_id": "…", "content": "…", "ts": "…"}
```

| Key | Required | Meaning |
|---|---|---|
| `id` | **yes** | Stable per-message id. Claude quote-replies by it and the gateway logs delivery by it. A line with no string `id` is dropped loudly. |
| `content` | **yes** (non-empty) | The message text. A message with no deliverable content is dropped — see gating below. |
| `chat_id` | populate | Conversation/channel id; Claude passes it back to `reply`. |
| `user` | populate | Human-readable sender name. |
| `user_id` | populate | Stable sender id (used for gating). |
| `ts` | populate | Timestamp (ISO-8601 recommended). |

The gateway maps this into the `<channel>` envelope Claude receives: `id → message_id`, `content`
→ the body, `chat_id/user/user_id/ts` → envelope attributes. Populate them all — they carry
provenance the user and Claude rely on.

**`on_inbound` MUST NOT block** (see [§5](#5-threading--the-no-deadlock-rule)): it returns
immediately (the gateway hands the line to a worker). Call it from your event loop and move on.

### Gating is YOUR job (the security boundary)

The gateway delivers whatever you pass to `on_inbound`. **An adapter must gate before calling it** —
this is the channel's security boundary and it lives in the adapter, never in the Claude-facing
gateway:

- **Drop bot authors** (don't let other bots, including your own echoes, drive Claude).
- **Drop any sender not on the allowlist.** **Fail closed**: a missing or unreadable allowlist
  means *nobody* is allowed — an open inbound channel is a prompt-injection vector.
- Decide what counts as "no deliverable content" and drop it (the discord adapter annotates
  attachment-only messages rather than dropping them, so the user learns they didn't get through).

The discord adapter centralizes gate+shape in one module (`gate.py`) the inbound path calls, so the
boundary and the line shape exist in exactly one place. Do the same.

The allowlist itself is managed outside the adapter (for discord: `~/.claude/channels/discord/access.json`,
`{"allowFrom": ["<user_id>", …]}`, edited only via the trusted `/discord:access` terminal skill —
never in response to a channel message). Your adapter only *reads* it.

---

## 4. The `ChannelClient` contract

Your `client` entrypoint class is constructed as `Class(on_inbound)` and must expose (the Protocol
lives in `bridge/channel.py`):

| Member | Contract |
|---|---|
| `loop` | The asyncio event loop your client runs on — the gateway schedules `dispatch` onto it via `run_coroutine_threadsafe`. Create it in `__init__` (before `start`). |
| `start()` | Begin running the connection on `loop`, on its own thread. Non-blocking. If you can't even begin (e.g. missing creds), set `fatal` instead of raising. |
| `dispatch(verb, *args)` | Return a coroutine resolving to `(returncode, stdout)` — the [§2](#2-outbound--the-dispatch-verbs) verbs. |
| `shutdown(timeout=…)` | Tear the connection down (so a recycle doesn't leave a live double-connect) and stop the loop/thread. Bounded, idempotent, never raises into the caller. |
| `fatal` | A `threading.Event` you set when the connection is **unrecoverable** (bad creds, missing permission, link down past your grace window). The gateway then alerts and exits so the supervisor relaunches the whole daemon. |
| `fatal_reason` | A short string explaining the fatal — surfaced in the alert. |

**Load your own credentials.** The daemon's environment has `config.env` vars (e.g. `PYTHON_BIN`)
but **not** your provider tokens. Read them yourself (the discord client reads
`~/.claude/channels/discord/.env`). If they're absent, set `fatal` — don't connect half-configured.

---

## 5. Threading & the no-deadlock rule

The gateway daemon is synchronous/thread-based; your client is asyncio. The seam:

- **Outbound** crosses sync→async: the gateway calls `run_coroutine_threadsafe(dispatch(...), loop)`
  from its own threads and blocks for the result. So `dispatch` runs on your loop; the gateway's
  call does not.
- **Inbound** crosses async→sync: your `on_message` (on the loop) calls `on_inbound(line)`. The
  gateway makes `on_inbound` **non-blocking** (it hands the line to a worker thread and returns at
  once). This is load-bearing: routing an inbound can re-enter outbound (the gateway reacts ❌ when
  no Claude is attached), which bridges back onto your loop — if `on_inbound` blocked the loop
  waiting on that, it would **deadlock**. So: never block your event loop on the result of handing
  off an inbound.

Your library (discord.py, etc.) owns transient reconnects/heartbeat/backoff and rate-limit retry.
Don't reimplement those — lean on it.

---

## 6. Capability negotiation

Declare in `capabilities` only the verbs your `dispatch` implements. The gateway checks a capability
before using it and **degrades** around anything missing — e.g. no `react` ⇒ no 👀/✅ acks; no
`edit` ⇒ no live status mirror. `send` is assumed (a channel that can't send isn't useful).
`listen` is **required for inbound** — it's the sole path in, so if the active adapter doesn't
declare it the gateway **alerts at boot** rather than coming up with silently-dead inbound.
Declaring a capability you don't implement is the one real footgun: the gateway will call it and
the call will fail loud.

---

## 7. The failure contract

- **Fail loud.** After your own retries, an unrecoverable outbound returns **non-zero** with a
  message on stderr — never `(0, …)` on a broken operation. A phantom success relocates the failure
  downstream to a user wondering why Claude went silent.
- **Fatal vs transient.** A blip your library reconnects through is not fatal — stay up. A condition
  you can't recover from (bad creds, missing intent/permission, link down past your grace window)
  sets `fatal` so the daemon relaunches. Don't sit alive-but-silent; a green process with dead
  inbound strands the user with no signal.
- **Don't swallow.** A success with a missing id, an unparseable payload, an empty allowlist —
  surface it, don't paper over it.
- **Idempotency & clean shutdown.** Emit each message once (a reconnect/RESUME must not re-emit).
  On `shutdown`, close the connection before the process exits so a recycle doesn't momentarily
  double-connect.

---

## 8. Checklist for a new adapter

1. `mkdir plugins/<name>/`, write `plugin.json` (`kind:"messaging"`, `client:"module:Class"`,
   honest `capabilities`).
2. Write the client class satisfying [`ChannelClient`](#4-the-channelclient-contract): `__init__(on_inbound)`,
   `loop`, `start()`, `dispatch`, `shutdown`, `fatal`/`fatal_reason`. Read your own creds; set
   `fatal` if they're missing.
3. Implement `dispatch` verbs: `send` (returns id) + `edit` + `react`/`unreact`; optionally `latest`
   and `fetch`.
4. Inbound: gate+shape each message and call `on_inbound(line)` ([§3](#3-inbound--the-line-shape)) —
   non-blocking. **Gate before emitting** (drop bots, fail-closed allowlist). Put gate+shape in one place.
5. Respect the [no-deadlock rule](#5-threading--the-no-deadlock-rule) and the
   [failure contract](#7-the-failure-contract).
6. Select it: set `CHANNEL=<name>` in `config.env` (only needed if more than one messaging plugin is installed).

If your class satisfies the contract and your inbound emits the line shape, the gateway drives your
service end-to-end with no changes to `bridge/`.
