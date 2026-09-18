#!/usr/bin/env bash
# check-sign-in-posture.sh — run every zone's sign-in test in one pass.
#
# Each repo carries its own `scripts/verify-sso.py` for the reasons in
# docs/sign-in-posture.md: the checks are specific to that deployment (Cerulean's
# edge and Vault, Capstone's six gateways, Monarch's thirteen media gateways,
# Signara's API, Olympus's Studio, Distro's OIDC-native console) and the person
# who changes one of them should not have to reason about the others. What was
# missing is a single command that runs all of them, which is this file — the
# estate-wide half, kept in the estate-wide repo.
#
# ONE CAVEAT, because it reads as a zone failure and is not one: a zone's script
# asserts things about the host that runs that zone (that the apps answer on
# loopback, that the store answers where its gateways dial). Running it from
# another host reports those host-local checks as failures. Run a zone's script
# on that zone's own host when you want its full result; use this runner for the
# fleet-wide picture.
#
# It is a runner, not a re-implementation: each zone's script owns its assertions
# and its own exit codes, and this only reports what they said.
#
# Usage:
#   ./scripts/check-sign-in-posture.sh                  # every zone
#   ./scripts/check-sign-in-posture.sh --only olympus capstone
#   ./scripts/check-sign-in-posture.sh --list
#
# Exit codes: 0 = every zone passed (skips are allowed), 1 = at least one FAILED.
# A zone whose own script exits 2 is reported SKIP, not FAIL: exit 2 means "I
# cannot run" (unconfigured, or the deployment is unreachable from here), which is
# a different fact from "the deployment is broken" and must not be reported as one.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# zone|repo-relative path to its current-directory-sensitive script
ZONES=(
  "cerulean|1-primary/cerulean"
  "magnate|1-primary/magnate"
  "signara|1-primary/signara"
  "capstone|2-voice/capstone"
  "monarch|3-media/monarch"
  "olympus|5-dev/olympus"
  # Distro joined when its console was made Authentik-only; it is OIDC-native
  # (no oauth2-proxy gateway), so its test asserts the issuer handshake and a
  # real code flow rather than the gateway hop the six above do.
  "distro|5-dev/distro"
)

SCRIPT="scripts/verify-sso.py"
SELFTEST="scripts/tests/test_verify_sso.py"

ONLY=()
LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only) shift; while [[ $# -gt 0 && "$1" != --* ]]; do ONLY+=("$1"); shift; done ;;
    --list) printf '%s\n' "${ZONES[@]%%|*}"; exit 0 ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'check-sign-in-posture: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

selected() {
  [[ ${#ONLY[@]} -eq 0 ]] && return 0
  local want
  for want in "${ONLY[@]}"; do [[ "$want" == "$1" ]] && return 0; done
  return 1
}

printf 'Sign-in posture — %s\n\n' "$ROOT"
printf '%-10s %-8s %s\n' ZONE RESULT DETAIL
printf '%-10s %-8s %s\n' ---- ------ ------

failed=0
ran=0
skipped=0

for entry in "${ZONES[@]}"; do
  zone="${entry%%|*}"
  rel="${entry##*|}"
  dir="$ROOT/$rel"
  selected "$zone" || continue

  if [[ ! -f "$dir/$SCRIPT" ]]; then
    printf '%-10s %-8s %s\n' "$zone" "MISSING" "$rel/$SCRIPT"
    failed=$((failed + 1))
    continue
  fi

  # The scripts read `.env` relative to their own repo, so the working directory
  # matters and is set explicitly rather than inherited.
  log="$LOG_DIR/$zone.log"
  ( cd "$dir" && timeout 900 python3 "$SCRIPT" ) >"$log" 2>&1
  code=$?
  ran=$((ran + 1))

  case "$code" in
    0)
      detail="$(grep -cE 'PASS' "$log") check(s) passed"
      printf '%-10s %-8s %s\n' "$zone" "PASS" "$detail"
      ;;
    2)
      skipped=$((skipped + 1))
      printf '%-10s %-8s %s\n' "$zone" "SKIP" "$(grep -m1 -E '^SKIP' "$log" | cut -c1-70)"
      ;;
    124)
      failed=$((failed + 1))
      printf '%-10s %-8s %s\n' "$zone" "TIMEOUT" "no result within 900s — see $log"
      ;;
    *)
      failed=$((failed + 1))
      printf '%-10s %-8s %s\n' "$zone" "FAIL" "$(grep -m1 -E 'FAIL|check\(s\) failed' "$log" | cut -c1-70)"
      printf '\n── %s (last 40 lines of %s) ──\n' "$zone" "$log"
      tail -40 "$log" | sed 's/^/   /'
      ;;
  esac
done

printf '\n%d zone(s) run, %d skipped, %d failed\n' "$ran" "$skipped" "$failed"

# A zone with no test at all is a posture gap, not a pass.
for entry in "${ZONES[@]}"; do
  zone="${entry%%|*}"; rel="${entry##*|}"
  if [[ -f "$ROOT/$rel/$SELFTEST" ]]; then
    ( cd "$ROOT/$rel" && python3 "$SELFTEST" >/dev/null 2>&1 ) \
      || printf 'note: %s has a script self-test that is not passing\n' "$zone"
  fi
done

[[ "$failed" -eq 0 ]] || exit 1
