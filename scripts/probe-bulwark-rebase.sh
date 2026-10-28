#!/usr/bin/env bash
# Probe how much of the Loomix working-tree diff lands on a clean 1.18.0 tree.
set -u
BUILD=/root/bulwark-src-build-1.18.0
P=/root/mail-backups/20261027T034500/loomix-worktree.patch

cd "$BUILD" || { echo "no build tree at $BUILD"; exit 1; }
git checkout -- . 2>/dev/null
git clean -fdq 2>/dev/null

git apply --3way "$P" > /tmp/apply.log 2>&1

conflicted=$(git diff --name-only --diff-filter=U 2>/dev/null | wc -l)
missing=$(grep -c 'does not exist in index' /tmp/apply.log 2>/dev/null || true)
applied=$(grep -c '^Applied patch to' /tmp/apply.log 2>/dev/null || true)

echo "applied cleanly : $applied"
echo "conflicted      : $conflicted"
echo "absent upstream : $missing"
echo
echo "--- conflicted files ---"
git diff --name-only --diff-filter=U 2>/dev/null | head -40
echo
echo "--- paths absent from 1.18.0 ---"
grep 'does not exist in index' /tmp/apply.log 2>/dev/null | sed 's/^error: //; s/: does not exist.*//' | head -20
