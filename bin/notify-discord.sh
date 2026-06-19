#!/usr/bin/env bash
# notify-discord.sh "message" — post a message to the configured Discord channel
# via the bot API. Used by the sidecar/fallback, which run outside Claude and so
# cannot reach the Discord MCP. Best-effort: never fails the caller.
set -uo pipefail
CLIDECAR_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIDECAR_DATA="$HOME/.clidecar"
. "$CLIDECAR_DATA/config.env" 2>/dev/null || true
ENV_FILE="$HOME/.claude/channels/discord/.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

msg="${1:-(no message)}"
if [ -z "${DISCORD_BOT_TOKEN:-}" ] || [ -z "${DISCORD_CHANNEL_ID:-}" ]; then
  echo "notify: no token/channel configured; would have said: $msg" >&2
  exit 0
fi

payload="$(printf '%s' "$msg" | python3 -c 'import json,sys; print(json.dumps({"content": sys.stdin.read()}))')"
if curl -sf -X POST "https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages" \
     -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
     -H "Content-Type: application/json" \
     --data "$payload" >/dev/null; then
  echo "notify: sent"
else
  echo "notify: FAILED to post to Discord" >&2
fi
exit 0
