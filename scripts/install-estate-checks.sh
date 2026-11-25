#!/usr/bin/env bash
# Fill the estate-checks directory from this checkout — and take the list from the unit.
#
# WHY THIS EXISTS
# ---------------
# `systemd/estate-checks.service` names every check by absolute path
# (`/opt/innotel/estate-checks/check-*.py`). The directory and the unit are one list split
# across two files, and the failure mode of a mismatch is `status=203/EXEC` on every run
# with every file, timer and unit in place, which is the trap the checklist already
# documents. So this fills the directory *and* installs the unit that names it, from the
# same checkout and under the same guard: a unit edit cannot land beside scripts from
# another revision, or the other way round.
#
# The list here is *derived*, not typed: every file the unit names inside the install
# directory (`ExecStart=`'s script and the data files passed to it), plus the shared module
# the checks import, plus the few repo files a check streams or reads beside itself. So a
# check added to the unit arrives with it, and a check removed from the unit stops being
# installed — the same reasoning as `check-stalwart-oidc-preflight.py` streaming the
# runbook's own script instead of reimplementing it.
#
# THE UNIT TOO
# ------------
# The unit and its timer are installed into `/etc/systemd/system` here, because a unit left
# in a checkout is a timer running yesterday's `ExecStart=` lines beside today's scripts —
# the same mismatch as a missing script, one file over. When the installed text changes the
# script runs `systemctl daemon-reload`, since an edited unit that was never reloaded keeps
# running the old text, which is the other trap the checklist names.
#
# THE JOBS THAT ARE NOT CHECKS
# ----------------------------
# `estate-checks.service` is not the only thing on a schedule. The console's AMI reconciler
# and the monthly break-glass drill are timers of their own, with scripts in this same
# directory — the derivation above cannot see them, because no `ExecStart=` in the check
# unit names them, and that is how they came to be installed by hand. They are listed below
# and installed (and drift-checked) with the same guard: a unit installed without its
# script is `203/EXEC` on a schedule nobody is watching, and these two are precisely the
# jobs that run when nothing else is looking.
#
# It still does not start or restart anything: starting is what would hide the 203/EXEC this
# is meant to prevent, and the checklist's last step is a person reading `ExecMainStatus`.
#
# THE GUARD
# ---------
# What lands has to be what the repository says, not merely *a* copy of it. A working tree
# with an uncommitted edit, a checkout on a branch, or a deployed check silently replaced by
# an older copy an hour after it landed (measured 2026-11-15): none of those show up in the
# run itself. The checks keep working, they just stop asserting what `main` says.
#
# So every installed file is compared byte for byte with `scripts/<name>` at a revision,
# `origin/main` unless told otherwise, and a difference fails the install. Deploying a fix
# before its pull request merges is legitimate; `--allow-drift` is how a person says so out
# loud, and the difference is still printed.
#
# `--verify` runs that comparison alone, changing nothing: it is what catches a copy that
# drifted *after* the install, and what a periodic job should call.
#
# Exit codes: 0 = every file is in place and matches, 1 = one was missing from the checkout,
# landed short, or differs from the revision, 2 = this is not a checkout (no unit to read).
#
# On the edge, from a checkout:
#
#     sudo bash scripts/install-estate-checks.sh
#     sudo bash scripts/install-estate-checks.sh --print        # the list, changing nothing
#     sudo bash scripts/install-estate-checks.sh --verify       # installed == revision?
#     sudo bash scripts/install-estate-checks.sh --allow-drift  # installing from a branch
#     sudo bash scripts/install-estate-checks.sh --ref <rev>    # hold it to another revision
set -euo pipefail

self_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(cd -- "$self_dir/.." && pwd)
unit="$root/systemd/estate-checks.service"
dest=${DEST:-/opt/innotel/estate-checks}
# Where systemd reads the job from. Overridable so the tests — and anyone staging a copy —
# can install somewhere that is not the live unit directory.
unit_dest=${UNIT_DEST:-/etc/systemd/system}
# The two files that make up the estate-checks job. The timer rides with the service: a
# service installed without its timer never runs, and a timer without its service fires into
# nothing, both of them silently.
UNIT_FILES=(estate-checks.service estate-checks.timer)
# The jobs of their own: the reconciler that re-pins the console's AMI settings every ten
# minutes, and the drill that measures the break-glass path monthly. Their scripts live in
# the install directory like the checks', so they are installed from the same checkout and
# compared with the same revision.
COMPANION_UNITS=(
  console-ami-pin.service
  console-ami-pin.timer
  pbx-breakglass-drill.service
  pbx-breakglass-drill.timer
)
COMPANION_SCRIPTS=(
  ensure-console-ami.py
  drill-pbx-breakglass.py
)
ALL_UNITS=("${UNIT_FILES[@]}" "${COMPANION_UNITS[@]}")

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
  { unit_files; printf '%s\n' "${SIBLINGS[@]}" "${SHARED[@]}" "${COMPANION_SCRIPTS[@]}"; } | sort -u
}

ref=${REF:-}
verify=0
allow_drift=0
while [ $# -gt 0 ]; do
  case "$1" in
    --print|-n) all_files; exit 0 ;;
    --verify) verify=1 ;;
    --allow-drift) allow_drift=1 ;;
    --ref) shift; [ $# -gt 0 ] || die "--ref needs a revision"; ref=$1 ;;
    --ref=*) ref=${1#--ref=} ;;
    *) die "unknown argument: $1 (--print, --verify, --allow-drift, --ref <rev>)" ;;
  esac
  shift
done
# A revision nobody can resolve is the guard failing open, which is worse than not having
# it: the install would print ok for a copy it never compared. Refuse instead.
if [ -n "$ref" ] && ! git -C "$root" rev-parse --verify --quiet "$ref^{commit}" >/dev/null 2>&1; then
  git -C "$root" fetch -q origin 2>/dev/null || true
fi
has_ref=0
if [ -n "$ref" ]; then
  git -C "$root" rev-parse --verify --quiet "$ref^{commit}" >/dev/null 2>&1 && has_ref=1
  [ "$has_ref" -eq 1 ] || die "cannot resolve $ref in $root — nothing to compare the install with"
else
  for candidate in origin/main HEAD; do
    if git -C "$root" rev-parse --verify --quiet "$candidate^{commit}" >/dev/null 2>&1; then
      ref=$candidate
      has_ref=1
      break
    fi
  done
fi

drift=0

# check_drift_at <path in the repository> <installed file> — compare one file with the
# revision. It takes the path in the repository rather than assuming `scripts/`, because the
# unit is held to the same comparison as the scripts it names.
check_drift_at() {
  [ "$has_ref" -eq 1 ] || return 0
  if ! git -C "$root" cat-file -e "$ref:$1" 2>/dev/null; then
    printf 'DRIFT    %-38s installed, and %s does not have it\n' "${1##*/}" "$ref" >&2
    drift=1
    return 0
  fi
  if ! git -C "$root" show "$ref:$1" 2>/dev/null | cmp -s - "$2"; then
    printf 'DRIFT    %-38s differs from %s:%s\n' "${1##*/}" "$ref" "$1" >&2
    drift=1
  fi
}

# The checks half: everything the unit runs lives in `scripts/`.
check_drift() {
  check_drift_at "scripts/$1" "$2"
}

if [ "$verify" -eq 1 ]; then
  [ -d "$dest" ] || die "no install directory at $dest"
  verify_missing=0
  while read -r name; do
    [ -n "$name" ] || continue
    if [ ! -f "$dest/$name" ]; then
      printf 'MISSING  %-38s the unit names it, %s does not have it\n' "$name" "$dest" >&2
      verify_missing=1
      continue
    fi
    check_drift "$name" "$dest/$name"
  done < <(all_files)
  # The other direction, over every check on disk rather than the unit's list: a script
  # left behind is drift as soon as the revision stops carrying it.
  while read -r deployed; do
    [ -n "$deployed" ] || continue
    [ "$has_ref" -eq 1 ] || break
    git -C "$root" cat-file -e "$ref:scripts/$deployed" 2>/dev/null && continue
    printf 'DRIFT    %-38s installed, and %s does not have it\n' "$deployed" "$ref" >&2
    drift=1
  done < <(cd "$dest" && find . -maxdepth 1 -type f -name 'check-*.py' -printf '%f\n' | sort)
  # The unit is the other half of the list, and the same invisibility applies: a unit an
  # edit behind names a script the directory no longer has, and the `-` on every ExecStart
  # turns that into a silent textfile rather than an error.
  for name in "${ALL_UNITS[@]}"; do
    if [ ! -f "$root/systemd/$name" ]; then
      continue
    fi
    if [ ! -f "$unit_dest/$name" ]; then
      printf 'MISSING  %-38s the checkout has it, %s does not\n' "$name" "$unit_dest" >&2
      verify_missing=1
      continue
    fi
    check_drift_at "systemd/$name" "$unit_dest/$name"
  done
  printf '\nverified %s against %s\n' "$dest" "${ref:-nothing (not a checkout)}"
  [ "$verify_missing" -eq 0 ] || exit 1
  if [ "$drift" -ne 0 ]; then
    if [ "$allow_drift" -eq 1 ]; then
      printf 'note     drift accepted with --allow-drift\n' >&2
      exit 0
    fi
    exit 1
  fi
  exit 0
fi

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
  check_drift "$name" "$dest/$name"
  installed=$((installed + 1))
done < <(all_files)

# The unit half of the guard, before anything is written over /etc: a checkout on a branch
# has a unit that differs from main's, and installing that is how the two lists came apart.
for name in "${ALL_UNITS[@]}"; do
  if [ -f "$root/systemd/$name" ]; then
    check_drift_at "systemd/$name" "$root/systemd/$name"
  fi
done

# The other direction: a `check-*.py` left in the directory that the unit no longer runs.
# Not a failure — the file is inert — but it is how `check-stuck-processes.py` sat on the
# edge unowned for six days, and the difference between "deployed" and "run by a timer" is
# exactly what this whole file is about.
if [ -d "$dest" ]; then
  # `find -printf %f` rather than `ls`: this list is compared line-for-line with `comm`
  # against the names the unit runs, so a name `ls` prints with escapes is a stale note
  # either invented or lost, and the note is the point.
  deployed=$(cd "$dest" && find . -maxdepth 1 -type f -name 'check-*.py' -printf '%f\n' | sort)
  while read -r stale; do
    [ -n "$stale" ] || continue
    printf 'note     %-38s in %s, but no ExecStart runs it\n' "$stale" "$dest"
  done < <(comm -13 <(all_files) <(printf '%s\n' "$deployed"))
fi

printf '\n%d file(s) in %s\n' "$installed" "$dest"
if [ "$short" -ne 0 ]; then
  printf 'install-estate-checks: something did not land — do NOT reload the unit yet\n' >&2
  exit 1
fi
if [ "$drift" -ne 0 ]; then
  if [ "$allow_drift" -eq 1 ]; then
    printf 'note     the installed copy is not %s; accepted with --allow-drift\n' "$ref" >&2
  else
    printf 'install-estate-checks: the installed copy is not %s — install from %s, or pass\n' "$ref" "$ref" >&2
    printf '         --allow-drift when you are deliberately ahead of it\n' >&2
    exit 1
  fi
fi

# The unit and its timer, now that the scripts they name are there. This is the half that
# made "the unit is right" and "the checks are right" two separate facts a person had to keep
# level by hand.
unit_changed=0
for name in "${ALL_UNITS[@]}"; do
  if [ ! -f "$root/systemd/$name" ]; then
    continue
  fi
  # An identical unit is left untouched rather than rewritten: `install` bumps the mtime,
  # systemd reads that as "the text changed", and every install would ask for a reload it
  # does not need — `NeedDaemonReload=yes` on a host where nothing is waiting.
  if cmp -s "$root/systemd/$name" "$unit_dest/$name"; then
    printf 'ok       %-38s %s (unchanged)\n' "$name" "$unit_dest/$name"
    continue
  fi
  mkdir -p "$unit_dest" || die "could not create $unit_dest"
  unit_changed=1
  install -m 0644 "$root/systemd/$name" "$unit_dest/$name"
  # Prove the copy landed, for the same reason the checks are proved: a short unit file is a
  # unit systemd reads as truncated, and a `cp` that returned 0 is not that proof.
  if ! cmp -s "$root/systemd/$name" "$unit_dest/$name"; then
    printf 'FAIL     %-38s did not land as %s/%s\n' "$name" "$root/systemd" "$name" >&2
    exit 1
  fi
  printf 'ok       %-38s %s\n' "$name" "$unit_dest/$name"
done
# Only when the text actually changed, and only when this is the directory systemd reads: a
# reload is reading the unit files again, and an edited-but-unreloaded unit is the trap the
# checklist calls "fixed `ExecStart=` that looks unfixed".
if [ "$unit_changed" -eq 1 ]; then
  if [ "$unit_dest" = /etc/systemd/system ] && [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    printf 'reloaded systemd — it reads the unit that is there now (nothing was started)\n'
  else
    printf 'note     the unit changed; run: systemctl daemon-reload\n' >&2
  fi
fi

cat <<EOF

next, from docs/unit-apply-checklist.md:
  systemctl enable --now estate-checks.timer   # if it is not enabled yet
  systemctl enable --now console-ami-pin.timer pbx-breakglass-drill.timer
  systemctl start estate-checks.service
  systemctl show -p Result -p ExecMainStatus estate-checks.service
EOF
