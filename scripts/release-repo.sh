#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# release-repo.sh — cut a release for any repo in the Innotel Platform Stack.
#
# Usage:
#   scripts/release-repo.sh <repo-dir> [version]
#
# Examples:
#   scripts/release-repo.sh distro            # bump patch from the last tag
#   scripts/release-repo.sh distro v0.1.2     # explicit version
#
# Behavior:
#   1. Refuses to run on a dirty working tree (commit first).
#   2. Pushes the current branch.
#   3. Computes the next patch (or minor for X.Y schemes) from the last
#      remote tag, or uses the explicit version argument.
#   4. Creates + pushes the tag (fires the repo's release workflow).
#   5. Ensures a GitHub Release exists for the tag (create or update).
#   6. Watches the release workflow and reports the outcome.
#
# Works with every product repo (image pipelines, bundle-only pipelines,
# draft-style pipelines). It never commits, so a release always reflects the
# committed state of the branch.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

usage() { echo "usage: release-repo.sh <repo-dir> [version]" >&2; exit 1; }

[ $# -ge 1 ] || usage
REPO_DIR="$1"
EXPLICIT="${2:-}"

[ -d "$REPO_DIR/.git" ] || { echo "error: $REPO_DIR is not a git repo" >&2; exit 1; }

cd "$REPO_DIR"

# ── 1. Working tree must be clean ─────────────────────────────────────────
if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is dirty — commit or stash before releasing" >&2
  git status --short >&2
  exit 1
fi

REMOTE="$(git remote get-url origin 2>/dev/null || true)"
[ -n "$REMOTE" ] || { echo "error: no origin remote" >&2; exit 1; }
SLUG="$(printf '%s' "$REMOTE" | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
BRANCH="$(git branch --show-current)"
[ -n "$BRANCH" ] || { echo "error: detached HEAD" >&2; exit 1; }

# ── 2. Push the branch ────────────────────────────────────────────────────
echo "==> pushing $BRANCH to $SLUG"
git push origin "$BRANCH"

# ── 3. Resolve the version ────────────────────────────────────────────────
if [ -n "$EXPLICIT" ]; then
  VERSION="${EXPLICIT#v}"
else
  LAST="$(git ls-remote --tags origin 2>/dev/null | awk -F/ '{print $NF}' | grep -v '\^{}' | sort -V | tail -1)"
  LAST="${LAST#v}"
  if [ -z "$LAST" ]; then
    VERSION="0.1.0"
  else
    IFS='.' read -r -a PARTS <<<"$LAST"
    if [ "${#PARTS[@]}" -ge 3 ]; then
      VERSION="${PARTS[0]}.${PARTS[1]}.$((PARTS[2] + 1))"
    else
      VERSION="${PARTS[0]}.$((PARTS[1] + 1))"
    fi
  fi
fi
TAG="v$VERSION"

# ── 4. Tag + push ─────────────────────────────────────────────────────────
echo "==> tagging $TAG"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "tag $TAG already exists locally — reusing"
else
  git tag "$TAG"
fi
git push origin "$TAG"

# ── 5. Ensure the GitHub Release exists ───────────────────────────────────
echo "==> ensuring GitHub Release for $TAG"
NOTES=$(printf '%s\n' \
  "$SLUG **$TAG** — cut by scripts/release-repo.sh." \
  "" \
  "The repo's release workflow builds and attaches its artifacts to this release.")
gh release create "$TAG" -R "$SLUG" --title "$TAG" --notes "$NOTES" 2>/dev/null \
  || gh release edit "$TAG" -R "$SLUG" --title "$TAG" --notes "$NOTES" >/dev/null 2>&1 \
  || true

# ── 6. Watch the release workflow ─────────────────────────────────────────
echo "==> watching the release workflow (can take several minutes)..."
RUN_ID=""
for _ in $(seq 1 60); do
  RUN_ID="$(gh run list -R "$SLUG" --limit 20 --json databaseId,workflowName,event --jq \
    "[.[] | select(.workflowName|test(\"(?i)release\") and .event==\"push\")][0].databaseId" 2>/dev/null || true)"
  [ -n "$RUN_ID" ] && break
  sleep 5
done
[ -n "$RUN_ID" ] || { echo "error: no release workflow run found after the tag push" >&2; exit 1; }

gh run watch "$RUN_ID" -R "$SLUG" --exit-status --interval 20 2>/dev/null \
  && echo "release $TAG: workflow succeeded" \
  || { echo "release $TAG: workflow FAILED (run $RUN_ID)" >&2; exit 1; }