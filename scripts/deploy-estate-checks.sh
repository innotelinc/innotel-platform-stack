#!/usr/bin/env bash
# Deploy the estate checks — the step that was missing between "merged" and "running".
#
# WHY THIS EXISTS
# ---------------
# `install-estate-checks.sh` is the guard: it installs the checks the unit names and compares
# every installed byte with a revision, refusing a short copy or a drifted one. It is correct
# and it was never the problem. The problem was that *nothing ran it*. On 2026-11-18 the edge
# was carrying a set of checks that no longer matched `main`, and the only witness was
# `check-installed-checks.py` — a scheduled check that compares the install to the revision —
# whose own series was unread, because the metrics path it publishes into was down. Two
# independent silences, and the estate ran a week-old assertion while every door looked fine.
#
# So this is one command with one meaning: take the checkout to `origin/main`, install exactly
# that revision, and then *prove* it by running the comparison a second time. A deploy that
# cannot prove what it deployed is the thing this replaces.
#
# WHAT IT REFUSES
# ---------------
# - A dirty checkout. Installing from one installs somebody's half-finished edit as if it were
#   main, and `--allow-drift` on the installer would then be doing the hiding.
# - A checkout behind or ahead of `origin/main` unless `--allow-drift` is passed, because a
#   deploy from a branch is a decision, not a default. The difference is printed either way.
# - A verification that does not pass. The exit status is the point: a script that installed
#   files and printed ok without comparing them is what the estate already had.
#
# It does not start the run. `estate-checks.timer` runs every half hour and `--run` starts one
# now; a deploy is not an excuse to page about a check that a person is already watching.
#
# Usage, on the edge, from a checkout:
#
#     sudo bash scripts/deploy-estate-checks.sh
#     sudo bash scripts/deploy-estate-checks.sh --run       # and start a run afterwards
#     sudo bash scripts/deploy-estate-checks.sh --allow-drift   # deploying a branch on purpose
#     sudo bash scripts/deploy-estate-checks.sh --ref <rev>     # hold it to another revision
#
# Exit codes: 0 = installed and verified, 1 = installed but the comparison failed, 2 = this is
# not a checkout, or the checkout is not the revision it was told to deploy.
set -euo pipefail

self_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(cd -- "$self_dir/.." && pwd)
installer="$self_dir/install-estate-checks.sh"

ref=${REF:-origin/main}
allow_drift=0
run_after=0

die() { printf 'deploy-estate-checks: %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --allow-drift) allow_drift=1 ;;
    --run) run_after=1 ;;
    --ref) shift; [ $# -gt 0 ] || die "--ref needs a revision"; ref=$1 ;;
    --ref=*) ref=${1#--ref=} ;;
    *) die "unknown argument: $1 (--run, --allow-drift, --ref <rev>)" ;;
  esac
  shift
done

[ -f "$installer" ] || die "no installer at $installer — run this from the repository checkout"
[ -d "$root/.git" ] || die "$root is not a git checkout, so there is no revision to deploy"
[ -d /run/systemd/system ] || die "this host does not run systemd; install the checks with the installer instead"

# The checkout has to be main's, or the install is of something else. Fetch first: a ref that
# is one merge behind reports the checks that landed in between as drift, which is how the
# first run of `check-installed-checks.py` read a sound install as broken.
if [ "$ref" = origin/main ] || [ "$ref" = origin/master ]; then
  git -C "$root" fetch --quiet --prune origin "${ref#origin/}" || \
    printf 'deploy-estate-checks: warning: fetch failed; deploying the revision this checkout already has\n' >&2
fi
resolved=$(git -C "$root" rev-parse --verify --quiet "$ref^{commit}") || \
  die "cannot resolve $ref in $root — nothing to deploy"
head=$(git -C "$root" rev-parse HEAD)

# Tracked changes only. An untracked file is never installed — the installer copies exactly
# the names the unit carries — and the estate deliberately keeps `*.bak` copies of the check
# it replaced beside the live one (see the installer's own header). Refusing a deploy over
# those would ask a person to delete a record in order to ship a fix.
if [ -n "$(git -C "$root" status --porcelain --untracked-files=no)" ]; then
  git -C "$root" status --porcelain --untracked-files=no >&2
  die "the checkout has uncommitted changes to tracked files; a deploy must install a revision"
fi

if [ "$head" != "$resolved" ] && [ "$allow_drift" -eq 0 ]; then
  printf 'deploy-estate-checks: this checkout is %s and %s is %s\n' \
    "$(git -C "$root" rev-parse --short HEAD)" "$ref" "$(git -C "$root" rev-parse --short "$resolved")" >&2
  die "not deploying a checkout that is not $ref — pull it, or pass --allow-drift on purpose"
fi
if [ "$head" != "$resolved" ]; then
  printf 'note     deploying %s (%s) while %s is %s — accepted with --allow-drift\n' \
    "$(git -C "$root" rev-parse --short HEAD)" "$(git -C "$root" log -1 --format=%s HEAD)" \
    "$ref" "$(git -C "$root" rev-parse --short "$resolved")" >&2
fi

printf 'deploying %s (%s)\n\n' "$(git -C "$root" rev-parse --short HEAD)" \
  "$(git -C "$root" log -1 --format=%s HEAD)"

# The install itself, held to the revision being deployed. `--allow-drift` is forwarded so a
# deliberate branch deploy is not refused by the installer after this script allowed it.
install_args=(--ref "$ref")
[ "$allow_drift" -eq 1 ] && install_args+=(--allow-drift)
bash "$installer" "${install_args[@]}"

# And the proof, from the same revision, changing nothing: the installer compared as it wrote,
# this compares what is actually on disk now. A file replaced in between is exactly the
# 2026-11-15 failure, and only the second read can see it.
printf '\nverifying the installed copy\n'
if ! bash "$installer" --ref "$ref" --verify; then
  printf 'deploy-estate-checks: installed, but the copy on disk is not %s\n' "$ref" >&2
  exit 1
fi

# The timer is what makes any of this run. Enabling it is idempotent and is part of a deploy
# because a service installed without its timer never runs, silently.
if systemctl list-unit-files estate-checks.timer >/dev/null 2>&1; then
  systemctl enable --now estate-checks.timer >/dev/null 2>&1 || \
    printf 'deploy-estate-checks: warning: could not enable estate-checks.timer\n' >&2
  printf '\nnext run: %s\n' "$(systemctl list-timers estate-checks.timer --no-pager 2>/dev/null | sed -n 2p | cut -c1-60)"
fi

if [ "$run_after" -eq 1 ]; then
  printf '\nstarting one run now\n'
  systemctl start estate-checks.service
  systemctl show -p Result -p ExecMainStatus estate-checks.service
fi

printf '\ninstalled and verified: %s\n' "$(git -C "$root" rev-parse --short HEAD)"
