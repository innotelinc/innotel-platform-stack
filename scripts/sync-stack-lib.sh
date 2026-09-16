#!/usr/bin/env bash
# sync-stack-lib.sh — mirror the central stack-lib.sh into consuming repos.
#
# Each platform repo sources the shared library from its own tree so scripts
# keep working offline and in CI without a second checkout:
#
#   ./scripts/sync-stack-lib.sh <repo-dir> [repo-dir ...]
#   ./scripts/sync-stack-lib.sh --all           # every member repo
#   ./scripts/sync-stack-lib.sh --check         # verify only; exit 1 on drift
#
# Member repos live under the group dirs beside the platform stack:
#   <root>/<N>-<group>/<repo>/scripts/stack-lib.sh
#
# Idempotent: writes scripts/stack-lib.sh (or the target path) in each repo.
# Repos that already carry the file get it refreshed verbatim from here, so
# the canonical copy is the only source of truth.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_SRC="$HERE/stack-lib.sh"
ROOT="$(cd "${HERE}/../.." && pwd)"

die() { printf 'sync-stack-lib: error: %s\n' "$*" >&2; exit 1; }
say() { printf 'sync-stack-lib: %s\n' "$*"; }

usage() {
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
}

[ -f "$LIB_SRC" ] || die "canonical library not found at $LIB_SRC"

mode="sync"
case "${1:-}" in
  --all)     shift; mode="sync" ;;
  --check)   shift; mode="check" ;;
  -h|--help) usage; exit 0 ;;
esac

# Every git checkout one level below a group dir (<root>/<N>-<group>/<repo>).
member_repos() {
  local group repo
  for group in "$ROOT"/*/; do
    [ -d "$group" ] || continue
    for repo in "$group"*/; do
      [ -d "${repo}.git" ] || continue
      echo "${repo%/}"
    done
  done
}

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
  while read -r d; do [ -n "$d" ] && targets+=("$d"); done < <(member_repos)
fi
[ ${#targets[@]} -gt 0 ] || die "no member repos found under $ROOT (use: sync-stack-lib.sh <repo-dir> [...] | --all)"

drift=0
updated=0
for repo in "${targets[@]}"; do
  # Keep the path the caller gave us: the cd below overwrites $repo, and a
  # failure message that names an empty string is worse than no message.
  given="$repo"
  if ! repo="$(cd "$repo" 2>/dev/null && pwd)"; then
    die "not a directory: $given"
  fi
  dest="$repo/scripts/stack-lib.sh"
  [ "$dest" = "$LIB_SRC" ] && continue    # the canonical copy itself

  if [ -f "$dest" ] && cmp -s "$LIB_SRC" "$dest"; then
    [ "$mode" = "check" ] && say "ok    $dest"
    continue
  fi

  if [ "$mode" = "check" ]; then
    printf 'sync-stack-lib: DRIFT %s\n' "$dest" >&2
    drift=$((drift + 1))
    continue
  fi

  mkdir -p "$(dirname "$dest")"
  cp "$LIB_SRC" "$dest"
  chmod +x "$dest"
  say "synced $dest"
  updated=$((updated + 1))
done

if [ "$mode" = "check" ]; then
  if [ "$drift" -gt 0 ]; then
    printf 'sync-stack-lib: FAIL — %d copy(ies) differ from the canonical %s\n' "$drift" "$LIB_SRC" >&2
    exit 1
  fi
  say "done — ${#targets[@]} repo(s) match the canonical copy"
else
  say "done — $updated updated, ${#targets[@]} checked"
fi
