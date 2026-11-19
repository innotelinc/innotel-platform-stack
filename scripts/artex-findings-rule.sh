#!/usr/bin/env bash
# artex-findings-rule.sh - make ARTEX register a demonstrated disclosure as a finding.
#
# ARTEX keeps agent prompts in Postgres and renders them per round, so the rule
# lives in the database: it survives image rebuilds and is covered by the same
# backup as everything else. Applying it twice is a no-op.
#
#   ./artex-findings-rule.sh            apply (default agents: planner worker)
#   ./artex-findings-rule.sh --check    report whether the rule is present
#
# AGENTS="planner worker" API=... ENV_FILE=... override the defaults.
set -uo pipefail

API="${API:-http://127.0.0.1:8790}"
ENV_FILE="${ENV_FILE:-/root/artex.env}"
AGENTS="${AGENTS:-planner worker}"
MODE="${1:-apply}"
MARKER="[Estate rule: direct content disclosure]"
TOKEN=""

RULE='[Estate rule: direct content disclosure]
A scanner match that proves the target actually served a sensitive file — an exposed
/.git/config, /.env, database dump, backup archive, directory listing, or similar, with the
returned content in hand — is a demonstrated disclosure, not an inference and not one of the
"low-yield misconfigurations" this prompt tells you to skip. Register it with report_finding
once per distinct file or endpoint (severity per data class, evidence = the matched URL plus a
short excerpt of what was returned), and only then continue. Everything else in this prompt
still holds unchanged: version/CVE matching, "looks injectable", and external intelligence
never become findings on their own.'

[ -r "$ENV_FILE" ] || { echo "cannot read $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"
[ -n "${ARTEX_ADMIN_PASSWORD:-}" ] || { echo "ARTEX_ADMIN_PASSWORD missing" >&2; exit 1; }

TOKEN=$(curl -sf -X POST "$API/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"ARTEX\",\"password\":\"$ARTEX_ADMIN_PASSWORD\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))')
[ -n "$TOKEN" ] || { echo "login failed" >&2; exit 1; }

rc=0
for key in $AGENTS; do
  cur=$(curl -sf -H "Authorization: Bearer $TOKEN" "$API/api/agents/$key" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("prompt") or "",end="")')
  [ -n "$cur" ] || { echo "$key: no prompt returned" >&2; rc=1; continue; }

  if printf '%s' "$cur" | grep -qF "$MARKER"; then
    echo "$key: rule already present"
    continue
  fi
  if [ "$MODE" = "--check" ]; then
    echo "$key: rule MISSING"
    rc=1
    continue
  fi

  body=$(CUR="$cur" RULE="$RULE" python3 -c '
import json, os
template = os.environ["CUR"].rstrip() + "\n\n" + os.environ["RULE"] + "\n"
print(json.dumps({"template": template, "note": "estate: report direct content disclosure"}))')
  out=$(curl -s -X PUT "$API/api/agents/$key/prompt" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$body")
  case "$out" in
    *'"version"'*) echo "$key: rule applied ($out)" ;;
    *) echo "$key: apply failed: $out" >&2; rc=1 ;;
  esac
done
exit $rc
