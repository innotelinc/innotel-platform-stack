#!/usr/bin/env bash
# sync-mesh.sh — mirror the canonical mesh.sh into every member repo.
#
# The mesh script has to be present in every repo that takes part in the mesh,
# because that is the copy an operator runs on that server — but there is only
# ever ONE implementation. This mirrors the canonical copy:
#
#   ./scripts/sync-mesh.sh <repo-dir> [repo-dir ...]
#   ./scripts/sync-mesh.sh --all        # every member repo in the workspace
#   ./scripts/sync-mesh.sh --check      # verify only; exit 1 on drift
#
# Member repos live under the group dirs beside the platform stack:
#
#   <root>/<N>-<group>/<repo>/scripts/mesh.sh
#   <root>/ips/scripts/mesh.sh                  ← canonical, never overwritten
#
# Idempotent: an identical copy is left alone, a stale one is refreshed.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${HERE}/mesh.sh"
ROOT="$(cd "${HERE}/../.." && pwd)"

die() { printf 'sync-mesh: error: %s\n' "$*" >&2; exit 1; }
say() { printf 'sync-mesh: %s\n' "$*"; }

[ -f "${SRC}" ] || die "canonical script not found at ${SRC}"

mode="sync"
case "${1:-}" in
  --all)   shift; mode="sync" ;;
  --check) shift; mode="check" ;;
esac

member_repos() {
  local group repo
  for group in "${ROOT}"/*/; do
    [ -d "${group}" ] || continue
    for repo in "${group}"*/; do
      [ -d "${repo}.git" ] || continue
      echo "${repo%/}"
    done
  done
}

targets=()
if [ "${mode}" = "sync" ] && [ "$#" -gt 0 ]; then
  targets=("$@")
elif [ "$#" -gt 0 ]; then
  targets=("$@")
else
  while read -r d; do [ -n "$d" ] && targets+=("$d"); done < <(member_repos)
fi
[ "${#targets[@]}" -gt 0 ] || die "no member repos found under ${ROOT} (use: sync-mesh.sh <repo-dir> [...] | --all)"

drift=0
updated=0
for repo in "${targets[@]}"; do
  repo="$(cd "${repo}" 2>/dev/null && pwd)" || die "not a directory: ${repo}"
  dest="${repo}/scripts/mesh.sh"
  [ "${dest}" = "${SRC}" ] && continue    # the canonical copy itself

  if [ -f "${dest}" ] && cmp -s "${SRC}" "${dest}"; then
    [ "${mode}" = "check" ] && say "ok    ${dest}"
    continue
  fi

  if [ "${mode}" = "check" ]; then
    printf 'sync-mesh: DRIFT %s\n' "${dest}" >&2
    drift=$((drift + 1))
    continue
  fi

  mkdir -p "$(dirname "${dest}")"
  cp "${SRC}" "${dest}"
  chmod +x "${dest}"
  say "synced ${dest}"
  updated=$((updated + 1))
done

if [ "${mode}" = "check" ]; then
  if [ "${drift}" -gt 0 ]; then
    printf 'sync-mesh: FAIL — %d copy(ies) differ from the canonical %s\n' "${drift}" "${SRC}" >&2
    exit 1
  fi
  say "done — ${#targets[@]} repo(s) match the canonical copy"
else
  say "done — ${updated} updated, ${#targets[@]} checked"
fi
