#!/bin/sh
# estate-notify.sh - put a failed unit in front of a person, on the estate's ntfy.
#
# Usage: estate-notify.sh UNIT-NAME
#
# Wired as a systemd template, so no unit has to know how the channel works:
#
#   [Unit]
#   OnFailure=estate-notify@%n.service
#
# and the template's ExecStart calls this with `%i`, the unit that just failed.
# It reads the unit's own verdict (`systemctl show`), takes the last lines of its
# journal as the message, and POSTs them to the estate's ntfy.
#
# The channel is `proxy`'s ntfy (binwiederhier/ntfy, host networking): it listens
# on 192.168.1.71:2586, NOT on :8080 - the estate's other web services answer
# there, which is why a probe of :8080 said "nothing is running" for a month while
# ntfy had been up for 31 h. It runs with `--auth-default-access deny-all`, so
# publishing needs the dedicated user; its credentials live in
# /etc/innotel/ntfy.env (mode 600, never in this repository).
#
# Exit: 0 published, 1 the POST failed, 2 could not run (no config, no curl).
set -u

ENV_FILE=${NTFY_ENV_FILE:-/etc/innotel/ntfy.env}

[ -r "$ENV_FILE" ] || { echo "CANNOT RUN: $ENV_FILE is not readable"; exit 2; }
# shellcheck disable=SC1090
. "$ENV_FILE"

URL=${NTFY_URL:-http://192.168.1.71:2586}
TOPIC=${NTFY_TOPIC:-}
[ -n "$TOPIC" ] || { echo "CANNOT RUN: NTFY_TOPIC is not set in $ENV_FILE"; exit 2; }
if [ -z "${NTFY_USER:-}" ] || [ -z "${NTFY_PASSWORD:-}" ]; then
  echo "CANNOT RUN: NTFY_USER/NTFY_PASSWORD are not set in $ENV_FILE"
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "CANNOT RUN: curl is missing"; exit 2; }

unit=${1:-}
[ -n "$unit" ] || { echo "CANNOT RUN: no unit name given"; exit 2; }
case "$unit" in
  *[!A-Za-z0-9@._:-]*) echo "CANNOT RUN: '$unit' does not look like a unit name"; exit 2 ;;
esac

result=$(systemctl show -p Result --value "$unit" 2>/dev/null)
status=$(systemctl show -p ExecMainStatus --value "$unit" 2>/dev/null)
body=$(journalctl -u "$unit" -n 25 --no-pager -o cat 2>/dev/null | tail -c 3000)
[ -n "$body" ] || body="(the unit left no journal lines)"

title="estate: $unit failed (${result:-unknown}${status:+, rc $status})"
[ -n "${result:-}" ] || title="estate: $unit failed"

# The password goes to curl through a config file on stdin, so it never appears
# in `ps` and never on a command line a shell would log.
code=$(
  printf 'user = "%s:%s"\n' "$NTFY_USER" "$NTFY_PASSWORD" |
    curl -sS --max-time 20 -K - -o /dev/null -w '%{http_code}' \
      -H "Title: $title" -H "Priority: high" -H "Tags: rotating_light" \
      -H "Content-Type: text/plain" \
      --data-binary "$body" "$URL/$TOPIC" 2>/dev/null
)

case "${code:-000}" in
  200) echo "notified: $TOPIC <- $unit" ;;
  000|'') echo "FAILED: could not reach $URL (no answer)" >&2; exit 1 ;;
  401|403) echo "FAILED: $URL refused the credential (HTTP $code) -- check $ENV_FILE" >&2; exit 1 ;;
  *) echo "FAILED: $URL answered HTTP $code" >&2; exit 1 ;;
esac
