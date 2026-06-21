# clidecar — project guide

Self-recycling Claude harness: a long-running `claude` session supervised by a
systemd --user service (`bin/sidecar.sh`) running inside a `screen` session. It
can reset its own context, relaunch in a new workdir/params, and hot-swap its
own supervisor. Full design in [SPEC.md](SPEC.md); user-facing overview in
[README.md](README.md).

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
- **Validate bash before it goes live**: `bash -n bin/sidecar.sh`. A sidecar that
  crashes on start trips systemd StartLimit → `OnFailure` restores `known-good/`.
- **Test recycles cost a context reset** — checkpoint to `~/.clidecar/state/state.md`
  (and durable facts to memory) BEFORE touching `~/.clidecar/control/RECYCLE`,
  or the fresh instance wakes amnesiac.
- **Code lives in the repo; all runtime/personal data lives in `~/.clidecar`** —
  config.env, state/, control/, known-good/. The repo ships only `*.example`
  templates. Don't write live state into the repo tree.

## Architecture map

- `bin/sidecar.sh` — supervisor loop: launch → wait-for-event → recycle/relaunch.
- `bin/clidecar` — control CLI (recycle / set / reload / status / logs / down / up /
  plugin). Symlinked onto PATH; resolves its own symlink to find the repo root.
- `bin/notify-discord.sh` — bot-API ping for code paths that can't reach the MCP.
- `bin/fallback.sh` — OnFailure one-shot: restore known-good sidecar, notify.
- `bin/_pluginctl.py` — enable/disable/list plugins by editing `.claude/settings.json`.
- `plugins/<name>/` — optional hook plugins, each self-contained with a `plugin.json`
  (a Claude Code hooks fragment using a `${PLUGIN_DIR}` placeholder). `discord-bridge`
  is the Discord output bridge (👀 ack reaction, live status mirror, deterministic
  Stop-hook final answer). Enable with `clidecar plugin enable <name>` + recycle.
- `systemd/` — the supervisor unit + the OnFailure unit.
- `~/.clidecar/control/` — runtime flags (`RECYCLE`) + `claude.pid`.
- `~/.clidecar/state/` — live `state.md` (the "now") + `queue.md` (backlog).
  The repo ships `state/*.md.example` templates only.

## Conventions

- Bash: `set -uo pipefail`; best-effort side calls guarded with `|| true`.
- Notifier is best-effort and never fails its caller (exit 0).
- Commit style: terse, lowercase, no AI co-author trailer.
