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
PIDFILE="$CONTROL_DIR/claude.pid"
GATEWAY_PIDFILE="$CONTROL_DIR/gateway.pid"
GATEWAY_SCRIPT="$CLIDECAR_HOME/bridge/gateway.py"
GATEWAY_LOG="$CLIDECAR_DATA/state/gateway-daemon.log"
CONFIG_FILE="$CLIDECAR_DATA/config.env"
KNOWN_GOOD="$CLIDECAR_DATA/known-good/sidecar.sh"
mkdir -p "$CONTROL_DIR" "$CLIDECAR_DATA/state" "$CLIDECAR_DATA/known-good"
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

claude_pid()   { cat "$PIDFILE" 2>/dev/null; }
claude_alive() {
  local p; p="$(claude_pid)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  # PID-reuse guard: after a reboot the recorded PID may now belong to an
  # unrelated process. Only treat it as our claude if the live process's cmdline
  # actually contains "claude" — else we'd falsely "adopt" a stranger and never
  # launch a fresh claude (a silent false-healthy). grep -a: cmdline is NUL-sep.
  grep -qa claude "/proc/$p/cmdline" 2>/dev/null
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
# stays transport-agnostic); fails LOUD (Discord ping) if the modal never shows or won't clear,
# rather than hanging silently. John's safety nets if inbound is down: console + remote-control.
confirm_dev_channel() {
  case "$CLAUDE_ARGS" in
    *dangerously-load-development-channels*) ;;
    *) return 0 ;;
  esac
  local snap tries=0 v
  snap="$(mktemp)"
  while [ "$tries" -lt 40 ]; do
    screen -S "$SCREEN_NAME" -X hardcopy "$snap" 2>/dev/null
    if grep -q "Loading development channels" "$snap" 2>/dev/null; then
      screen -S "$SCREEN_NAME" -X stuff $'\r'
      v=0
      while [ "$v" -lt 10 ]; do
        sleep 0.5
        screen -S "$SCREEN_NAME" -X hardcopy "$snap" 2>/dev/null
        grep -q "Loading development channels" "$snap" 2>/dev/null || {
          log "dev-channel modal auto-confirmed"; rm -f "$snap"; return 0; }
        v=$((v + 1))
      done
      log "dev-channel modal did NOT clear after confirm"
      "$CLIDECAR_HOME/bin/notify-discord.sh" \
        "⚠️ dev-channel modal didn't clear after auto-confirm — inbound may be stalled (console: screen -r $SCREEN_NAME)" || true
      rm -f "$snap"; return 1
    fi
    sleep 0.5; tries=$((tries + 1))
  done
  rm -f "$snap"
  log "dev-channel modal never appeared within timeout"
  "$CLIDECAR_HOME/bin/notify-discord.sh" \
    "⚠️ dev-channel modal never appeared — clidecar inbound may be down (console: screen -r $SCREEN_NAME)" || true
  return 1
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

launch_claude() {
  load_config
  wait_for_network
  # clear any stale screen session of the same name, then start fresh
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  rm -f "$PIDFILE"
  # the inner shell records its own PID then exec's claude, so PIDFILE == claude.
  # STARTUP_PROMPT (if set) is appended as claude's positional prompt and expanded
  # by the INNER shell from the (exported) environment, so its text never needs
  # shell-escaping here; an empty value is simply omitted (no blank prompt).
  # WORKDIR/PIDFILE/CLAUDE_BIN are %q-quoted so a path with a space or quote can't
  # break out of the inner command line; CLAUDE_ARGS stays unquoted on purpose so
  # it word-splits into separate flags. \$\$ stays literal for the inner shell.
  local inner
  inner="cd $(printf %q "$WORKDIR") && echo \$\$ > $(printf %q "$PIDFILE") && exec $(printf %q "$CLAUDE_BIN") $CLAUDE_ARGS"
  if [ -n "${STARTUP_PROMPT:-}" ]; then
    inner="$inner \"\$STARTUP_PROMPT\""
  fi
  screen -dmS "$SCREEN_NAME" bash -lc "$inner"
  sleep 1
  log "launched claude in screen='$SCREEN_NAME' workdir='$WORKDIR' pid=$(claude_pid)"
  confirm_dev_channel
}

graceful_kill() {
  load_config
  local p; p="$(claude_pid)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    log "SIGTERM claude ($p); grace ${GRACE_SECS}s"
    kill -TERM "$p" 2>/dev/null || true
    for _ in $(seq 1 "$GRACE_SECS"); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
    if kill -0 "$p" 2>/dev/null; then log "SIGKILL claude ($p)"; kill -KILL "$p" 2>/dev/null || true; fi
  fi
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  rm -f "$PIDFILE"
}

# --- crash-loop guard: bounds *unexpected* relaunches, not deliberate recycles ---
LAUNCH_TIMES=()
guard_relaunch() {
  load_config
  local now kept=() t
  now=$(date +%s)
  LAUNCH_TIMES+=("$now")
  for t in "${LAUNCH_TIMES[@]}"; do [ $((now - t)) -le "$WINDOW_SECS" ] && kept+=("$t"); done
  LAUNCH_TIMES=("${kept[@]}")
  if [ "${#LAUNCH_TIMES[@]}" -gt "$MAX_RELAUNCHES" ]; then
    log "crash-loop: ${#LAUNCH_TIMES[@]} relaunches in ${WINDOW_SECS}s — pausing ${PAUSE_SECS}s"
    "$CLIDECAR_HOME/bin/notify-discord.sh" \
      "⚠️ Managed Claude is crash-looping (${#LAUNCH_TIMES[@]} relaunches/${WINDOW_SECS}s). Pausing ${PAUSE_SECS}s — check queue.md / \`clidecar logs\`." || true
    sleep "$PAUSE_SECS"
    LAUNCH_TIMES=()
  fi
}

wait_for_event() {
  load_config
  if command -v inotifywait >/dev/null 2>&1; then
    inotifywait -q -t "$POLL_SECS" -e create -e moved_to -e attrib "$CONTROL_DIR" >/dev/null 2>&1 || true
  else
    sleep "$POLL_SECS"
  fi
}

trap 'log "received TERM/INT — exiting (claude left running for adoption)"; exit 0' TERM INT

log "starting (CLIDECAR_HOME=$CLIDECAR_HOME)"
load_config
# Gateway BEFORE claude: the daemon must be serving the broker socket before CC's shim attaches.
if gateway_enabled; then
  if gateway_alive; then
    log "adopted running gateway (pid=$(gateway_pid))"
  else
    launch_gateway
  fi
fi
if claude_alive; then
  log "adopted running claude (pid=$(claude_pid)) — not relaunching"
else
  guard_relaunch; launch_claude
fi

while true; do
  wait_for_event
  if [ -e "$CONTROL_DIR/GATEWAY_RELOAD" ]; then
    rm -f "$CONTROL_DIR/GATEWAY_RELOAD"
    log "GATEWAY_RELOAD requested"
    stop_gateway
    launch_gateway         # restart the daemon WITHOUT recycling Claude's context
    continue
  fi
  if [ -e "$CONTROL_DIR/RECYCLE" ]; then
    rm -f "$CONTROL_DIR/RECYCLE"
    log "RECYCLE requested"
    graceful_kill
    launch_claude          # deliberate recycle: not counted toward crash-loop
    continue
  fi
  if gateway_enabled && ! gateway_alive; then
    log "gateway not alive — relaunching"
    launch_gateway
  fi
  if ! claude_alive; then
    log "claude not alive — relaunching"
    guard_relaunch
    launch_claude
  fi
done
