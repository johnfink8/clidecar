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
- **Hook/plugin script edits are LIVE immediately; only `settings.json` registration
  needs a recycle.** Claude Code re-executes a command-hook's script file on every
  invocation, so editing a plugin's `*.py`/`*.sh` takes effect mid-session. But *which*
  hooks are registered (`.claude/settings.json`) loads once at launch — so
  `clidecar plugin enable/disable` (and moving hook files) needs a `clidecar recycle`
  to take effect. (Distinct from `bin/sidecar.sh`, which needs `clidecar reload`.)
- **Gateway daemon CORE edits (`bridge/gateway.py`, `exchange.py`) need
  `clidecar gateway reload`** — the running daemon parsed them at start. The hook
  scripts that talk to it are still live-on-save (above); only the daemon process
  itself must be restarted. ⚠️ KNOWN GAP: `gateway reload` restarts the daemon with a
  fresh broker socket, but the live session's shim stays attached to the OLD socket
  and does NOT auto-reconnect — so a reload strands inbound until the next recycle
  reattaches a fresh shim. Reload the daemon, then recycle (or schedule it).
- **The bridge is a uv project** (`pyproject.toml` + `uv.lock`, `.venv` via `uv sync`).
  Run tooling through it: `uv run ruff format` / `uv run ruff check` / `uv run pyright`
  (strict, 0 errors expected). The gateway daemon launches under the venv interpreter
  via `PYTHON_BIN` in `config.env` (live config) so its deps (croniter, holidays)
  resolve; the hooks + shim stay on portable `#!/usr/bin/env python3` (no venv-only
  imports, and the repo is public — no hardcoded venv paths in committed files).
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
    recycles): owns the messaging-channel connection (WS push via the adapter's `listen`,
    REST `poll` fallback) + inbound routing + the single outbound funnel. `exchange.py` =
    the unix-socket Broker (routes each inbound to EXACTLY ONE sink — open claim → attached
    Claude → else ❌; emit/edit/react/latest with retries + dedup; `emit` returns the msg id)
    plus its client helpers + `ask()` (cross-process request→reply). `gateway-shim.py` = the
    disposable per-launch MCP-stdio shim Claude Code spawns to attach to the daemon socket.
  - **Output + question hooks** — `hook-{ack,progress,final}.py` = UserPromptSubmit /
    PostToolUse+MessageDisplay / Stop (👀 ack, live status mirror, deterministic Stop-hook
    final answer); `hook-question.py` = the AskUserQuestion PreToolUse hook (renders options
    to the channel + BLOCKS for the answer via `exchange.ask`, degrades to deny-render).
    `_hooklib.py` = shared per-turn state + rendering + the `channel_*` broker clients;
    `transcript.py` = transcript-JSONL parsing; `channel.py` = resolves the active adapter →
    transport + capabilities. Registered directly in `.claude/settings.json` (core, not a
    toggle-able plugin). STRICT LANES: hooks talk only to the gateway, never the adapter.
- `plugins/<name>/` — messaging-channel ADAPTERS: a dumb transport that knows nothing about
  Claude. A `plugin.json` manifest (`{kind:"messaging", transport, capabilities}`) + a
  transport script. `plugins/discord/`: `msg.sh` (send/edit/react/latest/fetch via the bot
  API) + `listen.py` (hand-rolled Discord Gateway WS streamer = inbound push) + `poll.py`
  (REST poll fallback) + `gate.py` (shared inbound gate+shape: drop bots + non-allowlisted,
  fail-closed) + `history.py` (channel read-back). The gateway picks the adapter at runtime —
  `config.env` `CHANNEL`, else the sole installed messaging plugin — and degrades around any
  capability it doesn't declare.
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
