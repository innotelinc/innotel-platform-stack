#!/usr/bin/env bash
# up.sh — start the christ staging stack with the telephony guard enforced.
#
# Why this exists
# ---------------
# The overlay parks `gateway`, `telno` and `fax-engine` on a profile called
# "never-pstn". That stops every *named* profile from starting them, but
# `--profile '*'` activates every profile in the merged config — including that
# one. Measured on 2026-11-16: with `--profile '*'`, `compose config --services`
# returned all three forbidden services. A comment cannot prevent that; a wrapper
# can.
#
# So: this refuses the wildcard, then proves the exclusion by asking compose what
# it would actually start, and only then runs up.
#
#   ./up.sh core
#   ./up.sh core console agents
#
# Environment overrides: PROJECT, ENV_FILE, BASE_COMPOSE, OVERLAY

set -euo pipefail

PROJECT=${PROJECT:-christ-staging}
ENV_FILE=${ENV_FILE:-.env.staging}
BASE=${BASE_COMPOSE:-/opt/jesus/christ/compose.yml}
OVERLAY=${OVERLAY:-$(cd "$(dirname "$0")" && pwd)/compose.staging.yml}
FORBIDDEN='^(gateway|telno|fax-engine)$'

die() { printf 'up.sh: %s\n' "$*" >&2; exit 2; }

[ $# -ge 1 ] || die "usage: ./up.sh <profile> [profile...] — never '*'"

for a in "$@"; do
  case "$a" in
    *'*'*) die "refusing: a wildcard profile activates 'never-pstn' and would start the SIP border" ;;
  esac
done

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found (copy .env.staging.example)"
[ -f "$BASE" ]     || die "upstream compose not found at $BASE — set BASE_COMPOSE"
[ -f "$OVERLAY" ]  || die "overlay not found at $OVERLAY"

# The three fail-closed variables that are actually interpolated for this stack.
for v in CANTUS_SECRET CANTUS_CONTROL_TOKEN FAX_SECRET; do
  grep -qE "^${v}=.+" "$ENV_FILE" || die "$v is not set in $ENV_FILE"
done

args=(-p "$PROJECT" --env-file "$ENV_FILE" -f "$BASE" -f "$OVERLAY")
for p in "$@"; do args+=(--profile "$p"); done

tree=$(docker compose "${args[@]}" config --services) || die "compose config failed"

if printf '%s\n' "$tree" | grep -qE "$FORBIDDEN"; then
  printf '%s\n' "$tree" | grep -E "$FORBIDDEN" | sed 's/^/  resolved: /' >&2
  die "refusing to start: a telephony service is in the resolved tree"
fi

printf 'up.sh: profiles [%s] -> %s services\n' "$*" "$(printf '%s\n' "$tree" | wc -l)"
docker compose "${args[@]}" up -d
