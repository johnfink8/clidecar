#!/usr/bin/env bash
# sidecar.sh — supervises the managed Claude session.
#
# Responsibilities:
#   - launch Claude inside a detached `screen` session (per config.env)
#   - on RECYCLE flag: gracefully kill Claude and relaunch (fresh context),
#     re-reading config.env so workdir/param changes take effect
#   - if Claude dies unexpectedly: relaunch, with a crash-loop guard
#   - survive its own restart by ADOPTING an already-running Claude
#     (so `clidecar reload` swaps sidecar code without disturbing Claude)
#
# Runs as the `clidecar` systemd --user service. systemd provides
# reboot-survival (linger), Restart=always, StartLimit backoff, and OnFailure
# fallback — so this script does not reimplement any of that.
set -uo pipefail

CLIDECAR_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# All runtime/personal data lives OUTSIDE the repo, in ~/.clidecar. The repo
# holds only code + templates. Fixed path (not env-overridable) so the CLI and
# the systemd-launched sidecar can never disagree on where data lives.
CLIDECAR_DATA="$HOME/.clidecar"
export CLIDECAR_HOME CLIDECAR_DATA
CONTROL_DIR="$CLIDECAR_DATA/control"
AGENTS_DIR="$CONTROL_DIR/agents"          # per-agent pidfiles + RECYCLE flags: agents/<id>/{claude.pid,RECYCLE}
GATEWAY_PIDFILE="$CONTROL_DIR/gateway.pid"
GATEWAY_SCRIPT="$CLIDECAR_HOME/bridge/gateway.py"
GATEWAY_LOG="$CLIDECAR_DATA/state/gateway-daemon.log"
CONFIG_FILE="$CLIDECAR_DATA/config.env"
KNOWN_GOOD="$CLIDECAR_DATA/known-good/sidecar.sh"
FLEETCTL="$CLIDECAR_HOME/bin/_fleetctl.py"
mkdir -p "$CONTROL_DIR" "$AGENTS_DIR" "$CLIDECAR_DATA/state" "$CLIDECAR_DATA/known-good"
# seed the known-good copy on first run so OnFailure always has a restore target
[ -f "$KNOWN_GOOD" ] || cp "$CLIDECAR_HOME/bin/sidecar.sh" "$KNOWN_GOOD" 2>/dev/null || true

log() { printf '%s sidecar: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }

load_config() {
  if [ ! -f "$CONFIG_FILE" ]; then
    log "FATAL: $CONFIG_FILE not found — copy config.env.example there"
    exit 1
  fi
  set -a; . "$CONFIG_FILE"; set +a
}

# --- fleet manifest helpers (the desired-state source of truth) ---
# All read through bin/_fleetctl.py so the shell never parses JSON. A read FAILS LOUD (nonzero) on an
# unreadable/missing manifest, and every reconcile caller treats that as "skip — keep the live fleet",
# NEVER as "zero agents" (which would reconcile every agent to death).
fleet_enabled_agents() { python3 "$FLEETCTL" list --enabled 2>/dev/null; }
agent_field()          { python3 "$FLEETCTL" get "$1" "$2" 2>/dev/null; }
fleet_seed()           { python3 "$FLEETCTL" seed; }

agent_dir()     { echo "$AGENTS_DIR/$1"; }
agent_pidfile() { echo "$AGENTS_DIR/$1/claude.pid"; }
agent_screen()  { echo "${SCREEN_NAME:-clidecar}-$1"; }
agent_pid()     { cat "$(agent_pidfile "$1")" 2>/dev/null; }
agent_alive() {
  local p; p="$(agent_pid "$1")"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  # PID-reuse guard: after a reboot the recorded PID may now belong to an unrelated process. Only
  # treat it as our claude if the live process's cmdline really contains "claude" — else we'd
  # falsely "adopt" a stranger and never launch a fresh one. grep -a: cmdline is NUL-sep.
  grep -qa claude "/proc/$p/cmdline" 2>/dev/null
}

# Agents that currently have a live process (by per-agent pidfile), regardless of the manifest — so a
# reconcile can stop an agent that was disabled/removed from the fleet.
running_agents() {
  local d id
  [ -d "$AGENTS_DIR" ] || return 0
  for d in "$AGENTS_DIR"/*/; do
    [ -d "$d" ] || continue
    id="$(basename "$d")"
    agent_alive "$id" && echo "$id"
  done
}

# The persistent gateway daemon owns the Discord WS + broker socket + inbound routing, surviving
# both supervisor reloads (adopted, not killed) and Claude recycles. Only managed when
# GATEWAY_DAEMON=1: until the cutover that flips CC onto the stdio shim, the gateway still runs the
# old way (as CC's own MCP child), so this entire lane stays INERT and a `clidecar reload` can't
# spawn a competing daemon. Detached via setsid so a supervisor restart doesn't drag it down.
gateway_enabled() { [ "${GATEWAY_DAEMON:-0}" = 1 ]; }
gateway_pid()     { cat "$GATEWAY_PIDFILE" 2>/dev/null; }
gateway_alive() {
  local p; p="$(gateway_pid)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  # PID-reuse guard, same as claude: only ours if the live cmdline really is the gateway.
  grep -qa "gateway.py" "/proc/$p/cmdline" 2>/dev/null
}

launch_gateway() {
  load_config
  gateway_enabled || return 0
  rm -f "$GATEWAY_PIDFILE"
  local py="${PYTHON_BIN:-python3}"
  # the inner shell records its own PID then exec's python, so PIDFILE == the daemon process
  setsid bash -lc "echo \$\$ > $(printf %q "$GATEWAY_PIDFILE") && exec $(printf %q "$py") $(printf %q "$GATEWAY_SCRIPT")" \
    >> "$GATEWAY_LOG" 2>&1 &
  sleep 1
  log "launched gateway daemon pid=$(gateway_pid)"
}

stop_gateway() {
  load_config
  local p; p="$(gateway_pid)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    log "SIGTERM gateway ($p); grace ${GRACE_SECS}s"
    kill -TERM "$p" 2>/dev/null || true
    for _ in $(seq 1 "$GRACE_SECS"); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
    kill -0 "$p" 2>/dev/null && { log "SIGKILL gateway ($p)"; kill -KILL "$p" 2>/dev/null || true; }
  fi
  rm -f "$GATEWAY_PIDFILE"
}

# --dangerously-load-development-channels shows a blocking, per-launch modal at startup
# ("WARNING: Loading development channels", option 1 pre-selected). A custom channel — our
# transport-agnostic gateway — is the only provenance-preserving way to inject inbound, and
# during the research preview the approved allowlist is Anthropic-only, so the flag (and its
# modal) is unavoidable. Auto-confirm it here so unattended recycles don't stall. This stuffs a
# single Enter into a startup y/n DIALOG — it is NOT injecting channel content as a prompt, so
# it launders no untrusted provenance. Only runs when the flag is actually present (the sidecar
# stays transport-agnostic). Return code: 0 = cleared/absent; 1 = the modal was OBSERVED but won't
# clear (a definitive wedge) — the one stalled-inbound condition the crash-loop guard can't see (a
# wedged agent is alive, never relaunched), so the caller pages (throttled); 2 = indeterminate (modal
# text never matched, or the screen is gone) — log-only, so modal-wording drift in a future claude
# release can't cry wolf. The owner's safety nets if inbound is down: console + remote-control.
# $1 = the agent's screen session, $2 = that agent's CLAUDE_ARGS (per-agent now, from the fleet).
confirm_dev_channel() {
  local scr="$1" args="$2"
  case "$args" in
    *dangerously-load-development-channels*) ;;
    *) return 0 ;;
  esac
  local snap tries=0 v
  snap="$(mktemp)"
  while [ "$tries" -lt 40 ]; do
    # hardcopy returns nonzero when the session is gone — the agent died on launch, so no modal will
    # ever appear. Bail instead of polling a corpse for 20s (which also floods stdout with "No screen
    # session found" once per try). The crash-loop guard owns the relaunch-rate alert.
    screen -S "$scr" -X hardcopy "$snap" >/dev/null 2>&1 || { log "dev-channel: screen '$scr' gone — agent exited at launch"; rm -f "$snap"; return 2; }
    if grep -q "Loading development channels" "$snap" 2>/dev/null; then
      screen -S "$scr" -X stuff $'\r' >/dev/null 2>&1
      v=0
      while [ "$v" -lt 10 ]; do
        sleep 0.5
        screen -S "$scr" -X hardcopy "$snap" >/dev/null 2>&1
        grep -q "Loading development channels" "$snap" 2>/dev/null || {
          log "dev-channel modal auto-confirmed ($scr)"; rm -f "$snap"; return 0; }
        v=$((v + 1))
      done
      # Modal was observed but won't clear: the agent is alive but wedged (inbound stalled). Return 1
      # (definitive) — the caller pages for this, throttled per id (NOT a per-launch ping).
      log "dev-channel modal did NOT clear after confirm ($scr)"
      rm -f "$snap"; return 1
    fi
    sleep 0.5; tries=$((tries + 1))
  done
  rm -f "$snap"
  # Indeterminate: the modal text never matched in the scan window. Could be a genuinely-stuck agent,
  # but on a healthy agent it's most likely modal-wording drift in a new claude release — return 2
  # (log-only, no page) rather than cry wolf about stalled inbound.
  log "dev-channel modal never appeared within timeout ($scr)"
  return 2
}

# Network-readiness gate, geometric backoff capped at 1h.
#
# The unit's After=/Wants=network-online.target is a NO-OP for a --user service
# (that target is passive in the user manager — nothing activates it), so on a
# cold boot the sidecar can start before the network is up. Claude then spawns
# its MCP server children (quorelo, gmail, …) before DNS/API are reachable;
# those connects fail and CC does NOT retry them, leaving the session amnesiac
# until the next recycle. So block each Claude launch until the Anthropic API
# actually answers. (The gateway daemon is not gated here — it owns WS
# reconnect/backoff and self-heals a down network on its own.)
#
# Backoff doubles 5s→1h so a long outage (e.g. ISP still down after a power cut)
# is waited out patiently, not hammered. Discord can't be notified during the
# wait — that needs the very network we're waiting on — so the retry log lines
# (`clidecar logs`) are the loud signal.
wait_for_network() {
  command -v curl >/dev/null 2>&1 || { log "curl missing — skipping network gate"; return 0; }
  local url="${NET_PROBE_URL:-https://api.anthropic.com/}" delay=5 max=3600 waited=0
  # No -f on purpose: any HTTP response (even 404/401) proves DNS+TLS+reach; curl
  # only errors (resolve/connect/timeout) when the network is genuinely down.
  while ! curl -sS -o /dev/null --max-time 5 "$url" >/dev/null 2>&1; do
    log "network unreachable ($url) — retry in ${delay}s (waited ${waited}s total)"
    sleep "$delay"
    waited=$((waited + delay))
    delay=$((delay * 2)); [ "$delay" -gt "$max" ] && delay="$max"
  done
  [ "$waited" -gt 0 ] && log "network reachable after ${waited}s"
  return 0
}

# Launch ONE agent: its own screen (clidecar-<id>), pidfile, workdir/args/channel from the fleet, and
# CLIDECAR_AGENT_ID/CHANNEL exported so the shim announces the right agent and hooks reach the right
# channel. workdir/pidfile/CLAUDE_BIN are %q-quoted; args stays unquoted so it word-splits into flags.
launch_agent() {
  load_config
  local id="$1" scr workdir args channel pidfile
  scr="$(agent_screen "$id")"
  workdir="$(agent_field "$id" workdir)"
  args="$(agent_field "$id" args)"
  channel="$(agent_field "$id" channel)"
  if [ -z "$workdir" ]; then
    log "launch_agent '$id': no workdir in fleet — skipping"; return 1
  fi
  wait_for_network
  pidfile="$(agent_pidfile "$id")"
  mkdir -p "$(agent_dir "$id")"
  screen -S "$scr" -X quit >/dev/null 2>&1 || true
  rm -f "$pidfile"
  local inner
  inner="export CLIDECAR_AGENT_ID=$(printf %q "$id") CLIDECAR_AGENT_CHANNEL=$(printf %q "$channel") && cd $(printf %q "$workdir") && echo \$\$ > $(printf %q "$pidfile") && exec $(printf %q "$CLAUDE_BIN") $args"
  if [ -n "${STARTUP_PROMPT:-}" ]; then
    # `--` terminates option parsing so the positional prompt isn't swallowed by a preceding greedy
    # variadic flag. --dangerously-load-development-channels takes 1+ tagged entries; if it's the last
    # flag (e.g. an agent with no --remote-control after it), it eats the prompt as an untagged channel
    # entry and Claude exits at launch. The separator makes the prompt positional regardless of arg order.
    inner="$inner -- \"\$STARTUP_PROMPT\""
  fi
  screen -dmS "$scr" bash -lc "$inner"
  sleep 1
  log "launched agent '$id' screen='$scr' workdir='$workdir' pid=$(agent_pid "$id")"
  # Page only for a DEFINITIVE wedge (rc=1) on a still-alive agent — the stalled-inbound case the
  # crash-loop guard can't see. rc=2 (indeterminate / screen gone) is log-only: a dead agent is the
  # crash-loop guard's job, and an unmatched modal is likely wording drift, not a real stall.
  local rc=0
  confirm_dev_channel "$scr" "$args" || rc=$?
  if [ "$rc" -eq 1 ] && agent_alive "$id"; then
    alert_dev_modal_stall "$id"
  fi
}

graceful_kill_agent() {
  load_config
  local id="$1" scr p pidfile
  scr="$(agent_screen "$id")"; pidfile="$(agent_pidfile "$id")"; p="$(agent_pid "$id")"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    log "SIGTERM agent '$id' ($p); grace ${GRACE_SECS}s"
    kill -TERM "$p" 2>/dev/null || true
    for _ in $(seq 1 "$GRACE_SECS"); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
    if kill -0 "$p" 2>/dev/null; then log "SIGKILL agent '$id' ($p)"; kill -KILL "$p" 2>/dev/null || true; fi
  fi
  screen -S "$scr" -X quit >/dev/null 2>&1 || true
  rm -f "$pidfile"
}

stop_agent() { graceful_kill_agent "$1"; log "stopped agent '$1' (not in the enabled fleet)"; }

# --- per-agent crash-loop guard: bounds *unexpected* relaunches, not deliberate recycles ---
declare -A AGENT_LAUNCHES   # id → space-separated recent launch epochs
guard_agent_relaunch() {
  load_config
  local id="$1" now t kept="" count=0
  now=$(date +%s)
  for t in ${AGENT_LAUNCHES[$id]:-} "$now"; do
    if [ $((now - t)) -le "$WINDOW_SECS" ]; then kept="$kept $t"; count=$((count + 1)); fi
  done
  AGENT_LAUNCHES[$id]="$kept"
  if [ "$count" -gt "$MAX_RELAUNCHES" ]; then
    log "agent '$id' crash-loop: $count relaunches in ${WINDOW_SECS}s — pausing ${PAUSE_SECS}s"
    "$CLIDECAR_HOME/bin/notify-discord.sh" \
      "⚠️ agent '$id' is crash-looping ($count relaunches/${WINDOW_SECS}s). Pausing ${PAUSE_SECS}s — check queue.md / \`clidecar logs\`." || true
    sleep "$PAUSE_SECS"
    AGENT_LAUNCHES[$id]=""
  fi
}

# A dev-channel modal that won't clear leaves the agent ALIVE but wedged (inbound stalled). reconcile
# never relaunches an alive agent, so the crash-loop guard structurally can't page for this — this is
# the only place that can. Throttle per id so a persistently-broken launch reminds every
# DEV_MODAL_ALERT_COOLDOWN_S (default 600s), not on every relaunch.
declare -A DEV_MODAL_ALERTED   # id → epoch of last stall page
alert_dev_modal_stall() {
  local id="$1" now last cooldown="${DEV_MODAL_ALERT_COOLDOWN_S:-600}"
  now=$(date +%s); last="${DEV_MODAL_ALERTED[$id]:-0}"
  [ $((now - last)) -ge "$cooldown" ] || return 0
  "$CLIDECAR_HOME/bin/notify-discord.sh" \
    "⚠️ agent '$id' is wedged on the dev-channel modal at launch — inbound is stalled. Attach the console (\`screen -r $(agent_screen "$id")\`) or check \`clidecar logs\`." || true
  DEV_MODAL_ALERTED[$id]="$now"
}

# Reconcile running agents to the fleet's desired state: launch enabled-but-dead, stop
# running-but-not-enabled. The SUCCESS of the enabled-list read is the "known-good" signal — only a
# successful read makes an empty list a real "all disabled" (safe to stop extras); a missing/broken
# manifest exits nonzero here, so we SKIP entirely (keep the live fleet) and alert once. This is the
# cardinal rule: a dead-because-unreadable manifest must never be read as "zero agents = stop all".
FLEET_WAS_OK=1
reconcile_agents() {
  local id enabled
  if ! enabled="$(fleet_enabled_agents)"; then
    log "fleet store unreadable — skipping reconcile (live agents preserved)"
    if [ "$FLEET_WAS_OK" = 1 ]; then
      "$CLIDECAR_HOME/bin/notify-discord.sh" \
        "⚠️ clidecar fleet store is unreadable — NOT reconciling (no agents stopped). Fix it; check \`clidecar logs\`." || true
      FLEET_WAS_OK=0
    fi
    return 0
  fi
  FLEET_WAS_OK=1
  for id in $enabled; do
    if ! agent_alive "$id"; then
      log "agent '$id' enabled but not alive — launching"
      guard_agent_relaunch "$id"
      launch_agent "$id"
    fi
  done
  for id in $(running_agents); do
    if ! printf '%s\n' $enabled | grep -qxF "$id"; then
      stop_agent "$id"
    fi
  done
}

# Recycle (fresh context) every enabled agent — used by the global RECYCLE flag and after a gateway
# reload (all shims must reattach to the new socket).
recycle_all_agents() {
  local id
  for id in $(fleet_enabled_agents); do
    graceful_kill_agent "$id"
    launch_agent "$id"
  done
}

# Same bound for the gateway daemon. It exits non-zero on an unrecoverable client (bad token,
# missing intent, link down past its watchdog) so the supervisor relaunches it — which means a
# persistent fault would tight-loop relaunches. This caps that and alerts.
GATEWAY_LAUNCH_TIMES=()
guard_gateway_relaunch() {
  load_config
  local now kept=() t
  now=$(date +%s)
  GATEWAY_LAUNCH_TIMES+=("$now")
  for t in "${GATEWAY_LAUNCH_TIMES[@]}"; do [ $((now - t)) -le "$WINDOW_SECS" ] && kept+=("$t"); done
  GATEWAY_LAUNCH_TIMES=("${kept[@]}")
  if [ "${#GATEWAY_LAUNCH_TIMES[@]}" -gt "$MAX_RELAUNCHES" ]; then
    log "gateway crash-loop: ${#GATEWAY_LAUNCH_TIMES[@]} relaunches in ${WINDOW_SECS}s — pausing ${PAUSE_SECS}s"
    "$CLIDECAR_HOME/bin/notify-discord.sh" \
      "⚠️ clidecar gateway daemon is crash-looping (${#GATEWAY_LAUNCH_TIMES[@]} relaunches/${WINDOW_SECS}s) — inbound is DOWN. Check \`clidecar gateway logs\`. Pausing ${PAUSE_SECS}s." || true
    sleep "$PAUSE_SECS"
    GATEWAY_LAUNCH_TIMES=()
  fi
}

wait_for_event() {
  load_config
  if command -v inotifywait >/dev/null 2>&1; then
    # -r so per-agent flags (agents/<id>/RECYCLE) wake the loop, not just top-level control flags.
    inotifywait -q -r -t "$POLL_SECS" -e create -e moved_to -e attrib "$CONTROL_DIR" >/dev/null 2>&1 || true
  else
    sleep "$POLL_SECS"
  fi
}

trap 'log "received TERM/INT — exiting (agents left running for adoption)"; exit 0' TERM INT

log "starting (CLIDECAR_HOME=$CLIDECAR_HOME)"
load_config
# Seed the fleet store from the legacy single-agent config if it's missing; a present store is left
# untouched. A broken/absent-and-unseedable store is surfaced by reconcile_agents below.
fleet_seed || log "fleet seed skipped/failed — ensure the fleet store exists (seed from config.env or \`clidecar agent\`)"
# One-time migration: a legacy single-agent claude recorded its pid at control/claude.pid. Adopt it as
# agent "main" by moving the pidfile into the per-agent path, so reconcile_agents below ADOPTS the
# live session instead of launching a duplicate beside it.
if [ -f "$CONTROL_DIR/claude.pid" ] && [ ! -f "$AGENTS_DIR/main/claude.pid" ]; then
  mkdir -p "$AGENTS_DIR/main"
  mv "$CONTROL_DIR/claude.pid" "$AGENTS_DIR/main/claude.pid"
  log "migrated legacy claude.pid → agents/main/claude.pid (adopting the live session as 'main')"
fi
# Gateway BEFORE agents: the daemon must be serving the broker socket before any shim attaches.
if gateway_enabled; then
  if gateway_alive; then
    log "adopted running gateway (pid=$(gateway_pid))"
  else
    launch_gateway
  fi
fi
reconcile_agents   # adopt already-running agents, launch enabled-but-dead ones

while true; do
  wait_for_event
  if [ -e "$CONTROL_DIR/GATEWAY_RELOAD" ]; then
    rm -f "$CONTROL_DIR/GATEWAY_RELOAD"
    log "GATEWAY_RELOAD requested"
    stop_gateway
    launch_gateway
    # The new daemon serves a fresh broker socket, but each live session's MCP stdio shim stays bound
    # to the OLD socket and CC can't reconnect a stdio server mid-session — so a bare daemon restart
    # strands inbound. Recycle ALL agents so their fresh shims attach to the new socket. BUT gate the
    # recycle on the daemon actually serving: `gateway reload` runs right after a gateway-core edit,
    # so a daemon that won't boot is the LIKELY case, and recycling into a dead socket is strictly
    # worse than the old gap. launch_gateway returns 0 unconditionally, so poll gateway_alive first.
    gw_up=0
    for _ in $(seq 1 "${GATEWAY_READY_SECS:-10}"); do
      if gateway_alive; then gw_up=1; break; fi
      sleep 1
    done
    if [ "$gw_up" = 1 ]; then
      log "gateway up after reload — recycling all agents to reattach their shims to the new socket"
      recycle_all_agents
    else
      # Keep the (healthy, checkpointable) live sessions rather than recycle blind. Notify via the
      # bot-API pinger — it doesn't depend on the gateway.
      log "gateway did NOT come up after reload — NOT recycling agents (live sessions preserved)"
      "$CLIDECAR_HOME/bin/notify-discord.sh" \
        "⚠️ gateway reload: daemon failed to restart — NOT recycling. Live sessions preserved but inbound is stranded (no daemon). Check \`clidecar gateway logs\`, fix bridge/gateway.py, then reload again." || true
    fi
    continue
  fi
  if [ -e "$CONTROL_DIR/RECYCLE" ]; then
    rm -f "$CONTROL_DIR/RECYCLE"
    log "RECYCLE (all agents) requested"
    recycle_all_agents     # deliberate recycle: not counted toward crash-loop
    continue
  fi
  # Per-agent recycle flags (clidecar recycle from inside an agent, or `recycle <id>` on the control
  # channel): kill + relaunch just that agent. reconcile_agents below then sees it alive (no-op).
  for f in "$AGENTS_DIR"/*/RECYCLE; do
    [ -e "$f" ] || continue
    rid="$(basename "$(dirname "$f")")"
    rm -f "$f"
    log "RECYCLE agent '$rid' requested"
    graceful_kill_agent "$rid"
    launch_agent "$rid"
  done
  if gateway_enabled && ! gateway_alive; then
    log "gateway not alive — relaunching"
    guard_gateway_relaunch
    launch_gateway
  fi
  reconcile_agents
done
