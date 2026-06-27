# clidecar — a self-recycling Claude harness

A supervised **fleet** of long-running [Claude Code](https://claude.com/claude-code)
agents — each able to reset its own context, relaunch itself in a different working
directory or with different parameters, and even hot-swap its own supervisor, all
from inside the session, no SSH-in required. A small `systemd --user` service keeps
the whole fleet alive, fresh, and reboot-proof, and you manage it from a chat channel
(Discord) — spawn, recycle, and re-point agents from your phone.

## Why

The goal is **durable, always-on agents across many projects** — each pinned to its
own project and its own chat channel, all managed from one place — not a pile of
throwaway chat windows you babysit.

A plain Claude session doesn't get you there: its context fills up and goes stale,
it's pinned to the directory you launched it in, and it dies on reboot or logout. So
you end up restarting it, re-orienting it, and opening a new window per project.

clidecar turns each agent into a durable one, and the set of them into a fleet you
control:

- **Stays fresh** — each agent checkpoints its working state and *recycles* its own
  context on demand, then a fresh instance reads the checkpoint and continues.
- **One agent per project** — every agent has its own working directory, its own
  launch args, and its own chat channel; add, remove, or re-point them without
  touching the supervisor.
- **Managed from anywhere** — an owner-gated **control channel** spawns / recycles /
  re-routes agents from Discord (so, from your phone), with **no LLM in the lifecycle
  path** — the control plane is parsed deterministically.
- **Survives reboots** — supervised by `systemd --user` with linger, so the whole
  fleet comes back with no cron and no sudo.
- **Improves itself in place** — an agent can edit the supervisor script, validate
  it, and reload — adopting the running fleet untouched.
- **Triages inbound events** — wired to a chat channel, an agent can be woken by
  alerts and work them human-in-the-loop.

## How it works

- **Supervisor:** `systemd --user` runs `bin/sidecar.sh`. With linger enabled it
  survives reboot/logout with no sudo. `Restart=always` + `StartLimit*` give crash
  recovery and backoff; `OnFailure` restores a known-good sidecar. It keeps two kinds
  of long-lived child alive: the gateway daemon and the fleet of agents.
- **The fleet** — N managed Claude agents. Each runs in its own detached `screen`
  (`clidecar-<id>`; `screen -r clidecar-main` to watch one), with its own workdir,
  launch args, and bound chat channel. The desired set of agents lives in the
  `~/.clidecar/fleet.db` SQLite store; the supervisor **reconciles** running processes
  to it — launching an enabled agent that's down, stopping one that's been removed. A
  missing or unreadable store is never read as "zero agents": it fails closed and
  keeps the live fleet.
- **The gateway daemon** is the other long-lived child: one persistent process that
  owns the single messaging-channel connection and routes each inbound message to the
  agent bound to that channel. Because it's separate from the sessions, the connection
  survives a recycle — messages aren't missed while an agent restarts. Its core
  reloads via `clidecar gateway reload`.
- **The control channel** — one owner-gated channel the gateway parses
  **deterministically** (no LLM in the lifecycle path) to manage the fleet: `spawn` /
  `recycle` / `stop` / `route` / `set` / … An optional Haiku front-end lets you phrase
  commands in natural language, but it only *proposes* — every command still passes
  the deterministic gate, and exact commands work even if it's unreachable. Design:
  [docs/multi-agent.md](docs/multi-agent.md).
- **Recycle (context reset):** an agent checkpoints to `state.md` + its memory store,
  then runs `clidecar recycle` (agent-aware — just itself). The sidecar SIGTERMs it
  (10s grace) and launches a fresh instance, which re-reads config, `state.md`, and
  recalls from memory.
- **Change workdir/params:** `clidecar set WORKDIR /path` (or edit `config.env`) then
  recycle; or `clidecar agent set <id> …` for a fleet member. The next launch reads
  the new config.
- **Self-modify the sidecar:** edit `bin/sidecar.sh`, then `clidecar reload`
  (syntax-checks, copies to known-good, `systemctl --user restart`). The running
  agents are adopted, not disturbed (`KillMode=process`).

## Install

Requires: `claude` (Claude Code), `bash` 5+, `screen`, `curl`, `python3`,
[`uv`](https://docs.astral.sh/uv/) (the bridge/gateway run in a uv-managed venv),
`systemd --user`. Optional: `inotify-tools` (instant flag response; falls back to
polling). The systemd units assume the repo lives at `~/clidecar`.

```sh
# 1. clone to ~/clidecar and build the bridge/gateway venv
git clone <repo-url> ~/clidecar
cd ~/clidecar && uv sync

# 2. put the control CLI on your PATH
ln -sf ~/clidecar/bin/clidecar ~/.local/bin/clidecar

# 3. create your data dir (config + state) from the templates
#    (the sidecar also creates ~/.clidecar on start)
mkdir -p ~/.clidecar/state
cp ~/clidecar/config.env.example     ~/.clidecar/config.env
cp ~/clidecar/state/state.md.example ~/.clidecar/state/state.md
cp ~/clidecar/state/queue.md.example ~/.clidecar/state/queue.md
# edit ~/.clidecar/config.env: set WORKDIR, CLAUDE_BIN, and add the optional
# Discord/remote-control args to CLAUDE_ARGS if you want them

# 4. (optional) Discord. Bot token -> ~/.claude/channels/discord/.env as
#    DISCORD_BOT_TOKEN=...; the channel-agnostic bridge auto-selects plugins/discord
#    as the sole messaging adapter (no enable step — the bridge is core, not a toggle
#    plugin).
#    OUTBOUND: the bridge hooks are registered in .claude/settings.json with command
#    paths under $CLAUDE_PROJECT_DIR/bridge, so they resolve to your checkout with no
#    per-machine path edits.
#    INBOUND: set GATEWAY_DAEMON=1 in config.env, register bridge/gateway-shim.py as the
#    `clidecar` MCP server in ~/.claude.json, and add the channel arg to CLAUDE_ARGS (see
#    config.env.example) so the agent attaches to the gateway. The supervisor keeps the
#    daemon alive across recycles, so inbound isn't missed while a session restarts.
#    FLEET: the fleet.db store seeds on first run from DISCORD_CHANNEL_ID as a single
#    agent "main". To manage a fleet from chat, set CONTROL_CHANNEL (a dedicated channel)
#    and CONTROL_OWNER (your Discord user_id — gates who may run control commands; empty
#    = fleet control disabled). Then add agents with `clidecar agent spawn <id>` or with
#    natural language on the control channel.

# 5. install the systemd --user units
mkdir -p ~/.config/systemd/user
ln -sf ~/clidecar/systemd/clidecar.service          ~/.config/systemd/user/
ln -sf ~/clidecar/systemd/clidecar-fallback.service ~/.config/systemd/user/
systemctl --user daemon-reload

# 6. survive reboot/logout (no sudo needed for the rest, but this one helps)
loginctl enable-linger "$USER"

# 7. start it
systemctl --user enable --now clidecar
```

`clidecar status` / `clidecar logs` to check on it; `clidecar agent list` to see the
fleet; `screen -r clidecar-<id>` to attach to a live agent (e.g. `clidecar-main`).

## Memory model

Three layers, deliberately separate:

- **~/.claude/CLAUDE.md** — always-loaded rules / recycle protocol.
- **a durable memory store** — pull-based history, retrieved by semantic recall.
  clidecar is built around [quorelo](https://quorelo.com), the per-user memory MCP
  server (`recall` / `remember` / `update_memory`): an agent checkpoints durable facts
  there and recalls them by circumstance after a recycle, instead of carrying a giant
  context file. Any MCP memory server with recall/store semantics works, but quorelo
  is the recommended pairing. Because I built it.
- **state.md / queue.md** — the live now + backlog (churn every recycle).

## Channel bridge

clidecar interposes its own channel-agnostic gateway between the agents and the
messaging app, owning **both** directions — so inbound arrives with its provenance
intact and an answer is never lost to a console you aren't watching.

- **Inbound** — a persistent **gateway daemon** (supervised alongside the agents, in
  `bridge/`) owns the messaging-app connection and presents itself to each agent as a
  *channel*. It routes each incoming message to exactly one sink: the owner-gated
  control channel, or the agent bound to that channel. It survives recycles, so
  messages aren't missed while a session restarts; each agent attaches through a thin,
  disposable stdio shim that announces its agent id.
- **Outbound** — Claude Code hooks (the only Claude-aware code, registered in
  `.claude/settings.json`) react 👀 to your message, keep a live status message
  mirroring the turn's narration and tool calls (re-homed below anything you send
  mid-turn), and a `Stop` hook deterministically posts the turn's closing answer every
  turn. Delivery never depends on the model *choosing* to send — there is no reply
  tool; the agent's normal output **is** what reaches the channel. Typed under pyright
  strict, every JSON boundary validated.

**Strict lanes:** the hooks and the daemon talk only to the gateway; only the channel
*adapter* talks to the messaging app. An adapter under `plugins/<name>/` is a transport
that knows nothing about Claude — a `plugin.json` manifest declaring `kind: "messaging"`,
a `client` entrypoint (`module:Class`), and its `capabilities`, plus a Python client
class the daemon imports and drives (one persistent connection for both inbound and
outbound). The gateway resolves the active adapter at runtime (`CHANNEL` in `config.env`,
else the sole installed messaging plugin) and degrades around any capability the channel
doesn't declare. `plugins/discord/` is the bundled adapter (a discord.py client). Writing
your own: [plugins/README.md](plugins/README.md).

Editing a hook script takes effect immediately; changing the gateway daemon core —
including the in-process adapter client — needs `clidecar gateway reload`, and changing
which hooks are registered in `.claude/settings.json` needs a recycle.

## Remote control (optional)

You can also drive a managed agent from claude.ai or the Claude mobile app with Claude
Code's [Remote Control](https://code.claude.com/docs/en/remote-control): add
`--remote-control "clidecar"` to `CLAUDE_ARGS` in `config.env`, then recycle. Requires
a claude.ai subscription and a logged-in session. Handy here because a managed agent has
no attached terminal of its own — this gives you a way in from anywhere without
`screen -r`.

## Limitations & security model

- **The auto-mode permission classifier will block some actions, by design — and we
  keep it that way.** With `--permission-mode auto`, Claude Code runs a classifier that
  denies actions it judges risky given their *provenance*. In particular, requests that
  arrive over an inbound channel (e.g. Discord) are treated as untrusted — the
  classifier can't distinguish a legitimate request from a prompt-injection wearing the
  same words — so it refuses security-sensitive operations (changing trust/permission
  state, etc.) even with a matching allow-rule. This is a feature, not a bug: it's what
  makes it safe to expose an autonomous agent to an inbound channel. **Do not disable
  it**, and we recommend others keep it on too. The escape hatch, when you genuinely
  need such an action, is to run that one command yourself from the terminal — your own
  invocation isn't subject to the classifier.
- **Fleet control is owner-gated and deterministic.** Lifecycle commands
  (spawn / recycle / stop / route / …) are accepted only on the control channel, only
  from the configured `CONTROL_OWNER` user id (set from the terminal, never mutable from
  chat), and are parsed deterministically — the optional Haiku translator only proposes,
  it never decides. So an inbound prompt-injection can't spawn, stop, or re-point agents.
- **Linux + systemd --user only.** No macOS/launchd or Windows support.
- **Single host.** The supervisor, gateway daemon, and every agent run on one machine;
  there's no multi-host distribution.

## License

[MIT](LICENSE).
