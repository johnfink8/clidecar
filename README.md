# clidecar — a self-recycling Claude harness

A long-running [Claude Code](https://claude.com/claude-code) session that can
reset its own context, relaunch itself in a different working directory or with
different parameters, and even hot-swap its own supervisor — all from inside the
session, no SSH-in required. A small `systemd --user` service keeps it alive,
fresh, and reboot-proof.

## Why

The goal is **one always-on assistant that's both a coding agent and a triage
agent, across many projects** — not a fleet of throwaway chat windows.

A plain Claude session doesn't get you there: its context fills up and goes
stale, it's pinned to the directory you launched it in, and it dies on reboot or
logout. So you end up babysitting it — restarting, re-orienting, opening a new
window per project.

clidecar turns a single instance into a durable one:

- **Stays fresh** — it checkpoints its working state and *recycles* its own
  context on demand, then a fresh instance reads the checkpoint and continues.
- **Follows you across projects** — change the working directory (or launch
  args) and relaunch, without touching the supervisor.
- **Survives reboots** — supervised by `systemd --user` with linger, so it comes
  back with no cron and no sudo.
- **Improves itself in place** — it can edit its own supervisor script, validate
  it, and reload — adopting the running session untouched.
- **Triages inbound events** — wired to an optional chat channel (Discord), it
  can be woken by alerts and work them human-in-the-loop.

## How it works

- **Supervisor:** `systemd --user` runs `bin/sidecar.sh`. With linger enabled it
  survives reboot/logout with no sudo. `Restart=always` + `StartLimit*` give
  crash recovery and backoff; `OnFailure` restores a known-good sidecar.
- **The managed Claude** runs inside a detached `screen` session
  (`screen -r clidecar` to watch). The sidecar launches it, watches for a
  `RECYCLE` flag, and relaunches it if it dies.
- **Recycle (context reset):** the session checkpoints to `state.md` + its memory
  store, then runs `clidecar recycle`. The sidecar SIGTERMs it (10s grace) and
  launches a fresh instance, which re-reads config, `state.md`, and recalls from
  memory.
- **Change workdir/params:** `clidecar set WORKDIR /path` (or edit `config.env`)
  then `clidecar recycle`. The next launch reads the new config.
- **Self-modify the sidecar:** edit `bin/sidecar.sh`, then `clidecar reload`
  (syntax-checks, copies to known-good, `systemctl --user restart`). The managed
  Claude is adopted, not disturbed (`KillMode=process`).

See [SPEC.md](SPEC.md) for the full design and rationale.

## Layout

Code and templates live in the repo. Everything dynamic and personal — your
config, live state, runtime flags, the known-good cache — lives in a separate
data dir, `~/.clidecar`, so the repo stays clean.

```
~/clidecar/                    the repo (code + templates)
  config.env.example           launch + timing config template
  bin/
    sidecar.sh                 the supervisor loop (systemd --user service)
    clidecar                   control CLI used from inside the managed session
    notify-discord.sh          bot-API ping (sidecar/fallback can't reach the MCP)
    fallback.sh                OnFailure: restore known-good sidecar + ping
    _pluginctl.py              enable/disable hook plugins (edits .claude/settings.json)
  systemd/
    clidecar.service           the supervisor unit
    clidecar-fallback.service  OnFailure one-shot
  plugins/
    discord-bridge/            Discord output bridge (optional hook plugin)
      plugin.json              hooks fragment + metadata
      hook-{ack,progress,final}.py, _hooklib.py, transcript.py, discord-msg.sh
  state/{state,queue}.md.example

~/.clidecar/                   the data dir (outside the repo)
  config.env                   your live config (copied from the template)
  state/{state,queue}.md       the live "now" + backlog (churn each recycle)
  control/                     runtime flags + claude.pid
  known-good/sidecar.sh        last-validated sidecar (auto-restored on failure)
```

## Install

Requires: `claude` (Claude Code), `bash` 5+, `screen`, `curl`, `python3`,
`systemd --user`. Optional: `inotify-tools` (instant flag response; falls back to
polling), and `bun` if you use the Discord channel plugin. The systemd units
assume the repo lives at `~/clidecar`.

```sh
# 1. clone to ~/clidecar
git clone <repo-url> ~/clidecar

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

# 4. (optional) Discord bridge: put your bot token in
#    ~/.claude/channels/discord/.env  as  DISCORD_BOT_TOKEN=...
#    set DISCORD_CHANNEL_ID in ~/.clidecar/config.env, then enable the plugin:
#    clidecar plugin enable discord-bridge   (takes effect on next recycle)

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

`clidecar status` / `clidecar logs` to check on it; `screen -r clidecar` to
attach to the live session.

## Memory model

Three layers, deliberately separate:

- **~/.claude/CLAUDE.md** — always-loaded rules / recycle protocol.
- **a durable memory store** — pull-based history, retrieved by semantic recall.
  clidecar is built around [quorelo](https://quorelo.com), the per-user memory MCP
  server (`recall` / `remember` / `update_memory`): the managed Claude
  checkpoints durable facts there and recalls them by circumstance after a
  recycle, instead of carrying a giant context file. Any MCP memory server with
  recall/store semantics works, but quorelo is the recommended pairing. Because I
  built it.
- **state.md / queue.md** — the live now + backlog (churn every recycle).

## Plugins

Optional hook-based add-ons live in `plugins/<name>/`, each a self-contained dir
with a `plugin.json` (a Claude Code hooks fragment using a `${PLUGIN_DIR}`
placeholder). Manage them from the control CLI:

```sh
clidecar plugin list                   # what's available + on/off
clidecar plugin enable discord-bridge  # wire its hooks into .claude/settings.json
clidecar plugin disable discord-bridge
clidecar recycle                       # hooks load at launch, so recycle to apply
```

Bundled: **discord-bridge** — makes Discord a guaranteed, console-faithful output
sink. It reacts 👀 to your message, keeps a live status message mirroring the
turn's narration and tool calls (re-homed below any messages you send mid-turn),
and a `Stop` hook deterministically posts the turn's closing answer — so a reply
is never lost to the console even if the model forgets to send it. Editing a
plugin's scripts takes effect immediately; only enabling/disabling (which changes
`.claude/settings.json`) needs a recycle.

## Remote control (optional)

You can drive the managed session from claude.ai or the Claude mobile app with
Claude Code's [Remote Control](https://code.claude.com/docs/en/remote-control):
add `--remote-control "clidecar"` to `CLAUDE_ARGS` in `config.env`, then
`clidecar recycle`. Requires a claude.ai subscription and a logged-in session.
Handy here because the managed Claude has no attached terminal of its own — this
gives you a way in from anywhere without `screen -r`.

## Limitations & security model

- **The auto-mode permission classifier will block some actions, by design — and
  we keep it that way.** With `--permission-mode auto`, Claude Code runs a
  classifier that denies actions it judges risky given their *provenance*. In
  particular, requests that arrive over an inbound channel (e.g. Discord) are
  treated as untrusted — the classifier can't distinguish a legitimate request
  from a prompt-injection wearing the same words — so it refuses
  security-sensitive operations (changing trust/permission state, etc.) even with
  a matching allow-rule. This is a feature, not a bug: it's what makes it safe to
  expose an autonomous agent to an inbound channel. **Do not disable it**, and we
  recommend others keep it on too. The escape hatch, when you genuinely need such
  an action, is to run that one command yourself from the terminal — your own
  invocation isn't subject to the classifier.
- **Linux + systemd --user only.** No macOS/launchd or Windows support.
- **One managed session per service.** The harness supervises a single Claude.

## License

[MIT](LICENSE).
