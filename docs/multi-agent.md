# Multi-agent control at the gateway

Status: **in-build** (full-stack arc, started 2026-06-25). This is the durable
plan — a recycle mid-build resumes from here. Cross-check against `state.md` for
the live cursor.

## Goal

Promote clidecar from a fixed **1:1:1** topology (one Discord channel → one
gateway daemon → one attached Claude, newest-wins, zero identity) to a **fleet**:
N managed Claude agents, each bound to its own Discord channel, all routed by one
persistent gateway daemon, plus a deterministic **control channel** for fleet
management — spawned/recycled/routed from Discord (phone-reachable), no LLM in the
lifecycle path.

Design decisions (2026-06-25):
1. **Control plane** = a dedicated control channel, parsed **deterministically by
   the gateway** (not an orchestrator Claude). Matches clidecar's "deterministic
   gateway, dumb adapter" law and keeps process lifecycle off an LLM.
2. **Routing** = **one channel per agent** (+ one control channel).
3. **Scope** = full stack in one arc.

## The load-bearing insight

`chat_id` **is** the Discord channel id, and it already flows end-to-end:
`gate.shape` emits `msg.channel_id` as `chat_id` → `Inbound` → the
`<channel chat_id=…>` envelope → the broker's `chat_id`-keyed outbound. The only reason it
isn't already a router is that `client.py` **ignores it on outbound** (always
sends to the one hardwired `self._channel_id`) and `on_message` hard-drops
everything not in that one channel. So "channel → agent" is the natural routing
dimension; the data path barely changes — we make `chat_id` a first-class router
on both directions.

## Target topology

- **One** clidecar supervisor (systemd `--user`, `bin/sidecar.sh`).
- **One** persistent gateway daemon (`bridge/gateway.py`) — owns the single
  Discord connection + broker socket, survives recycles, now routes across many
  channels to many agents and intercepts the control channel.
- **N** managed Claude sessions, each with:
  - its own screen `clidecar-<id>`,
  - its own PID `~/.clidecar/control/agents/<id>/claude.pid`,
  - its own workdir / args,
  - its own per-agent control flag `~/.clidecar/control/agents/<id>/RECYCLE`,
  - attaches to the **one** gateway socket via the shim, announcing its agent id.
- **One** Discord bot connection, listening on the fleet's channel set; each agent
  bound to exactly one channel; plus one control channel.

## Desired-state: the fleet manifest

`~/.clidecar/fleet.db` — a SQLite store, the single source of truth.
The control language mutates it; the supervisor reconciles processes to it; the
gateway derives the channel→agent routing table + the client's channel set from it.

```json
{
  "control_channel": "…",
  "agents": {
    "clidecar":  { "channel": "…", "workdir": "~/clidecar",   "args": "--permission-mode auto", "enabled": true },
    "assistant": { "channel": "…", "workdir": "~/assistant",  "args": "--permission-mode auto", "enabled": true }
  }
}
```

Invariants (validated at load, **fail loud** on violation — never silently
reconcile to a degraded fleet):
- every `channel` is unique across agents, and distinct from `control_channel`;
- agent ids match `[a-z0-9][a-z0-9_-]*`;
- a parse error / missing-required-field keeps the **last-known-good** fleet and
  alerts the control channel — a malformed manifest must NEVER be read as "zero
  agents" (that would reconcile the whole fleet to death).

Helper `bin/_fleetctl.py` owns all read/mutate (atomic write, like `clidecar set`):
- `list [--enabled]` → agent ids (one per line)
- `get <id> <field>` → field value (for the bash supervisor)
- `routes` → `chat_id<TAB>agent_id` lines + a `control<TAB><cid>` line (gateway)
- `add/set/remove/enable/disable/route` → mutate (used by the control parser)
- `validate` → exit nonzero + reason on any invariant break

## Component changes

### Shim — `bridge/gateway-shim.py` (identity)
Read `CLIDECAR_AGENT_ID` from env; announce `{"role":"channel","agent":"<id>"}`.
Missing env → **exit 1 loud** (never attach anonymously). The supervisor sets the
env when launching each agent's Claude.

### Broker — `bridge/exchange.py` (multi-sink + chat_id outbound)
- `self._channel: socket|None` → `self._channels: dict[str, socket]` (agent_id →
  conn), `_chan_lock`-guarded. `_serve_channel` reads `agent` from the handshake
  (reject loudly if absent/non-string), registers newest-wins **per agent**
  (close a superseded conn for the same id), deregisters on disconnect iff still
  current.
- New `self._routes: dict[str,str]` (chat_id → agent_id) + `control_channel`, set
  by the gateway via `set_routes(...)`, refreshed live on manifest change.
- `route_inbound` step 2: `agent = self._routes.get(msg.chat_id)`; push to
  `self._channels[agent]` if attached → `claude:<agent>`; bound-but-detached → ❌
  `undelivered`; unrouted known channel → ❌ + log; control channel never reaches
  here (intercepted upstream). Claims (step 1) stay chat_id-matched — with one
  channel per agent, chat_id already scopes a claim to its agent.
- **Outbound gains chat_id.** `Outbound` gets a `chat_id` field; `emit/edit/react/
  latest` take `chat_id` and thread it through `_op` → transport →
  `client.dispatch(verb, chat_id, *args)`. Dedup key becomes `(chat_id, kind,
  dedup_key)` so two agents' identical text don't cross-dedup.

### Discord client — `plugins/discord/client.py` (multi-channel)
- Constructed with a **channel set** (gateway-supplied from the fleet); `set_channels(ids)`
  updates it live. `on_message` admits any message whose channel ∈ set (still
  sender-gated by `gate.py`, fail-closed); emits chat_id = channel id (already does).
  Adapter stays Claude-unaware and fleet-unaware — it's handed a set of ids; all
  routing policy lives in the gateway.
- Outbound `send/edit/react/latest/fetch(chat_id, …)` resolve `int(chat_id)` →
  `get_channel` and operate there. Unknown/missing chat_id → **raise** (fail loud),
  not fall back to a default. `DISCORD_CHANNEL_ID` in `.env` becomes legacy (only
  seeds the migration default).

### Gateway daemon — `bridge/gateway.py` (routes + control interception)
- At start: load fleet via `_fleetctl routes`, `broker.set_routes(...)`, hand the
  channel set to the client. Watch the `fleet.db` store (mtime poll) → refresh
  routes + client channel set **live** (routing changes need no recycle).
- **Control interception** in `_route_line`: if `msg.chat_id == control_channel`,
  hand to the control parser instead of `route_inbound`.
- `call_tool` threads chat_id into `_outbound` (it currently drops it).

### Control language (Haiku-translated, deterministically executed, owner-gated)
Designated control channel; commands gated to the **control owner** user_id
(`controlOwner` in `~/.claude/channels/discord/access.json` — separate from
`allowFrom`; allowlist mutations stay terminal-only and are NOT reachable from the
control language). An owner message is first handed to an **isolated Haiku
translator** (`bridge/translate.py`, an `claude -p` one-shot run sandboxed away
from the bridge's own hooks/MCP/CLAUDE.md, under the existing `CLAUDE_BIN` auth —
no API key) that returns two things: an **optional canonical command** and a
**reply** to the owner. The command — if any — is then run through the SAME
deterministic word grammar below: **Haiku only proposes; nothing runs without
passing the deterministic gate**, and if the translator is unreachable or returns
non-contract output the raw text is parsed deterministically (loud `⚠️` notice), so
exact commands never depend on Haiku. The canonical grammar (`control.GRAMMAR`, the
single source the translator is shown and `help` advertises) — plain words,
case-insensitive, no prefix (a leading `!` is tolerated but never required);
`help` (or `?`) prints the list:
- `agents` — list id · channel · workdir · enabled · alive?
- `spawn <id> channel=<cid> workdir=<path> [args=…]` — add (enabled) → supervisor launches
- `recycle <id>` — drop the per-agent RECYCLE flag
- `stop <id>` / `start <id>` — toggle enabled → supervisor stops/launches
- `remove <id>` — stop + forget
- `route <id> channel=<cid>` — rebind
- `set <id> workdir=<path>|args=…` — mutate (next relaunch)
- `status` — gateway + fleet health
- `help` — print the available commands
Per message the owner may see two posts: Haiku's `reply` (intent) and the
deterministic executor's authoritative confirmation (ground truth — it catches the
validation failures Haiku's optimistic reply would mask). Executor replies are
**gateway-authored** (deterministic, like the ❌ react); every refusal / validation
failure is loud (❌ + one-line reason).

### Supervisor — `bin/sidecar.sh` (reconcile loop)
Replace single `launch_claude`/`PIDFILE`/`SCREEN_NAME` assumptions with per-agent:
- `launch_agent <id>` — screen `clidecar-<id>`, PID `…/agents/<id>/claude.pid`,
  workdir/args from manifest, exports `CLIDECAR_AGENT_ID` + `CLIDECAR_AGENT_CHANNEL`;
  per-agent dev-channel auto-confirm targets `clidecar-<id>`.
- `agent_alive <id>` — per-agent PID + `/proc` cmdline guard.
- Reconcile each tick (inotify on control dir + the fleet store + POLL backstop):
  enabled-but-dead → guard+launch; running-but-not-enabled → stop; per-agent
  RECYCLE flag → graceful-kill + relaunch. Per-agent crash-loop guard. Gateway
  daemon kept alive unchanged (shared).
- **Fail-closed on manifest parse error**: keep running agents, alert; never
  reconcile against an empty/garbage fleet.

### Hooks — `bridge/_hooklib.py` + `hook-*.py`
Already parse chat_id from the envelope into `TurnState`. Thread it through
`channel_send/edit/react/latest` → broker (now chat_id-aware). Each agent's hooks
run in that agent's process with that agent's chat_id, so they route correctly.
Hook scripts are live-on-save (no recycle).

## Migration / backward-compat
If the `fleet.db` store is missing, seed agent `main` from the legacy single-agent config
(`WORKDIR`, `CLAUDE_ARGS`, `DISCORD_CHANNEL_ID`); `control_channel` from a new
`CONTROL_CHANNEL` config value (control disabled until set). Fail loud if neither
a manifest nor legacy single-agent config exists.

## Reload matrix (what costs what)
- **Fleet/routing change** (spawn, bind, enable): gateway re-reads → routes +
  client channels update **live**; supervisor launches the new agent **live**. No
  global recycle.
- **Broker/exchange/gateway/client CODE change**: `clidecar gateway reload`
  (recycles all agents — checkpoint first).
- **`sidecar.sh` change**: `clidecar reload`.
- **Hook script change**: live-on-save.

## Failure-loud checklist
- Unrouted / bound-but-detached inbound → ❌ + log (never silent).
- Shim without `CLIDECAR_AGENT_ID` → exit 1.
- Channel-role attach without agent id → reject loudly.
- Duplicate channel binding / control-channel collision → reject at load + alert.
- Control command from non-owner or wrong channel → ❌ + reason.
- Per-agent crash-loop → per-agent guard, alert control channel, others survive.
- Outbound to unknown chat_id → client raises (no silent default).
- Manifest parse error → fail-closed (last-known-good), never reconcile to empty.

## Build order
1. Plan doc (this file). ✅
2. `fleet.db` SQLite store + `_fleetctl.py` (read/mutate/validate).
3. Broker multi-sink + chat_id outbound (`exchange.py`).
4. Shim identity (`gateway-shim.py`).
5. Client multi-channel + chat_id outbound (`client.py`).
6. Gateway routes + control interception + control parser (`gateway.py`, new
   `control.py`).
7. Hooks chat_id threading (`_hooklib.py`).
8. Supervisor reconcile loop (`sidecar.sh`) + `clidecar agent …` CLI verbs.
9. Migration seed + `config.env`/README/CLAUDE.md docs.
10. pyright strict 0 + ruff + ephemeral tests green; reviewers; then reload arc.
```
