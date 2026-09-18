#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# audit-attribution.sh — audit attribution across the stack, on demand.
#
# The attribution guard (`.githooks/guard-lib`, `attribution-guard.yml`) is a
# gate: it checks the commits and the file lines a push introduces, and it
# checks PR text. That is the right thing to gate on, and it leaves one blind
# spot — a violation already *in* history is only re-examined when something
# makes the guard scan a wider range (a branch pushed for the first time scans
# the full history; an ordinary push scans only its own commits). A trailer can
# therefore sit in a repository for months and then block a release branch.
#
# This script closes that blind spot: it audits history directly, with the same
# policy the repos enforce, and reports every offending commit with a hash, a
# date and the guard's own reason. It never writes. Fixing history is a
# deliberate, reviewed act (rewrite the message, then force-push with a lease),
# which is not something an audit should do behind your back.
#
# Usage:
#   scripts/audit-attribution.sh [options] [repo-dir ...]
#   scripts/audit-attribution.sh --org                 # every repo in the org
#   scripts/audit-attribution.sh --selftest            # prove the audit fires
#
# Options:
#   --org [owner]     audit every repository of the org that carries the guard
#                     (default owner: this repo's git remote). Needs `gh`.
#   --dir DIR         where --org keeps its clones (default: a temp dir)
#   --since DATE      only commits after DATE (default: the whole history)
#   --limit N         scan at most N commits per repo, newest first (default 500)
#   --content         also scan tracked file contents, not just messages
#   --json            machine-readable report on stdout
#   --quiet           one summary line per repository, nothing else
#   --selftest        build a clean and a violating fixture repo and assert the
#                     audit clears one and flags the other
#   -h, --help        this text
#
# Exit: 0 clean · 1 violations found · 2 usage or scan error
#
# The audit is a thin layer over the guard: `guard_check_stream` decides, so the
# tool cannot disagree with the local hooks or with CI, and no pattern is
# duplicated here.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CANON_GUARD="$ROOT/.githooks/guard-lib"

MODE="dirs"
OWNER=""
WORK_DIR=""
SINCE=""
LIMIT=500
SCAN_CONTENT=0
AS_JSON=0
QUIET=0

usage() { sed -n '2,41p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
say()  { printf '%s\n' "$*"; }
warn() { printf 'audit-attribution: %s\n' "$*" >&2; }
die()  { printf 'audit-attribution: error: %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --org)
      MODE="org"
      # Optional owner: `--org innotelinc`, or a bare `--org`.
      if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then OWNER="$2"; shift; fi
      ;;
    --dir)      WORK_DIR="${2:-}"; shift ;;
    --since)    SINCE="${2:-}"; shift ;;
    --limit)    LIMIT="${2:-}"; shift ;;
    --content)  SCAN_CONTENT=1 ;;
    --json)     AS_JSON=1 ;;
    --quiet)    QUIET=1 ;;
    --selftest) MODE="selftest" ;;
    -h|--help)  usage; exit 0 ;;
    -*)         die "unknown option: $1 (try --help)" ;;
    *)          break ;;
  esac
  shift
done

# ── policy ─────────────────────────────────────────────────────────────────
# The audited repository's own guard is preferable: it is the policy that repo
# enforces locally and in CI. The canonical copy here is the fallback for a
# checkout that has not synced it yet, and drift is reported rather than hidden.
# The policy a repo enforces is the one in its own tree at the ref being
# audited — including a partial clone with no working tree, where the file has
# to come out of the object store. Only a repo that carries no guard at all
# falls back to the canonical copy here.
load_guard() { # <repo-dir> [ref]
  local dir="$1" ref="${2:-HEAD}" candidate=""
  if [ -n "$(git -C "$dir" rev-parse --verify --quiet "$ref:.githooks/guard-lib" 2>/dev/null)" ]; then
    candidate="$(mktemp "${TMPDIR:-/tmp}/guard-lib.XXXXXX")"
    git -C "$dir" show "$ref:.githooks/guard-lib" >"$candidate" 2>/dev/null || candidate=""
  elif [ -f "$dir/.githooks/guard-lib" ]; then
    candidate="$dir/.githooks/guard-lib"
  fi
  [ -n "$candidate" ] || candidate="$CANON_GUARD"
  # shellcheck source=/dev/null
  source "$candidate"
}

# ── helpers ────────────────────────────────────────────────────────────────
default_ref() { # <repo-dir> -> a ref that resolves
  local dir="$1" ref
  for ref in "$(git -C "$dir" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)" \
              origin/main origin/master HEAD; do
    [ -n "$ref" ] || continue
    git -C "$dir" rev-parse --verify --quiet "$ref" >/dev/null 2>&1 && { printf '%s' "$ref"; return 0; }
  done
  return 1
}

# Tracked files worth scanning: source, config and docs. An image cannot carry a
# credit line and a package manager's output is noise, so both are skipped.
TRACKED_RE='\.(md|txt|rst|yml|yaml|toml|json|cfg|ini|py|ts|tsx|js|jsx|mjs|cjs|sh|bash|ps1|psm1|html|css|scss|go|rs|rb|php|java|kt|swift|sql|tf|hcl|env|example)$|(^|/)(Dockerfile|Makefile|LICENSE|NOTICE)$'
tracked_ok() {
  case "$1" in
    */node_modules/*|*/vendor/*|*/dist/*|*/build/*|*/.venv/*|*/site-packages/*) return 1 ;;
  esac
  grep -Eq "$TRACKED_RE" <<<"$1"
}

HEADER_PRINTED=0
quiet_header() {
  [ "$HEADER_PRINTED" -eq 1 ] && return 0
  # Header, then verdict column: REPO commits violations guard. Called from the
  # first audit so it lands after any intro line, in both modes.
  printf '%-30s %-9s %-12s %s\n' REPO COMMITS VIOLATIONS GUARD
  HEADER_PRINTED=1
}

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/}"
  printf '%s' "$s"
}

# ── tallies ────────────────────────────────────────────────────────────────
REPOS_SCANNED=0
COMMITS_SCANNED=0
FILES_SCANNED=0
VIOLATION_COMMITS=0
VIOLATION_FILES=0
JSON_ROWS=""
TRUNCATED_REPOS=""

row() { # append a JSON object to the report
  JSON_ROWS="${JSON_ROWS:+$JSON_ROWS,}$1"
}

# ── the audit itself ───────────────────────────────────────────────────────
audit_repo() { # <repo-dir> [label]
  local dir="$1" label="${2:-}" ref
  [ -d "$dir/.git" ] || { warn "not a git checkout: $dir"; return 0; }
  label="${label:-$(basename "$dir")}"

  if ! ref="$(default_ref "$dir")"; then
    warn "$label: no ref to scan"
    return 0
  fi

  local guard_used="canonical" repo_guard_blob
  repo_guard_blob="$(git -C "$dir" rev-parse --verify --quiet "$ref:.githooks/guard-lib" 2>/dev/null)"
  if [ -n "$repo_guard_blob" ]; then
    if [ "$repo_guard_blob" = "$(git hash-object "$CANON_GUARD")" ]; then
      guard_used="repo"
    else
      guard_used="repo (DRIFTED)"
    fi
  fi
  load_guard "$dir" "$ref" || die "could not load a guard policy for $label"

  local -a rev_args=(rev-list --max-count="$LIMIT")
  [ -n "$SINCE" ] && rev_args+=(--since="$SINCE")

  local sha msg reason sha_short date subject bad=0 repo_commits=0
  while read -r sha; do
    [ -n "$sha" ] || continue
    COMMITS_SCANNED=$((COMMITS_SCANNED + 1))
    repo_commits=$((repo_commits + 1))
    msg="$(git -C "$dir" log -1 --format=%B "$sha" 2>/dev/null)" || continue
    reason="$(guard_check_stream 2>&1 <<<"$msg")" && continue
    bad=$((bad + 1))
    VIOLATION_COMMITS=$((VIOLATION_COMMITS + 1))
    [ "$QUIET" -eq 1 ] && continue
    sha_short="${sha:0:9}"
    date="$(git -C "$dir" log -1 --format=%ad --date=short "$sha")"
    subject="$(git -C "$dir" log -1 --format=%s "$sha")"
    if [ "$AS_JSON" -eq 1 ]; then
      row "$(printf '{"repo":"%s","kind":"commit","sha":"%s","date":"%s","subject":"%s","reason":"%s"}' \
        "$(json_escape "$label")" "$sha" "$date" "$(json_escape "$subject")" "$(json_escape "$reason")")"
    else
      say "  ✗ $sha_short  $date  $subject"
      printf '%s\n' "$reason" | sed 's/^/      /'
    fi
  done < <(git -C "$dir" "${rev_args[@]}" "$ref" 2>/dev/null)

  # ── file contents (opt-in: it is the slow half) ──────────────────────────
  local files_bad=0 path
  if [ "$SCAN_CONTENT" -eq 1 ]; then
    while read -r path; do
      [ -n "$path" ] || continue
      tracked_ok "$path" || continue
      FILES_SCANNED=$((FILES_SCANNED + 1))
      reason="$(git -C "$dir" show "$ref:$path" 2>/dev/null | guard_check_stream 2>&1)" && continue
      files_bad=$((files_bad + 1))
      VIOLATION_FILES=$((VIOLATION_FILES + 1))
      [ "$QUIET" -eq 1 ] && continue
      if [ "$AS_JSON" -eq 1 ]; then
        row "$(printf '{"repo":"%s","kind":"file","path":"%s","reason":"%s"}' \
          "$(json_escape "$label")" "$(json_escape "$path")" "$(json_escape "$reason")")"
      else
        say "  ✗ $path"
        printf '%s\n' "$reason" | sed 's/^/      /'
      fi
    done < <(git -C "$dir" ls-tree -r --name-only "$ref" 2>/dev/null)
  fi

  REPOS_SCANNED=$((REPOS_SCANNED + 1))
  [ "$QUIET" -eq 1 ] && [ "$AS_JSON" -eq 0 ] && quiet_header

  # A limit that silently truncates would turn an audit into a false clean, so
  # say what was left unscanned, loudly, in every mode.
  local total
  total="$(git -C "$dir" rev-list --count "$ref" 2>/dev/null || printf '%s' "$repo_commits")"
  local scan_note=""
  if [ "$repo_commits" -lt "$total" ]; then
    scan_note=" — scanned newest $repo_commits of $total; raise --limit or narrow with --since"
    TRUNCATED_REPOS+=" $label"
  fi

  if [ "$QUIET" -eq 1 ]; then
    printf '%-30s %-9s %-12s %s\n' "$label" "$repo_commits/$total" \
      "$bad commit(s)$([ "$files_bad" -gt 0 ] && printf ', %s file(s)' "$files_bad")" "$guard_used"
  elif [ "$AS_JSON" -eq 0 ]; then
    if [ "$bad" -eq 0 ] && [ "$files_bad" -eq 0 ]; then
      say "  ✓ $label: clean (guard: $guard_used)$scan_note"
    else
      say "  · $label: $bad commit(s) and $files_bad file(s) to fix — shown above$scan_note"
    fi
  fi
}

# ── org mode ───────────────────────────────────────────────────────────────
audit_org() {
  command -v gh >/dev/null 2>&1 || die "--org needs the gh CLI"
  if [ -z "$OWNER" ]; then
    OWNER="$(git -C "$ROOT" remote get-url origin 2>/dev/null | sed -E 's#.*[:/]([^/]+)/[^/]+(\.git)?$#\1#')"
  fi
  [ -n "$OWNER" ] || die "could not work out the org; pass it: --org <owner>"
  [ -n "$WORK_DIR" ] || WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/audit-attribution.XXXXXX")"
  mkdir -p "$WORK_DIR"

  say "scanning every $OWNER repo that carries the guard (clones under $WORK_DIR)"
  local name dir
  # Only repos that carry the policy are in scope: a fork's upstream history is
  # not ours to police, and the guard is the marker that a repo is ours.
  while read -r name; do
    [ -n "$name" ] || continue
    gh api "repos/$OWNER/$name/contents/.githooks/guard-lib" --jq '.name' >/dev/null 2>&1 || continue
    dir="$WORK_DIR/$name"
    if [ ! -d "$dir/.git" ]; then
      git clone --quiet --filter=blob:none --no-checkout "https://github.com/$OWNER/$name.git" "$dir" >/dev/null 2>&1 \
        || { warn "$name: clone failed"; continue; }
    else
      git -C "$dir" fetch --quiet --filter=blob:none --force origin \
        '+refs/heads/*:refs/remotes/origin/*' >/dev/null 2>&1
    fi
    audit_repo "$dir" "$name"
  done < <(gh repo list "$OWNER" --limit 200 --json name --jq '.[].name' 2>/dev/null)
}

# ── selftest ───────────────────────────────────────────────────────────────
# An audit that never fires is worse than none: build one fixture repo that
# violates the policy and one that does not, and require the audit to tell them
# apart. The fixture text is assembled from the guard's own token at runtime, so
# this file never contains a literal attribution.
selftest() {
  local tmp tool trailer
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/audit-selftest.XXXXXX")"
  tool="${TOOL_TOKEN%%|*}"
  trailer="Co-Authored-By: ${tool^} <noreply@${tool}.com>"

  local clean="$tmp/clean-repo" bad="$tmp/bad-repo" dir
  for dir in "$clean" "$bad"; do
    mkdir -p "$dir/.githooks"
    git -C "$dir" init -q
    git -C "$dir" config user.email "selftest@example.com"
    git -C "$dir" config user.name "Selftest"
    cp "$CANON_GUARD" "$dir/.githooks/guard-lib"
    printf 'An ordinary repository file.\n' >"$dir/README.md"
  done
  git -C "$clean" add -A && git -C "$clean" commit -qm "Add the readme"
  git -C "$bad" add -A
  git -C "$bad" commit -qm "$(printf 'Add the readme\n\nGenerated with %s\n%s\n' "${tool^}" "$trailer")"

  local failures=0 mark
  mark="$VIOLATION_COMMITS"
  audit_repo "$clean" "clean-fixture" >/dev/null 2>&1
  if [ "$VIOLATION_COMMITS" -ne "$mark" ]; then
    say "selftest: FAIL — the audit flagged a clean repository"
    failures=$((failures + 1))
  fi
  mark="$VIOLATION_COMMITS"
  audit_repo "$bad" "violating-fixture" >/dev/null 2>&1
  if [ "$VIOLATION_COMMITS" -le "$mark" ]; then
    say "selftest: FAIL — the audit passed a repository with a violating commit"
    failures=$((failures + 1))
  fi
  rm -rf "$tmp"

  if [ "$failures" -eq 0 ]; then
    say "selftest: ok — a violating repository is flagged, a clean one is cleared"
    return 0
  fi
  say "selftest: $failures failure(s)"
  return 1
}

# ── run ────────────────────────────────────────────────────────────────────
[ -f "$CANON_GUARD" ] || die "canonical policy not found at $CANON_GUARD"

case "$MODE" in
  selftest)
    load_guard "$ROOT" || die "no guard-lib to load"
    selftest || exit 1
    exit 0
    ;;
  org)
    [ "$AS_JSON" -eq 0 ] && [ "$QUIET" -eq 0 ] && say "attribution audit — org mode"
    audit_org
    ;;
  *)
    [ "$AS_JSON" -eq 0 ] && [ "$QUIET" -eq 0 ] && say "attribution audit"
    if [ $# -eq 0 ]; then
      # No arguments means "audit the repository I am standing in" — the caller's
      # working directory, not this script's own checkout, which is what makes
      # `cd ../some-repo && ../ips/scripts/audit-attribution.sh` do the obvious
      # thing. Fall back to this checkout when neither is a git repository.
      set -- "$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$ROOT")"
    fi
    for target in "$@"; do
      audit_repo "$target"
    done
    ;;
esac

# ── report ─────────────────────────────────────────────────────────────────
if [ "$AS_JSON" -eq 1 ]; then
  printf '{"repos":%d,"commits_scanned":%d,"files_scanned":%d,"violating_commits":%d,"violating_files":%d,"violations":[%s]}\n' \
    "$REPOS_SCANNED" "$COMMITS_SCANNED" "$FILES_SCANNED" "$VIOLATION_COMMITS" "$VIOLATION_FILES" "$JSON_ROWS"
fi

if [ -n "$TRUNCATED_REPOS" ] && [ "$AS_JSON" -eq 0 ]; then
  say ""
  say "! not fully scanned (--limit $LIMIT):$TRUNCATED_REPOS"
  say "  older commits in those repos were not examined — a clean result here is partial"
fi

if [ "$VIOLATION_COMMITS" -eq 0 ] && [ "$VIOLATION_FILES" -eq 0 ]; then
  if [ "$AS_JSON" -eq 0 ]; then
    say "attribution audit: clean — $COMMITS_SCANNED commit(s) across $REPOS_SCANNED repo(s)$([ "$SCAN_CONTENT" -eq 1 ] && printf ', %s file(s)' "$FILES_SCANNED")"
  fi
  exit 0
fi

if [ "$AS_JSON" -eq 0 ]; then
  say ""
  say "attribution audit: $VIOLATION_COMMITS commit(s)$([ "$VIOLATION_FILES" -gt 0 ] && printf ' and %s file(s)' "$VIOLATION_FILES") to fix — only Darnel Hunter <dhunter@innotel.us> may be credited"
  say "these are already in history: the hooks and CI stop new ones, they do not remove these"
fi
exit 1
