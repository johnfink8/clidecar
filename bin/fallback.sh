#!/usr/bin/env bash
# fallback.sh — OnFailure handler for clidecar.service.
# Fires when systemd gives up restarting the sidecar (StartLimit hit), which in
# practice means a freshly-edited sidecar.sh won't start. Restore the last
# known-good copy, stash the broken one, ping Discord, and try once more.
set -uo pipefail
CLIDECAR_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIDECAR_DATA="$HOME/.clidecar"
KG="$CLIDECAR_DATA/known-good/sidecar.sh"
LIVE="$CLIDECAR_HOME/bin/sidecar.sh"
NOTIFY="$CLIDECAR_HOME/bin/notify-discord.sh"

if [ -f "$KG" ] && ! diff -q "$KG" "$LIVE" >/dev/null 2>&1; then
  cp "$LIVE" "$CLIDECAR_DATA/known-good/sidecar.broken.$(date +%s)" 2>/dev/null || true
  cp "$KG" "$LIVE"
  "$NOTIFY" "🛟 Sidecar failed to start — restored known-good sidecar.sh (broken copy stashed) and restarting." || true
  systemctl --user reset-failed clidecar 2>/dev/null || true
  systemctl --user restart clidecar || true
else
  "$NOTIFY" "⚠️ Sidecar failed and the live copy already matches known-good — cannot auto-heal. Needs a human look (\`clidecar logs\`)." || true
fi
