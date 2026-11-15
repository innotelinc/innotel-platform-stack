#!/usr/bin/env bash
# Fill the estate-checks directory from this checkout — and take the list from the unit.
#
# WHY THIS EXISTS
# ---------------
# `systemd/estate-checks.service` names every check by absolute path
# (`/opt/innotel/estate-checks/check-*.py`) and is installed on the edge by hand
# (`docs/unit-apply-checklist.md` step 2). Until now the copying half of that was a line in
# somebody's shell history, so the directory and the unit were two lists kept level by
# memory — and the failure mode of a mismatch is `status=203/EXEC` on every run with every
# file, timer and unit in place, which is the trap the checklist already documents.
#
# The list here is *derived*, not typed: every file the unit names inside the install
# directory (`ExecStart=`'s script and the data files passed to it), plus the shared module
# the checks import, plus the few repo files a check streams or reads beside itself. So a
# check added to the unit arrives with it, and a check removed from the unit stops being
# installed — the same reasoning as `check-stalwart-oidc-preflight.py` streaming the
# runbook's own script instead of reimplementing it.
#
# WHAT IT DOES NOT DO
# -------------------
# It does not install the unit and it does not restart anything: those are steps 4 and 5 of
# the checklist, and a copy that quietly reloads systemd is a copy that can hide the very
# 203/EXEC this is meant to prevent. It prints what to run next instead.
#
# Exit codes: 0 = every file is in place, 1 = one was missing from the checkout or landed
# short, 2 = this is not a checkout (no unit to read).
#
# On the edge, from a checkout:
#
#     sudo bash scripts/install-estate-checks.sh
#     sudo bash scripts/install-estate-checks.sh --print        # the list, changing nothing
set -euo pipefail

self_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(cd -- "$self_dir/.." && pwd)
unit="$root/systemd/estate-checks.service"
dest=${DEST:-/opt/innotel/estate-checks}

die() { printf 'install-estate-checks: %s\n' "$*" >&2; exit 2; }

# Files a check reads from its own directory rather than from the estate. Each line is a
# claim about a script: `check-stalwart-oidc-preflight.py` streams
# `stalwart-oidc-discovery-setup.sh` into the mail container, and exits 2 with that reason
# when it is not beside the check. `test_install_estate_checks.py` holds this list to the
# `with_name(...)` literals in the checks, so a new sibling dependency that is not added
# here fails the suite rather than the next run on the edge.
SIBLINGS=(
  stalwart-oidc-discovery-setup.sh
)

# The shared telemetry contract every check imports (`estate_check.py`).
SHARED=(
  estate_check.py
)

[ -f "$unit" ] || die "no unit at $unit — run this from the repository checkout"

# Everything the unit names inside the install directory, as basenames. Both the script and
# the `--pins/--redirect-pins/--expect` data files match this, which is why one pattern
# covers the checks and the tables they are given.
unit_files() {
  grep -E '^ExecStart=' "$unit" \
    | grep -oE '/opt/innotel/estate-checks/[A-Za-z0-9._-]+' \
    | sed 's|.*/||'
}

all_files() {
  { unit_files; printf '%s\n' "${SIBLINGS[@]}" "${SHARED[@]}"; } | sort -u
}

if [ "${1:-}" = "--print" ] || [ "${1:-}" = "-n" ]; then
  all_files
  exit 0
fi
[ $# -eq 0 ] || die "unknown argument: $1 (the only one is --print)"

mkdir -p "$dest" || die "could not create $dest"

installed=0
short=0
while read -r name; do
  [ -n "$name" ] || continue
  source_file="$self_dir/$name"
  if [ ! -f "$source_file" ]; then
    printf 'MISSING  %-38s the unit names it, scripts/ does not have it\n' "$name" >&2
    short=1
    continue
  fi
  # Executables stay executable: the unit runs them through `/usr/bin/python3`, and a
  # sibling such as the setup script is invoked as `bash <path>`, but both were `0755` in
  # the checkout and a 0644 copy of a script is the kind of difference that only shows up
  # when something else calls it directly. Tables are data and stay 0644.
  case "$name" in
    *.py | *.sh) install -m 0755 "$source_file" "$dest/$name" ;;
    *) install -m 0644 "$source_file" "$dest/$name" ;;
  esac
  # Prove the copy landed rather than that install returned 0: a short or empty file is a
  # check that imports to nothing, which reads as a failing check instead of a bad deploy.
  if [ ! -s "$dest/$name" ]; then
    printf 'FAIL     %-38s landed empty at %s\n' "$name" "$dest/$name" >&2
    short=1
    continue
  fi
  printf 'ok       %-38s %s\n' "$name" "$dest/$name"
  installed=$((installed + 1))
done < <(all_files)

# The other direction: a `check-*.py` left in the directory that the unit no longer runs.
# Not a failure — the file is inert — but it is how `check-stuck-processes.py` sat on the
# edge unowned for six days, and the difference between "deployed" and "run by a timer" is
# exactly what this whole file is about.
if [ -d "$dest" ]; then
  while read -r stale; do
    [ -n "$stale" ] || continue
    printf 'note     %-38s in %s, but no ExecStart runs it\n' "$stale" "$dest"
  done < <(comm -13 <(all_files) <(cd "$dest" && ls -1 check-*.py 2>/dev/null | sort || true))
fi

printf '\n%d file(s) in %s\n' "$installed" "$dest"
if [ "$short" -ne 0 ]; then
  printf 'install-estate-checks: something did not land — do NOT reload the unit yet\n' >&2
  exit 1
fi
cat <<EOF

next, from docs/unit-apply-checklist.md:
  install -m 0644 systemd/estate-checks.service systemd/estate-checks.timer /etc/systemd/system/
  systemctl daemon-reload && systemctl enable --now estate-checks.timer
  systemctl start estate-checks.service
  systemctl show -p Result -p ExecMainStatus estate-checks.service
EOF
