#!/usr/bin/env bash
# Port the Loomix customisation of the four admin pages from 1.17.1's _tabs/
# onto 1.18.0's _pages/, which is where upstream moved them.
set -u
BUILD=/root/bulwark-src-build-1.18.0
P=/root/mail-backups/20261027T034500/loomix-worktree.patch
P2=/tmp/p-tabs.patch

cd "$BUILD" || exit 1

sed 's#app/(main)/admin/_tabs/#app/(main)/admin/_pages/#g' "$P" > "$P2"

git apply --3way \
  --include='app/(main)/admin/_pages/marketplace.tsx' \
  --include='app/(main)/admin/_pages/policy.tsx' \
  --include='app/(main)/admin/_pages/telemetry.tsx' \
  --include='app/(main)/admin/_pages/version.tsx' \
  "$P2" 2>&1 | head -20

echo "rc=$?"
echo "--- unresolved after port ---"
git diff --name-only --diff-filter=U 2>/dev/null | head -10
echo "(empty = clean)"
