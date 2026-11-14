#!/bin/sh
# regcache-credential-check.sh - does the pull-through cache still have a Hub identity?
#
# `regcache` (192.168.1.79:5000) proxies registry-1.docker.io with a Docker Hub
# personal access token in REGISTRY_PROXY_USERNAME/PASSWORD. When that token
# expires the cache does not stop: it falls back to anonymous pulls, keeps
# serving everything already cached, and returns `500 toomanyrequests` only for
# images it has never seen. By 2026-11-14 that had produced 123 such 500s and
# nobody noticed, because the images people use daily were all cached.
#
# So the credential is asked directly: a token request for `library/alpine`
# answers 200 when it authenticates and 401 when it is dead. No layers are
# pulled and nothing is written. The registry's own log is then read for the
# other symptom, `toomanyrequests`, which proves degradation even in the window
# before this check first ran.
#
# Exit: 0 the credential works and nothing failed on the anonymous limit
#       1 the credential is dead, or uncached pulls are already failing
#       2 could not run
set -u

INCUS=${INCUS:-regcache}
CONTAINER=${CONTAINER:-registry}
USERNAME=${DOCKERHUB_USERNAME:-innotel}
LOG_WINDOW=${LOG_WINDOW:-24h}
TOKEN_URL='https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull'

command -v incus >/dev/null 2>&1 || { echo "CANNOT RUN: incus is missing"; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "CANNOT RUN: curl is missing"; exit 2; }

VCMD="incus exec $INCUS -- docker"
$VCMD inspect "$CONTAINER" >/dev/null 2>&1 \
  || { echo "CANNOT RUN: no container $CONTAINER in $INCUS"; exit 2; }

# The environment file the rotation writes is preferred; before a rotation has
# run, the password exists only in the container's Config.Env - which is itself
# the exposure the rotation is meant to end. Either way it is read here and
# never printed.
env_lines=$(incus exec "$INCUS" -- sh -c 'cat /root/registry.env 2>/dev/null' 2>/dev/null || true)
if printf '%s\n' "$env_lines" | grep -q '^REGISTRY_PROXY_PASSWORD='; then
  source="/root/registry.env in $INCUS"
else
  env_lines=$($VCMD inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" | tr -d '\r')
  source="the container environment (never rotated into a file)"
fi

pass=$(printf '%s\n' "$env_lines" | sed -n 's/^REGISTRY_PROXY_PASSWORD=//p' | head -1)
user=$(printf '%s\n' "$env_lines" | sed -n 's/^REGISTRY_PROXY_USERNAME=//p' | head -1)
user=${user:-$USERNAME}
[ -n "$pass" ] || { echo "CANNOT RUN: no REGISTRY_PROXY_PASSWORD in $source"; exit 2; }
case "$pass" in
  *'"'*|*"\\"*) echo "CANNOT RUN: the stored token has a quote or backslash; refusing to re-express it"; exit 2 ;;
esac

token_file="${TMPDIR:-/tmp}/regcache-token.$$"
code=$(printf 'user = "%s:%s"\n' "$user" "$pass" | curl -sS -K - -o "$token_file" \
  -w '%{http_code}' --max-time 25 "$TOKEN_URL" 2>/dev/null)

# A 200 alone is not proof: the same endpoint answers 200 to a request with no
# credentials at all. What separates the two is the quota baked into the token -
# 200 pulls per 6 h for an authenticated account, 100 for anonymous - so the
# token's own claims are decoded and read. (Measured 2026-11-14: the stored token
# yields pull_limit 200 and the account name; a deliberately wrong secret gets
# 401 "incorrect username or password"; no credentials gets 200 with 100.)
limit=""
if [ "${code:-000}" = 200 ] && [ -s "$token_file" ]; then
  payload=$(sed -n 's/.*"token":"[^."]*\.\([^."]*\)\..*/\1/p' "$token_file" | head -1)
  case $(( ${#payload} % 4 )) in
    2) payload="$payload==" ;;
    3) payload="$payload=" ;;
  esac
  claims=$(printf '%s' "$payload" | tr '_-' '/+' | base64 -d 2>/dev/null || true)
  limit=$(printf '%s' "$claims" | sed -n 's/.*"pull_limit":"\([0-9]*\)".*/\1/p' | head -1)
fi
rm -f "$token_file"

rate=$( $VCMD logs --since "$LOG_WINDOW" "$CONTAINER" 2>&1 | grep -c 'toomanyrequests' || true )
rate=${rate:-0}

echo "credential: $source (user $user)"
echo "hub token request: HTTP ${code:-none}, pull_limit ${limit:-unknown}/6h"
echo "anonymous rate-limit errors in the last $LOG_WINDOW: $rate"

case "${code:-000}" in
  200) : ;;
  401|403)
    echo "result: Docker Hub rejected the registry's token -- every uncached pull now shares the anonymous limit"
    exit 1 ;;
  000|'')
    echo "CANNOT RUN: the Hub did not answer"
    exit 2 ;;
  *)
    echo "CANNOT RUN: the Hub answered HTTP $code to a token request"
    exit 2 ;;
esac

if [ "${limit:-}" != 200 ]; then
  echo "result: the Hub issued an anonymous token (pull_limit ${limit:-unknown}), so the registry's credential was not applied"
  exit 1
fi

if [ "$rate" -gt 0 ]; then
  echo "result: the token works, but $rate uncached pull(s) failed on Docker Hub's anonymous limit in the last $LOG_WINDOW"
  exit 1
fi

echo "result: the token authenticates and no uncached pull failed on the anonymous limit in the last $LOG_WINDOW"
exit 0
