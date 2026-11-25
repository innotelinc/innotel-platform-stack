#!/bin/sh
# estate-notify-once.sh - throttle wrapper for estate-notify@.service.
#
# A unit that fails every 5 minutes must not page every 5 minutes. First failure in
# a 30-minute window is published; repeats are suppressed with a log line. The
# stamp lives in /var/lib/estate-notify/<unit>.stamp.
#
# It lives at /usr/local/sbin/estate-notify-once.sh on the hosts (see
# `systemd/estate-notify@.service`); this copy is the repository's record of it.
set -u
unit=${1:?usage: estate-notify-once.sh UNIT}
dir=/var/lib/estate-notify
mkdir -p "$dir" 2>/dev/null || true
safe=$(printf '%s' "$unit" | tr -c 'A-Za-z0-9@._:-' '_')
stamp="$dir/$safe.stamp"
now=$(date +%s)
last=0
[ -r "$stamp" ] && { read -r last < "$stamp" 2>/dev/null || true; }
case "${last:-0}" in ''|*[!0-9]*) last=0 ;; esac
if [ $((now - last)) -lt 1800 ]; then
  logger -t estate-notify -- "suppressed: $unit already notified $((now - last))s ago"
  exit 0
fi
printf '%s\n' "$now" > "$stamp"
notify=/root/platform-stack/scripts/estate-notify.sh
[ -x "$notify" ] || notify=/usr/local/sbin/estate-notify.sh
exec "$notify" "$unit"
