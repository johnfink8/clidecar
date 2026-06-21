# Heartbeat scheduler — design spec

Status: DESIGN (nothing built). Scope: the *activation/scheduling mechanism* for
running scheduled assistant work on clidecar. Not the heartbeat *content* (Gmail
triage, daily plan, etc.) — that's a separate port.

PARKED (descoped 2026-06-21, scope creep): a multi-surface "fleet" where each
clidecar personality is a persistent *interactive* `claude` on its own Discord
channel (project dev surfaces: `quorelo-dev`, `mediamanager-dev`, …), spun up by
deterministic tooling that auto-creates the channel, with the gateway broker
multiplexing channel↔surface. Good idea, separate build. Heartbeats deliberately
do NOT use it: an interactive session waits for a human, which is a hang for
unattended work — `claude -p` (runs to completion, never waits) is the right
primitive for heartbeats and is what this spec plans.

## The problem

clidecar is one persistent interactive `claude` in `screen` carrying live
conversational context. We want scheduled work — heartbeats — to run **without
touching that session**: a scheduled fire must spawn a *fresh* context, do its
work, deliver to the channel, and exit, leaving the interactive session
untouched even if it's mid-task or idle-with-context.

We also want timing that's **deterministic and non-wasteful** (fires exactly
when due; no hourly polling that burns Max weekly-limit budget on idle wakes),
yet expressive enough for rules like "noon Friday and 9am Tuesday, except shift
to Wednesday on a long weekend."

## Mechanism

### Activation: fresh subprocess per fire

A scheduled fire spawns a **fresh headless `claude -p`** subprocess — empty
context, runs to completion, exits. Same `claude` binary → same Max
subscription/auth. Because it's a separate OS process, the interactive screen
session is *never* touched: no shared context, no interruption, and scheduled
work keeps running across a recycle of the interactive session.

(This is OpenClaw's `isolatedSession`/`agentTurn` idea, but as a real subprocess
instead of an in-process isolated run — we don't own the agent loop, which makes
the isolation harder and the implementation simpler.)

### Home: in the gateway daemon

The scheduler lives **inside the existing persistent gateway daemon**
(`bridge/gateway.py`) — already long-lived, already clidecar-aware, already owns
the Discord broker, already survives recycles. Concretely: a re-arming
timer loop (port OpenClaw's `setTimeout`-re-arm pattern; **not** OS cron) over a
persisted JSON job store. The daemon owning the broker is the payoff for
delivery (below).

### Delivery: through the broker the daemon already owns

The fresh `claude -p` run does its tool work (Gmail, quorelo, calendar, …) and
its output reaches the channel via the **broker the daemon already owns**
(`ex.emit`). The interactive session's bridge hooks never enter the picture — no
double-delivery, no entanglement.

**Default = capture-and-emit.** The `claude -p` run does its tool work and
returns a final summary as its result (`--output-format json`); the daemon
parses it and `ex.emit`s it to the target channel. The heartbeat needs **no**
gateway access of its own — delivery is the daemon's job. This is the most
decoupled and robust shape and avoids a trap: if a heartbeat attached the
gateway *shim* it would announce as a `channel` and fight the main interactive
session for the broker's single inbound-attach slot (the multiplexer that would
fix that is the parked fleet work).

**Opt-in = outbound-only broker client.** A heartbeat that needs rich or
incremental posting (multiple messages, an attachment) gets a broker client that
calls `emit`/`react`/`edit` **only** — it never claims the inbound `_channel`
slot, so no conflict. `emit` gains an optional `chat_id` target so a heartbeat
can post to a channel bound by id (e.g. an existing `quorelo-alerts`-style
channel) instead of the main DM. Default target is the main channel.

## The schedule model

Complex timing is expressed as a **union of plain cron lines pointing at one
heartbeat target** — not a mega-schedule with embedded logic. Exceptions/shifts
(the part a union can't express, since it can only add fire-times) are handled by
an optional per-trigger **guard predicate** evaluated *before* spawning, so a
suppressed fire costs nothing.

```
Job (a "heartbeat"):
  id          : stable slug
  prompt      : the task definition fed to the fresh session
  session     : { model?, permission_mode?, attach_shim?, hard_timeout_s, output_format }
  triggers    : [ Trigger, ... ]          # the union; ≥1
  lifecycle   : Lifecycle | null          # null = runs forever
  state       : { next_run, last_run, last_status, runs_done, running_at }

Trigger:
  kind        : "cron" | "at"
  expr        : cron string         (kind=cron)   e.g. "0 9 * * 2"
  when        : ISO-8601 absolute   (kind=at)
  tz          : IANA zone, default America/New_York
  guard       : GuardRef | null     # skip this fire unless the predicate passes

GuardRef:
  name        : a key in the shipped guard registry, e.g. "long_weekend"
  params      : object              # e.g. { "region": "US" }
  negate      : bool                # default false; true = skip WHEN it matches

Lifecycle (self-eating / one-time):
  max_runs    : int | null          # remove after this many CONSUMED runs (1 = one-shot)
  until       : ISO-8601 | null     # remove after this time regardless
  on_consume  : "success" | "fire"  # default "success"
```

Examples:

- "Noon Friday and 9am Tuesday" → one job, two cron triggers
  (`0 12 * * 5`, `0 9 * * 2`).
- "...but Wednesday on long weekends" → swap the Tuesday line for two guarded
  lines: `0 9 * * 2` guarded `long_weekend` (negate), and `0 9 * * 3` guarded
  `long_weekend`.
- "Remind me once next Tuesday 3pm, then delete" → one `at` trigger +
  `lifecycle: { max_runs: 1, on_consume: "success" }`.

## Self-eating semantics — "consume on success"

`max_runs`/`until` decrement/remove on a **consumed** run. Default
`on_consume: "success"` means a run is consumed only when the fire **succeeded**
— default definition: the headless process exited 0 **and** the delivery emit
landed. A failed fire is retried per policy and is **not** consumed, so a flaky
reminder doesn't silently eat itself. `on_consume: "fire"` consumes
unconditionally (fire-and-forget).

Open: "success" may later need a per-job structured marker the session emits
(e.g. it decides the task wasn't actually completable) rather than just exit-0.

## Runtime adaptability — schedules are data, guards are code

The job store is **data**, mutated at runtime through a gateway tool surface the
model already reaches over the broker:

```
schedule_list()
schedule_add(job)            # validated against the schema, atomic store write
schedule_update(id, patch)
schedule_remove(id)
```

The JSON store underneath is the persistence and stays human-editable. This lets
the interactive model — or a heartbeat session itself — add a one-shot, retune a
cron line, or disable a job, without a recycle.

**The boundary:** the model composes triggers and references guards *by name*
from a **shipped registry** of predicates (`long_weekend`, `business_day`,
`last_business_day_of_month`, …) with params. It cannot inject new predicate
*logic* at runtime — adding a new guard is a repo edit + reload. This is the same
mutable-policy / stable-mechanism split clidecar already runs on (hook scripts &
config are live; daemon/sidecar code needs reload).

## Robustness: the silent-brick antidote

`claude -p` has **no built-in wall-clock limit**, and — critically — it has
documented **silent failure modes** that are exactly how a headless agent
"bricks silently for weeks":

- It can **hang forever** with no output and no exit (subprocess-spawn hang,
  upstream issue #56268).
- In `--output-format stream-json` it can **exit 0 but never actually exit**
  after sending its final result (issue #25629) — a zombie.
- It can **exit 0 with empty/irrelevant output** on an ambiguous prompt.
- A provider streaming-idle timeout (~5 min on Vertex/Foundry via
  `CLAUDE_STREAM_IDLE_TIMEOUT_MS`/`API_FORCE_IDLE_TIMEOUT`) can abort a stalled
  stream — provider-dependent; may not apply on the Anthropic API path, but the
  hang/empty modes above do.

So **the scheduler must never trust the subprocess to fail loudly.** The daemon
owns the failure contract:

1. **Hard wall-clock ceiling** — every fire is wrapped
   `timeout --kill-after=<g> <hard_timeout_s> claude -p …`, so a hang is
   SIGKILLed, not waited-on forever.
2. **Three-way failure detection** — a fire is a FAILURE if: non-zero exit
   (incl. the `timeout` 124/137 codes), **or** zero exit with empty output,
   **or** killed by the ceiling. Only a clean exit-0-with-output is a candidate
   success.
3. **`output_format: "json"`, not `stream-json`** — avoids the post-completion
   zombie-hang bug; parse the structured result.
4. **Consume-on-success couples to this** — a failed/hung/empty fire is *not*
   consumed, so a self-eating job retries instead of silently deleting itself
   after a brick.
5. **Loud on every non-success** — a failed fire pings out-of-band
   (`notify-discord`) + persists, never a silent log line.
6. **Staleness escalation** — if a recurring job has had no *successful* run
   across N expected slots, escalate. This catches the precise "went dark weeks
   ago and nobody noticed" mode: a bricked heartbeat becomes a Discord ping, not
   silence.

Generous per-tool timeouts (`BASH_DEFAULT_TIMEOUT_MS`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS`)
can be set in the headless run's env for legitimately long work, but the
daemon-owned `timeout` wrapper is the real safety net — it's the one thing that
cannot itself hang.

## Inherited-for-free from the in-daemon store

Porting OpenClaw's store shape gets these without extra design:

- **Missed-run catch-up** — on daemon start, fire jobs whose previous scheduled
  slot elapsed unrun (`previous_slot > last_run`), bounded + staggered. A missed
  `at` reminder fires late (still wanted) unless its `until` has passed.
- **Overlap lock** — `running_at` reservation persisted before spawn; a job
  already running is skipped.
- **Timezone/DST** — per-trigger IANA zone; cron evaluated DST-correct.
- **Anti-thundering-herd** — deterministic per-job stagger so many top-of-hour
  jobs don't fire on the same second.

## Resolved decisions

- **Delivery** — capture-and-emit by default; outbound-only broker client opt-in
  (see Delivery, above).
- **Success** — a fire succeeds iff `claude -p` exits 0 **and** its
  `--output-format json` result is non-empty with `is_error: false`. Anything
  else (non-zero, timeout-kill 124/137, empty, `is_error`) is a failure → not
  consumed, retried, alerted.
- **Concurrency** — a configurable cap on concurrent heartbeat fires, default 1
  (serialize; heartbeats aren't latency-sensitive, and serializing is gentle on
  the Max weekly limit). Same-job overlap is already prevented by `running_at`.
- **Permissions / least privilege** — each job carries an `allowedTools`
  allowlist; the headless run uses a non-blocking permission mode scoped to that
  allowlist. **Not** `--dangerously-skip-permissions` (that would defeat the
  untrusted-data security model — a heartbeat reads untrusted content like Gmail
  bodies even though its prompt is trusted). Allowed tools don't prompt, so the
  run doesn't block.
- **AskUserQuestion in a heartbeat** — heartbeats run with a settings variant
  whose AskUserQuestion hook **denies immediately** with "you are an unattended
  heartbeat — report what you'd ask as a message and finish," so a heartbeat
  never blocks waiting for an answer it can't get.
- **Guard registry** — `bridge/guards.py`, a `name -> Callable` table; predicate
  signature `guard(now: datetime, params: dict[str, object]) -> bool` (True =
  allow this fire). Pyright-strict, core scheduler logic.

## Dependencies & environment

The bridge is a proper uv project (`pyproject.toml` + `uv.lock`, `.venv` via
`uv sync`). Functional deps: **`croniter`** (cron evaluation) and **`holidays`**
(holiday calendars for `long_weekend`-style guards). Dev tooling: `ruff` +
`pyright` (strict, config in `pyproject.toml`). The gateway daemon runs under the
venv interpreter via `PYTHON_BIN` in `~/.clidecar/config.env`; hooks and the
shim stay on portable `#!/usr/bin/env python3` (they don't import these deps).

## Build plan

Each phase is independently testable; the scheduler lives in the gateway daemon,
so it ships via `clidecar gateway reload` — **no Claude context recycle needed.**

1. **Job store + schema** (`bridge/schedule.py`) — `Job`/`Trigger`/`GuardRef`/
   `Lifecycle` dataclasses with validating `from_obj`, atomic JSON persistence
   under `~/.clidecar/state/schedule.json`, load/list/write. Pyright strict.
2. **Schedule math** — `croniter`-backed `compute_next_run(job, now)` over the
   trigger union (min future fire), `at` handling, guard evaluation *before* a
   fire is counted.
3. **Guard registry** (`bridge/guards.py`) — the predicate table + initial guards
   (`long_weekend`, `business_day`, …) backed by the `holidays` package.
4. **Fire path** — spawn `timeout … claude -p --output-format json` with the
   job's `allowedTools`/permission-mode/env; three-way failure detection;
   capture + parse result; `ex.emit` to the target channel; consume-on-success
   lifecycle; loud alert (`notify-discord` + persist) on any non-success;
   `running_at` lock; the concurrency semaphore.
5. **Daemon integration** (`bridge/gateway.py`) — the re-arming timer loop;
   missed-run catch-up on start; staleness escalation (no successful run across N
   expected slots → ping).
6. **Runtime tooling** — `schedule_add/update/remove/list` broker ops + MCP tools;
   `clidecar schedule …` CLI verb.
7. **Tests + reviews** — ephemeral tests for schedule math, guard eval,
   failure-detection, lifecycle consumption; fail-loud + code-style; pyright 0.
