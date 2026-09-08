#!/usr/bin/env bash
# sync-stack-lib.sh — mirror the central stack-lib.sh into consuming repos.
#
# Each platform repo sources the shared library from its own tree so scripts
# keep working offline and in CI without a second checkout:
#
#   ./scripts/sync-stack-lib.sh <repo-dir> [repo-dir ...]
#   ./scripts/sync-stack-lib.sh --all           # every sibling repo dir
#
# Idempotent: writes scripts/stack-lib.sh (or the target path) in each repo.
# Repos that already carry the file get it refreshed verbatim from here, so
# the canonical copy is the only source of truth.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_SRC="$HERE/stack-lib.sh"

die() { printf 'sync-stack-lib: error: %s\n' "$*" >&2; exit 1; }
say() { printf 'sync-stack-lib: %s\n' "$*"; }

[ -f "$LIB_SRC" ] || die "canonical library not found at $LIB_SRC"

targets=()
if [ "${1:-}" = "--all" ]; then
  for d in "$HERE"/../../; do
    [ -d "$d" ] || continue
    for sub in "$d"*/; do
      [ -d "$sub/.git" ] && targets+=("${sub%/}")
    done
  done
else
  targets=("$@")
fi
[ ${#targets[@]} -gt 0 ] || die "usage: sync-stack-lib.sh <repo-dir> [...] | --all"

for repo in "${targets[@]}"; do
  repo="$(cd "$repo" 2>/dev/null && pwd)" || die "not a directory: $repo"
  dest="$repo/scripts/stack-lib.sh"
  [ "$dest" = "$LIB_SRC" ] && continue  # the canonical copy itself
  mkdir -p "$(dirname "$dest")"
  cp "$LIB_SRC" "$dest"
  chmod +x "$dest"
  say "synced $dest"
done

say "done — ${#targets[@]} repo(s) updated"