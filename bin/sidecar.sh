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
claude_alive() { local p; p="$(claude_pid)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

launch_claude() {
  load_config
  # clear any stale screen session of the same name, then start fresh
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  rm -f "$PIDFILE"
  # the inner shell records its own PID then exec's claude, so PIDFILE == claude.
  # STARTUP_PROMPT (if set) is appended as claude's positional prompt and expanded
  # by the INNER shell from the (exported) environment, so its text never needs
  # shell-escaping here; an empty value is simply omitted (no blank prompt).
  local inner="cd '$WORKDIR' && echo \$\$ > '$PIDFILE' && exec $CLAUDE_BIN $CLAUDE_ARGS"
  if [ -n "${STARTUP_PROMPT:-}" ]; then
    inner="$inner \"\$STARTUP_PROMPT\""
  fi
  screen -dmS "$SCREEN_NAME" bash -lc "$inner"
  sleep 1
  log "launched claude in screen='$SCREEN_NAME' workdir='$WORKDIR' pid=$(claude_pid)"
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
if claude_alive; then
  log "adopted running claude (pid=$(claude_pid)) — not relaunching"
else
  guard_relaunch; launch_claude
fi

while true; do
  wait_for_event
  if [ -e "$CONTROL_DIR/RECYCLE" ]; then
    rm -f "$CONTROL_DIR/RECYCLE"
    log "RECYCLE requested"
    graceful_kill
    launch_claude          # deliberate recycle: not counted toward crash-loop
    continue
  fi
  if ! claude_alive; then
    log "claude not alive — relaunching"
    guard_relaunch
    launch_claude
  fi
done
