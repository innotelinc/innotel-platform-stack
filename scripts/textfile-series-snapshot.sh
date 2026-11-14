#!/bin/bash
# Take a snapshot of the series that prove the metrics directory works, and prove that a
# move of it kept them.
#
# WHY THIS EXISTS
# ---------------
# On 2026-11-14 the node-exporter textfile directory left `/root`
# (`docs/root-paths.md`, `docs/textfile-dir-move.md`). It moved because it had to: it was
# under `/root` only because the Authentik dump beside it was, and a `/root` cleanup takes
# things silently. But a move of that directory has a failure shape nothing else notices —
# the writers keep writing, node-exporter keeps scraping, and the *series* that were there
# before are simply no longer nameable. `backup.yml` keys on the presence of
# `cerulean_authentik_backup_*` and `estate-checks.yml` keys on the presence of
# `innotel_estate_check_*`, so the two rule sets that would look like they should catch it
# are exactly the two that go quiet.
#
# So this takes the series (name and every label), twice, and says what stopped existing.
# "Everything still answers" is not the question: the question is whether any series that
# existed before the move exists after it.
#
# USAGE
# -----
#   ./scripts/textfile-series-snapshot.sh --snapshot /tmp/before.txt
#   ... move the directory, re-apply the dump and node-exporter ...
#   ./scripts/textfile-series-snapshot.sh --snapshot /tmp/after.txt
#   ./scripts/textfile-series-snapshot.sh --diff /tmp/before.txt /tmp/after.txt
#
#   PROMETHEUS_URL=http://127.0.0.1:9090 ./scripts/textfile-series-snapshot.sh ...
#
# Exit codes, because a snapshot that could not be taken must never read as "no change":
#   0  snapshot taken / nothing disappeared
#   1  series that existed in the first snapshot no longer exist (the move lost data)
#   2  could not run: no Prometheus, or an answer that is not a series list
set -u

PROM="${PROMETHEUS_URL:-http://127.0.0.1:9090}"

# Every family the textfile directory is responsible for: the Authentik dump the directory
# was named after, the estate checks that write into it, and the one scheduled job that
# publishes its own verdict (`scripts/omniroute-selfheal`).
MATCH='{__name__=~"cerulean_authentik_backup_.*|innotel_estate_check_.*|omniroute_selfheal.*"}'

usage() {
  sed -n '2,30p' "$0" | sed -e 's/^# \{0,1\}//'
  exit 2
}

# One series per line, identity only: `__name__` and every label, sorted, so two snapshots
# of the same estate are byte-identical and a difference is a real difference.
parse_series() {
  python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    body = json.loads(raw)
except ValueError:
    print("not JSON from Prometheus: " + raw[:200].strip(), file=sys.stderr)
    raise SystemExit(2)
if body.get("status") != "success" or not isinstance(body.get("data"), list):
    print("Prometheus did not answer with a series list: " + raw[:200].strip(), file=sys.stderr)
    raise SystemExit(2)
rows = []
for series in body["data"]:
    if not isinstance(series, dict):
        continue
    rows.append(",".join(f"{k}={series[k]}" for k in sorted(series)))
for row in sorted(rows):
    print(row)
'
}

snapshot() {
  local out="$1"
  local answer
  # `--data-urlencode` so the selector's `|` and braces reach Prometheus unmangled.
  answer="$(curl -sS -G --max-time 30 --data-urlencode "match[]=$MATCH" \
              "$PROM/api/v1/series" 2>/dev/null)" || {
    echo "textfile-series-snapshot: cannot reach Prometheus at $PROM" >&2
    return 2
  }
  printf '%s' "$answer" | parse_series > "$out.tmp" || { rm -f "$out.tmp"; return 2; }
  mv "$out.tmp" "$out"
  printf '%s: %s series\n' "$out" "$(wc -l < "$out")"
  if [ "$(wc -l < "$out")" -eq 0 ]; then
    # An empty estate and a broken selector look the same here, and both make a diff
    # meaningless: the second snapshot would "miss" nothing because the first had nothing.
    echo "textfile-series-snapshot: $PROM has no series matching $MATCH" >&2
    return 2
  fi
}

diff_snapshots() {
  local before="$1" after="$2"
  for f in "$before" "$after"; do
    [ -s "$f" ] || { echo "textfile-series-snapshot: $f is missing or empty" >&2; return 2; }
  done
  local gone
  gone="$(comm -23 <(sort -u "$before") <(sort -u "$after"))"
  local added
  added="$(comm -13 <(sort -u "$before") <(sort -u "$after"))"
  if [ -n "$added" ]; then
    printf 'added (%s):\n%s\n' "$(printf '%s\n' "$added" | wc -l)" "$added"
  fi
  if [ -z "$gone" ]; then
    printf 'nothing disappeared: %s series before, %s after\n' \
      "$(sort -u "$before" | wc -l)" "$(sort -u "$after" | wc -l)"
    return 0
  fi
  printf 'DISAPPEARED (%s) — the move lost these:\n%s\n' \
    "$(printf '%s\n' "$gone" | wc -l)" "$gone" >&2
  return 1
}

case "${1:-}" in
  --snapshot) [ $# -eq 2 ] || usage; snapshot "$2" ;;
  --diff)     [ $# -eq 3 ] || usage; diff_snapshots "$2" "$3" ;;
  *)          usage ;;
esac
