#!/bin/sh
# Recover Stalwart's directory after a failed initialisation (2026-11-06).
#
# Installed at /usr/local/bin/stalwart-directory-watchdog.sh in the `mail`
# container, driven by stalwart-watchdog.timer. This file is the source of
# truth; deploy.sh copies it, the units beside it, and enables the timer.
#
# Stalwart 0.16.27's only directory is the Authentik OIDC one. It initialises
# once, and a failed discovery fetch is cached for the life of the process:
# from then on every bearer token answers HTTP 500 ("Directory is unavailable
# because it failed to initialize") and the webmail's SSO login fails *after* a
# correct Authentik sign-in. A restart is the only recovery, and no
# configuration change in this build avoids it.
#
# Restarting only when the IdP itself answers keeps an Authentik outage from
# becoming a restart loop, and the 15-minute floor keeps a restart that did not
# clear the fault from repeating every five minutes.
set -u

PROBE=http://127.0.0.1:8080/jmap/session
IDP=https://auth.cerulean.innotel.us/application/o/stalwart-webui/.well-known/openid-configuration
STATE=/var/lib/stalwart/.directory-watchdog

code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 \
	-H 'Authorization: Bearer directory-watchdog-probe' "$PROBE" 2>/dev/null)
[ "$code" = "500" ] || exit 0

idp=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$IDP" 2>/dev/null)
if [ "$idp" != "200" ]; then
	logger -t stalwart-watchdog "auth backend 500 but IdP answers ${idp:-nothing}; not restarting"
	exit 0
fi

last=$(cat "$STATE" 2>/dev/null || echo 0)
now=$(date +%s)
case "$last" in ''|*[!0-9]*) last=0 ;; esac
if [ $((now - last)) -lt 900 ]; then
	logger -t stalwart-watchdog "auth backend 500; last recovery was less than 15 minutes ago, waiting"
	exit 0
fi
echo "$now" > "$STATE"

logger -t stalwart-watchdog "auth backend 500 with the IdP reachable; restarting stalwart"
systemctl restart stalwart
sleep 6
after=$(curl -s -o /dev/null -w '%{http_code}' -m 5 \
	-H 'Authorization: Bearer directory-watchdog-probe' "$PROBE" 2>/dev/null)
logger -t stalwart-watchdog "after restart: HTTP ${after:-no-answer} (401 or 200 is healthy)"
