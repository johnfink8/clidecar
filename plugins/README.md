# Writing a clidecar messaging adapter

A **plugin** here is a messaging *adapter*: a dumb transport for one chat service
(Discord, Telegram, Slack, iMessage, …). It knows how to send/receive bytes on that
service and **nothing about Claude**. The clidecar bridge core (`bridge/`) is the only
thing that drives it.

Knowledge flows one way: the gateway knows an adapter exists and calls it; the adapter
never imports the bridge, never reasons about turns, claims, acks, or the AskUserQuestion
flow, and never talks to Claude's hooks. If you find yourself wanting Claude-awareness in
an adapter, it belongs in the gateway instead. This separation (**strict lanes**) is a hard
rule — it's what lets a new service be added without touching `bridge/`.

This document is the whole contract. You should be able to write a working adapter from it
without reading `plugins/discord/`. (That directory is a reference implementation, not the
spec.)

---

## What you ship

An adapter is a directory `plugins/<name>/` containing exactly two required things:

1. **`plugin.json`** — a manifest the gateway reads to discover you and learn what you can do.
2. **A transport script** — one executable that implements a fixed set of *verbs* (subcommands).
   It can shell out to helper scripts in the same dir; the gateway only ever calls the one
   script named in the manifest.

Everything else (helpers, language, deps) is your business. The discord adapter happens to be
`msg.sh` (bash) plus Python helpers, but a single Python or Go binary that implements the verbs
is equally valid.

---

## 1. The manifest — `plugin.json`

```json
{
  "name": "discord",
  "kind": "messaging",
  "description": "one line; says it's a dumb transport, knows nothing about Claude",
  "transport": "${PLUGIN_DIR}/msg.sh",
  "capabilities": { "edit": true, "react": true, "latest": true, "listen": true, "fetch": true }
}
```

| Field | Meaning |
|---|---|
| `name` | Adapter id. Becomes the channel `source` in the `<channel source="…">` envelope Claude sees. Must be word-chars. |
| `kind` | Must be `"messaging"`. The gateway only treats `kind:"messaging"` plugins as channels. |
| `transport` | Path to your transport script. `${PLUGIN_DIR}` is substituted with this plugin's absolute dir at resolve time — use it, don't hardcode paths (the repo is public). |
| `capabilities` | A flat `{string: bool}` map of optional verbs you implement. **Declared = the gateway may call it; absent/false = the gateway degrades around it.** See [capabilities](#5-capability-negotiation). |

**How the active channel is chosen** (`bridge/channel.py`): `config.env CHANNEL` if set and
installed, else the sole installed messaging plugin. Zero plugins, an ambiguous set with no
`CHANNEL`, or a `CHANNEL` naming something that isn't an installed messaging adapter all resolve
to a **loud** no-channel (logged reason), never a silent one. A present-but-unparseable
`plugin.json` logs to stderr rather than silently demoting you to "not a channel" — so malformed
JSON fails visibly.

---

## 2. The transport verbs

The gateway invokes `transport <verb> [args…]`. Verbs split into **outbound** (the gateway
tells you to do something), **inbound** (you hand the gateway new messages), and **history**.

### Outbound — always required for a useful channel

| Verb | Args | stdout | Notes |
|---|---|---|---|
| `send` | `"text" [reply_to_id]` | the created **message id** | `reply_to_id` quote-replies under an earlier message; omit for a normal post. A 2xx with no id is a failure — fail loud, don't print empty. |
| `edit` | `<id> "text"` | — | Edit a message **you** sent, in place, with no push notification. |
| `react` | `<id> <emoji> [chan]` | — | Add the bot's reaction. Unicode emoji passed through directly. |
| `unreact` | `<id> <emoji> [chan]` | — | Remove the bot's reaction. |

`send` printing the id is load-bearing: the gateway keeps a live status message and later
`edit`s it by that id, and `emit` returns the id up the stack.

### Inbound — implement `listen`

`listen` is the sole inbound path: a push stream over a long-lived connection. There is no REST
pull fallback — an adapter that can't push has no inbound.

| Verb | Args | Behavior |
|---|---|---|
| `listen` | — | **Long-running.** Hold a push connection and write **deliverable lines** ([§3](#3-the-inbound-line-shape)) to stdout, **flushed, as they arrive**, one per line. Runs until killed. See [§6](#6-the-listen-lifecycle--exit-codes) for its lifecycle and exit-code contract. |

### History — optional read-back

| Verb | Args | stdout | Notes |
|---|---|---|---|
| `fetch` | `[limit]` | human-readable lines, **oldest-first** | A deliberate **read** of channel history, **including the bot's own messages** (unlike the gated `listen` stream, which drops them). Backs Claude's "read recent messages" tool — for verifying how output rendered, not for inbound dispatch. Each line names its author so untrusted content stays visibly attributed. Free-form text, one message per line; no fixed schema. |

---

## 3. The inbound line shape

Every line emitted by `listen` is one compact JSON object with these keys (all string-valued):

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

The gateway maps this line into the `<channel>` envelope Claude receives:
`id → message_id`, `content` → the body, and `chat_id/user/user_id/ts` → envelope attributes.
So populate them all — they carry provenance the user and Claude rely on.

### Gating is YOUR job (the security boundary)

The gateway delivers whatever you emit. **An adapter must gate before emitting** — this is the
channel's security boundary and it lives in the adapter, never in the Claude-facing gateway:

- **Drop bot authors** (don't let other bots, including your own echoes, drive Claude).
- **Drop any sender not on the allowlist.** **Fail closed**: a missing or unreadable allowlist
  means *nobody* is allowed — an open inbound channel is a prompt-injection vector.
- Decide what counts as "no deliverable content" and drop it (the discord adapter annotates
  attachment-only messages rather than dropping them, so the user learns they didn't get through).

The discord adapter centralizes gate+shape in one module (`gate.py`) that the `listen` path calls,
so the boundary and the line shape exist in exactly one place. Do the same.

The allowlist itself is managed outside the adapter (for discord: `~/.claude/channels/discord/access.json`,
`{"allowFrom": ["<user_id>", …]}`, edited only via the trusted `/discord:access` terminal skill —
never in response to a channel message). Your adapter only *reads* it.

---

## 4. How the gateway calls you

- **Synchronous verbs** (`send`/`edit`/`react`/`unreact`/`latest`/`fetch`) are run as
  `subprocess.run([script, verb, *args])` with captured stdout, **bounded by `TRANSPORT_TIMEOUT_S`**.
  A verb that hangs (or sleeps on a rate-limit longer than the bound) is killed and treated as a
  failure — so don't block indefinitely inside a synchronous verb.
- **`listen`** is run as a long-lived `subprocess.Popen([script, "listen"])`; the gateway reads
  its stdout line-by-line and supervises its exit ([§6](#6-the-listen-lifecycle--exit-codes)).
- **Environment & creds:** the script inherits the gateway daemon's environment (which sourced
  `config.env`, so e.g. `PYTHON_BIN` is present). **Load your own credentials** — the discord
  adapter sources `config.env` and `~/.claude/channels/discord/.env` at the top of `msg.sh`. Don't
  expect the gateway to hand you tokens.
- **Interpreter for `listen`:** if your listener needs venv-only deps, exec it under
  `${PYTHON_BIN:-python3}` (the venv interpreter) so the imports resolve. Keep the lighter verbs on
  a portable interpreter if you can.
- `$0` is the absolute path the manifest resolved to, so `$(dirname "$0")` reliably finds sibling
  helper scripts.

---

## 5. Capability negotiation

Declare in `capabilities` only the verbs you actually implement. The gateway checks a capability
before using it and **degrades** around anything missing — e.g. no `react` ⇒ no 👀/✅ acks; no
`edit` ⇒ no live status mirror. `send` is assumed (a channel that can't send isn't useful).
`listen` is **required for inbound** — it's the sole path in, so if the active adapter doesn't
declare it the gateway **alerts at boot** rather than coming up with silently-dead inbound.
Declaring a capability you don't implement is the one real footgun: the gateway will call it and
the call will fail loud.

---

## 6. The `listen` lifecycle & exit codes

`listen` owns its own transient reconnects (heartbeat, resume, backoff) — a well-built listener
stays up across blips on its own. Because it self-heals transients, **its exit is meaningful**, and
the exit code is a signal to the gateway:

| Exit | Meaning | Gateway response |
|---|---|---|
| `0` | Deliberate stop (it received SIGTERM/SIGINT — e.g. daemon shutdown). | Clean — relaunched on next start, no alarm. |
| `4` (`LISTEN_FATAL_EXIT`) | **Impaired** — can't establish/keep a live session (bad token, missing privileged intent, or link down past the listener's grace window). | Log `listen_impaired`, alert out-of-band (throttled to one per `LISTEN_ALERT_COOLDOWN_S`), **relaunch**. |
| other non-zero | Crashed/exited unexpectedly. | Log `listen_exit`, relaunch with backoff; alert after repeated non-healthy restarts. |

Two hard rules for a listener:

- **On SIGTERM, exit `0`** — a deliberate stop is not an impairment.
- **Fail loud, never go quiet.** If you can't *establish* a session, or you were connected and the
  link stays down past a grace window your library reconnects through forever, **exit non-zero**
  (use `4` for "impaired") rather than sitting alive-but-silent. The gateway counts a live process
  as healthy; a silently-dead listener would strand inbound with no signal. Exiting is how a dead
  link becomes loud — the gateway relaunches you.

An impaired listener is **relaunched and alerted**, never abandoned — `listen` is the only way in,
so the gateway keeps bringing it back rather than giving up.

---

## 7. The failure contract (applies to every verb)

- **Fail loud.** After your own retries (e.g. honoring a rate-limit `retry_after`), any
  unrecoverable error exits **non-zero with a message on stderr**. The gateway logs it; sustained
  failure escalates out-of-band. Never exit 0 on a broken operation — a phantom success relocates
  the failure downstream to a user wondering why Claude went silent.
- **Don't swallow.** A 2xx with a missing id, an unparseable payload, an empty allowlist — surface
  it, don't paper over it.
- **Idempotency where it matters.** Emit each message once — a listener that reconnects/RESUMEs
  must not re-emit messages it already delivered. Emit stable `id`s so the gateway and Claude can
  refer to a message unambiguously.

---

## 8. Checklist for a new adapter

1. `mkdir plugins/<name>/`, write `plugin.json` (`kind:"messaging"`, `transport:"${PLUGIN_DIR}/<script>"`,
   honest `capabilities`).
2. Implement `send` (prints id) + `edit` + `react`/`unreact`.
3. Implement inbound: `listen`. Emit the [§3](#3-the-inbound-line-shape) line shape.
4. **Gate before emitting** — drop bots, enforce a fail-closed allowlist. Put gate+shape in one place.
5. Make every verb **fail loud**; bound your synchronous calls under `TRANSPORT_TIMEOUT_S`.
6. If `listen`: honor the [exit-code contract](#6-the-listen-lifecycle--exit-codes) — exit 0 on SIGTERM, non-zero (4) on impairment.
7. Optionally `fetch` for read-back, and `latest` for the newest id.
8. Select it: set `CHANNEL=<name>` in `config.env` (only needed if more than one messaging plugin is installed).

If you implement these verbs and the line shape, the gateway drives your service end-to-end with
no changes to `bridge/`.
