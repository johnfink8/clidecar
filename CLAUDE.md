# clidecar — project guide

Self-recycling Claude harness: a systemd --user service (`bin/sidecar.sh`)
supervises two long-lived children — a `claude` session (in `screen`) and a
persistent gateway daemon that owns the messaging-channel connection. The session
can reset its own context, relaunch in a new workdir/params, and hot-swap its own
supervisor; the daemon survives recycles so inbound isn't missed. User-facing
overview + design in [README.md](README.md).

## Working on this repo

- **Editing `bin/sidecar.sh` does NOT take effect until `clidecar reload`.** The
  running sidecar parses its bash functions once at startup; the main loop never
  re-reads the file. `reload` syntax-checks (`bash -n`), copies to `known-good/`,
  and `systemctl --user restart`s the service while ADOPTING the live Claude
  untouched (`KillMode=process`). This bit us on the first dogfood recycle.
- **`config.env` changes ARE picked up live** — `launch_claude` re-sources it on
  every (re)launch. Only sidecar *code* changes need a reload.
- **Hook script edits are LIVE immediately; only `settings.json` registration
  needs a recycle.** Claude Code re-executes a command-hook's script file (`bridge/hook-*.py`)
  on every invocation, so editing one takes effect mid-session. But *which* hooks are registered
  (`.claude/settings.json`) loads once at launch — so `clidecar plugin enable/disable` (and moving
  hook files) needs a `clidecar recycle` to take effect. (Distinct from `bin/sidecar.sh`, which
  needs `clidecar reload`.) NOTE: the messaging ADAPTER is no longer live-on-save — it's now an
  in-process client imported INTO the daemon (next bullet), so adapter edits need a `gateway reload`.
- **Gateway daemon CORE edits (`bridge/gateway.py`, `exchange.py`, `channel.py`, and the active
  adapter client `plugins/<name>/client.py` + what it imports — `gate.py`/`history.py`/`_message.py`)
  need `clidecar gateway reload`** — the running daemon imported them at start. The hook scripts that
  talk to it are still live-on-save (above); only the daemon process itself must be restarted. The new daemon serves a fresh broker socket and the live
  session's shim can't reconnect a stdio server mid-session, so `gateway reload` now
  ALSO recycles Claude (supervisor-side) so a fresh shim attaches to the new socket —
  but only AFTER polling that the new daemon actually came up. If it didn't (e.g. the
  core edit you just made won't boot), the supervisor SKIPS the recycle, keeps the live
  session, and pings Discord — better a preserved session than a context reset burned
  into a dead socket. ⚠️ A successful reload costs a context reset: checkpoint to
  state.md + memory FIRST, same as `recycle`.
- **The bridge is a uv project** (`pyproject.toml` + `uv.lock`, `.venv` via `uv sync`).
  Run tooling through it: `uv run ruff format` / `uv run ruff check` / `uv run pyright`
  (strict, 0 errors expected). The gateway daemon launches under the venv interpreter
  via `PYTHON_BIN` in `config.env` (live config) so its deps (croniter, holidays)
  resolve; the hooks + shim stay on portable `#!/usr/bin/env python3` (no venv-only
  imports, and the repo is public — no hardcoded venv paths in committed files).
  A tracked `.githooks/pre-commit` runs all three (ruff format `--check`, ruff check,
  pyright) and blocks a dirty commit — activate it per-clone with
  `git config core.hooksPath .githooks`.
- **Validate bash before it goes live**: `bash -n bin/sidecar.sh`. A sidecar that
  crashes on start trips systemd StartLimit → `OnFailure` restores `known-good/`.
- **Test recycles cost a context reset** — checkpoint to `~/.clidecar/state/state.md`
  (and durable facts to memory) BEFORE touching `~/.clidecar/control/RECYCLE`,
  or the fresh instance wakes amnesiac.
- **Code lives in the repo; all runtime/personal data lives in `~/.clidecar`** —
  config.env, state/, control/, known-good/. The repo ships only `*.example`
  templates. Don't write live state into the repo tree.

## Architecture map

- `bin/sidecar.sh` — supervisor loop: keeps TWO children alive (the managed `claude`
  in screen + the gateway daemon) → wait-for-event → recycle/relaunch; relaunches the
  daemon if it dies, without recycling Claude.
- `bin/clidecar` — control CLI (recycle / set / reload / status / logs / down / up /
  plugin / gateway). Symlinked onto PATH; resolves its own symlink to find the repo root.
- `bin/notify-discord.sh` — bot-API ping for code paths that can't reach the gateway.
- `bin/fallback.sh` — OnFailure one-shot: restore known-good sidecar, notify.
- `bin/_pluginctl.py` — enable/disable/list hook plugins by editing `.claude/settings.json`.
- `bridge/` — the channel-agnostic, Claude-aware bridge core. Typed under **pyright
  strict** (config in `pyproject.toml`); every JSON boundary validated via each
  dataclass's `from_obj` builder. Two halves:
  - **Gateway daemon** — `gateway.py` is the persistent process (supervised, survives
    recycles): imports the active adapter's in-process client (via `channel.client_entrypoint()`)
    and drives it for inbound + outbound. `_transport` bridges the daemon's sync threads to the
    client's asyncio loop (`run_coroutine_threadsafe`); inbound flows back through a non-blocking
    `on_inbound` that hands each line to a worker pool. **No-deadlock rule: `route_inbound` must
    never run on the client's loop thread** (it re-enters outbound for the ❌ no-Claude react).
    `exchange.py` = the unix-socket Broker (routes each inbound to EXACTLY ONE sink — open claim →
    attached Claude → else ❌; emit/edit/react/latest with retries + dedup; `emit` returns the msg
    id) plus its client helpers + `ask()` (cross-process request→reply) — transport-agnostic, takes
    `_transport` by injection, unchanged by the client swap. `gateway-shim.py` = the disposable
    per-launch MCP-stdio shim Claude Code spawns to attach to the daemon socket.
  - **Output + question hooks** — `hook-{ack,progress,final}.py` = UserPromptSubmit /
    PostToolUse+MessageDisplay / Stop (👀 ack, live status mirror, deterministic Stop-hook
    final answer); `hook-question.py` = the AskUserQuestion PreToolUse hook (renders options
    to the channel + BLOCKS for the answer via `exchange.ask`, degrades to deny-render).
    `_hooklib.py` = shared per-turn state + rendering + the `channel_*` broker clients;
    `transcript.py` = transcript-JSONL parsing; `channel.py` = resolves the active adapter →
    client entrypoint + capabilities, and declares the `ChannelClient` Protocol. Registered directly
    in `.claude/settings.json` (core, not a toggle-able plugin). STRICT LANES: hooks talk only to
    the gateway, never the adapter.
- `plugins/<name>/` — messaging-channel ADAPTERS: an in-process client that owns the provider
  connection (inbound + outbound) and knows nothing about Claude. A `plugin.json` manifest
  (`{kind:"messaging", client:"module:Class", capabilities}`) + a Python module exposing a class
  satisfying `channel.ChannelClient` (`loop`/`start`/`dispatch(verb,*args)->(code,stdout)`/`shutdown`/
  `fatal`). `plugins/discord/`: `client.py` (one persistent discord.py client — `on_message` inbound
  + `dispatch` send/edit/react/latest/fetch; discord.py owns reconnect + 429 backoff) + `gate.py`
  (inbound gate+shape: drop bots + non-allowlisted, fail-closed) + `history.py` (fetch read-back
  render) + `_message.py` (pydantic wire shape). The daemon imports the client under its venv
  (`PYTHON_BIN`), so the adapter may use venv deps; it picks the adapter at runtime — `config.env`
  `CHANNEL`, else the sole installed messaging plugin — and degrades around any capability it
  doesn't declare. **Writing a new adapter: [plugins/README.md](plugins/README.md)** — the full
  manifest + ChannelClient + inbound-line-shape contract, no reverse-engineering needed.
- `systemd/` — the supervisor unit + the OnFailure unit.
- `pyproject.toml` — uv project: runtime deps (croniter, holidays) + dev tooling (ruff,
  pyright) + their config. `uv sync` builds `.venv`; the daemon runs under it via `PYTHON_BIN`.
- `docs/` — design specs (e.g. `scheduler.md`, the heartbeat scheduler).
- `~/.clidecar/control/` — runtime flags (`RECYCLE`) + `claude.pid` + `gateway.pid` + the
  gateway broker socket.
- `~/.clidecar/state/` — live `state.md` (the "now") + `queue.md` (backlog) + gateway logs/events.
  The repo ships `state/*.md.example` templates only.

## Conventions

- Bash: `set -uo pipefail`; best-effort side calls guarded with `|| true`.
- Notifier is best-effort and never fails its caller (exit 0).
- Python: pyright strict (0 errors); `ruff` owns formatting and line length is
  deferred to the formatter (lint `ignore = ["E501"]`). Run via `uv run`.
- Commit style: terse, lowercase, no AI co-author trailer.
