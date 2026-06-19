# Self-recycling Claude harness — spec

A long-running Claude that can reset its own context, relaunch in a different
workdir / with different params, and even modify its own supervisor — without
an SSH-in. Cheap while idle, clean context on demand.

**Role:** a primary long-running assistant. Two jobs: (1) coding assistant,
(2) **triage agent** for inbound events (e.g. alerts routed to a chat channel).
The sidecar keeps me alive + fresh; a chat channel (Discord by default) is the
inbound channel; a durable memory store holds history and can act as a trigger
source.

**Already running inside a `screen` session** — so baseline persistence exists.
The sidecar adds *recycle / reset / relaunch-flexibility* on top of that; it is
not what keeps me alive in the first place.

## Environment

Targets Linux with `systemd --user`. If **linger** is enabled
(`loginctl enable-linger $USER`), user services survive reboot/logout with zero
privilege — no sudo required. Needs: git, screen, systemctl, bash 5+. Optional:
`inotifywait` (from inotify-tools) for instant flag response; without it the
sidecar falls back to a short poll loop.

Key consequence: **systemd --user is the supervisor AND the watchdog**, so a
hand-rolled mutual-watchdog process is unnecessary.

## Processes

- **C** — Claude itself (interactive, inside a `screen` session so the user can
  `screen -r` to attach). Not a persistent loop; killed/relaunched by S.
- **S** — sidecar (`sidecar.sh`), run as a **systemd --user service**
  (`clidecar.service`, `Restart=always`). The supervisor loop: launches C
  in screen, watches the flags, reads `config.env` for workdir/params.
- **~~W~~** — replaced by systemd. `Restart=always` + `StartLimitIntervalSec/
  Burst` give crash recovery + backoff for free; `OnFailure=` fires a handler to
  restore `known-good/` and ping Discord. No second bash watcher to maintain.

## Filesystem

Code and templates live in the repo; everything dynamic/personal lives in a
separate data dir, `~/.clidecar`, so the repo stays git-clean and publishable.

```
~/clidecar/                 # the repo (code + templates only)
  bin/
    sidecar.sh              # supervisor (the systemd --user service)
    clidecar                # control CLI (recycle / set / reload / status / ...)
    notify-discord.sh       # bot-API ping for code paths outside Claude
    fallback.sh             # OnFailure: restore known-good + notify
  systemd/                  # the supervisor unit + the OnFailure unit
  config.env.example        # template → copy to ~/.clidecar/config.env
  state/{state,queue}.md.example

~/.clidecar/                # the data dir (dynamic + personal, outside the repo)
  config.env               # WORKDIR, CLAUDE_ARGS, etc. — read at each (re)launch
  control/
    RECYCLE                # C touches → S recycles C (fresh context)
    claude.pid             # the managed Claude's PID (for adopt/kill)
  state/
    state.md               # C's handoff scratchpad (where things stand)
    queue.md               # next tasks
  known-good/
    sidecar.sh             # last-validated copy; OnFailure restores from here
```

## Core loop (S)

```
launch:  source ~/.clidecar/config.env  (or adopt an already-running C)
         launch C in $WORKDIR with $CLAUDE_ARGS inside screen; record PID
while true:
  wait until: RECYCLE flag appears  OR  C exits  (inotify, with a poll backstop)
  if RECYCLE:  rm flag; SIGTERM C; grace 10s; SIGKILL if alive; relaunch
  if C died:   relaunch (respecting the crash-loop guard)
```

Reloading the sidecar's own code is done out-of-band by `clidecar reload`
(`bash -n` validate → copy to known-good → `systemctl --user restart`, which
ADOPTS the running C untouched via `KillMode=process`) — not via an in-loop flag.

## Memory / persistence layers

Three layers, NOT interchangeable. Durable context lives in a **memory store**,
not a giant life-context markdown file.

- **CLAUDE.md** (`~/.claude/CLAUDE.md`) — auto-loaded every session. The
  always-on **rules / operational protocol** (how to recycle, the flag/config
  locations, checkpoint-before-flag). A fresh me must know this unprompted.
- **memory store** — durable, pull-based memory with semantic recall (e.g. an
  MCP memory server). The **history / deep context**: I recall by circumstance
  instead of ingesting a huge md. Lean on it for anything that must survive a
  recycle.
- **`state.md` + `queue.md`** — the live **now**: what I was mid-doing, what's
  next. Small, churns every recycle. NOT a context store.

Recycle handoff = checkpoint durable facts to the memory store + live state to
`state.md`, then flag. Fresh me: reads CLAUDE.md (always), reads `state.md`
(now), recalls from the memory store as needed (history).

## The three capabilities

1. **Flag-kill / context reset.** C checkpoints to `state.md`, then
   `touch control/RECYCLE`. S kills + relaunches a fresh C, which reads
   `state.md` + `queue.md` and continues. Order is load-bearing: checkpoint
   BEFORE flag.

2. **Change workdir / calling params.** C edits `config.env` (e.g. new
   `WORKDIR=`, different `CLAUDE_ARGS=`), then `touch RECYCLE`. Next loop
   iteration `source`s the new config and relaunches there. Config is the
   single source of truth for how C is launched.

3. **Self-modify the sidecar.** C edits `bin/sidecar.sh`, runs `bash -n` to
   validate, copies to `known-good/` only if valid, then
   `touch RELOAD_SIDECAR`. S does `exec "$0"` — the running process becomes
   the new script, same PID, C untouched. No tower of supervisors needed for
   the common case.

## Supervision (systemd --user, not hand-rolled W)

The mutual-watchdog idea was a workaround for not having a real supervisor. We
have one — systemd --user, reboot-proof via the already-set linger:

- **Self-update the sidecar:** edit `sidecar.sh` → `bash -n` validate → copy to
  `known-good/` → `systemctl --user restart clidecar`. systemd relaunches
  it cleanly. (No `exec "$0"` gymnastics needed, though it still works.)
- **Crash recovery + backoff:** `Restart=always` with
  `StartLimitIntervalSec=60` / `StartLimitBurst=5` — if I ship a sidecar that
  crashes on start, systemd retries a few times then gives up (no infinite
  fire), exactly the backoff I was hand-designing.
- **On give-up:** `OnFailure=clidecar-fallback.service` → restores
  `known-good/sidecar.sh` and pings Discord. This tiny fallback is the only
  remnant of W, and it's a one-shot unit, not a running watcher.
- **Cold start / reboot:** the user service is `enable`d, linger is on, so it
  comes back on reboot with no cron and no sudo.

## Safety rails

- **Checkpoint before flag** (reset) / **validate before reload** (`bash -n`,
  copy-to-known-good) — the discipline analogues that prevent amnesia and
  crash-loops.
- **Backoff:** systemd `StartLimitIntervalSec`/`Burst` handles it. A fresh C
  reading a poisoned `queue.md` and instantly re-flagging is the classic fire;
  the limit contains it, then `OnFailure` pings the user.
- **SIGTERM + 10s grace, then SIGKILL** — never corrupt a half-written file.
- **screen** so a recycle never loses the terminal; journald keeps S's logs.

## Settled

- **Host:** C runs in `screen`; S = systemd --user service drives recycle.
- **Supervisor:** systemd --user (reboot-proof via existing linger) — subsumes W.
- **State store:** a memory store for durable history; `state.md`/`queue.md` live handoff.
- **Location:** `~/clidecar/` is a **git repo** — scripts version-controlled.
- **Privilege:** buildable with NO sudo. Only optional item = inotify-tools.
