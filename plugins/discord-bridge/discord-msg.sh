#!/usr/bin/env bash
# discord-msg.sh — send/edit Discord messages via the bot API, for the hook layer
# that drives the ack→progress→final UX. Unlike notify-discord.sh (fire-and-forget),
# `send` prints the created message id on stdout so a later `edit` can target it.
#
#   discord-msg.sh send    "text" [reply_to_message_id]   -> prints message_id
#   discord-msg.sh edit    <message_id> "text"             -> edits in place (no ping)
#   discord-msg.sh react   <message_id> <emoji> [channel]  -> add the bot's reaction
#   discord-msg.sh unreact <message_id> <emoji> [channel]  -> remove the bot's reaction
#   discord-msg.sh latest                                  -> prints the newest message id
#
# Best-effort but HONEST: a failed API call exits non-zero and logs to stderr, so a
# caller (and John) can tell the bridge broke instead of silently losing output.
set -uo pipefail

CLIDECAR_DATA="$HOME/.clidecar"
. "$CLIDECAR_DATA/config.env" 2>/dev/null || true
ENV_FILE="$HOME/.claude/channels/discord/.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

if [ -z "${DISCORD_BOT_TOKEN:-}" ] || [ -z "${DISCORD_CHANNEL_ID:-}" ]; then
  echo "discord-msg: no token/channel configured" >&2
  exit 1
fi

API="https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages"
AUTH="Authorization: Bot ${DISCORD_BOT_TOKEN}"
CT="Content-Type: application/json"

# json_payload <text> [reply_to] — build the message body, optionally threaded.
json_payload() {
  CONTENT="$1" REPLYTO="${2:-}" python3 - <<'PY'
import json, os
body = {"content": os.environ["CONTENT"]}
rt = os.environ.get("REPLYTO") or ""
if rt:
    body["message_reference"] = {"message_id": rt}
print(json.dumps(body))
PY
}

cmd="${1:-}"
case "$cmd" in
  send)
    text="${2:-}"; reply_to="${3:-}"
    resp="$(curl -sf -X POST "$API" -H "$AUTH" -H "$CT" \
              --data "$(json_payload "$text" "$reply_to")")" || {
      echo "discord-msg: send FAILED" >&2; exit 1; }
    # A 2xx without an id is still a broken send — fail loud rather than crash opaquely.
    printf '%s' "$resp" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"]) if "id" in d else sys.exit("no id in response")' || {
      echo "discord-msg: send response had no id" >&2; exit 1; }
    ;;
  edit)
    mid="${2:?message_id required}"; text="${3:-}"
    curl -sf -X PATCH "${API}/${mid}" -H "$AUTH" -H "$CT" \
         --data "$(json_payload "$text")" >/dev/null || {
      echo "discord-msg: edit FAILED" >&2; exit 1; }
    ;;
  react|unreact)
    mid="${2:?message_id required}"; emoji="${3:?emoji required}"; chan="${4:-$DISCORD_CHANNEL_ID}"
    enc="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$emoji")"
    url="https://discord.com/api/v10/channels/${chan}/messages/${mid}/reactions/${enc}/@me"
    [ "$cmd" = react ] && method=PUT || method=DELETE
    curl -sf -X "$method" "$url" -H "$AUTH" >/dev/null || {
      echo "discord-msg: $cmd FAILED" >&2; exit 1; }
    ;;
  latest)
    resp="$(curl -sf "${API}?limit=1" -H "$AUTH")" || {
      echo "discord-msg: latest FAILED" >&2; exit 1; }
    printf '%s' "$resp" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d and "id" in d[0] else "")'
    ;;
  *)
    echo "usage: discord-msg.sh send \"text\" [reply_to] | edit <id> \"text\" | react|unreact <id> <emoji> [chan]" >&2
    exit 2
    ;;
esac
