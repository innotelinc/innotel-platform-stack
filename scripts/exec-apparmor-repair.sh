#!/usr/bin/env bash
# exec-apparmor-repair.sh — restore `incus exec` on containers whose AppArmor
# profile label is malformed.
#
# The fault this repairs
# ---------------------
# `incus exec` fails in under a second with
#   Error: Failed to retrieve PID of executing child process
# and the container's log shows
#   apparmor - Failed to write AppArmor profile
#     "incus-<name>_</var/lib/incus>//&:incus-<name>_<var-lib-incus>:unconfined" to 7
#   attach - do_attach - Failed to attach to container
#
# The label contains the raw storage path (`</var/lib/incus>`) stacked in front of
# the correctly escaped one (`<var-lib-incus>`), so LXC cannot write it to
# /proc/<pid>/attr/current and every attach fails. The container itself keeps
# running: its services are fine, only the shell is unreachable. That distinction
# is why this script never treats a broken exec as a service outage.
#
# Verified on i1 2026-11-16: the four affected containers (atheniq, ontrak,
# regcache, sage) all had clean config, and a single stop/start of each restored
# exec. That is the signature of a **stale label**: the running processes were
# started under an older incus/apparmor and still wear the label from then, while
# the daemon computes a different one when it attaches. A restart is the repair,
# and it works even though the old profile name stays visible in `aa-status`.
#
# What it does NOT do
# -------------------
# It never sets `lxc.apparmor.profile=unconfined`, never sets
# `security.privileged=true`, and never removes an AppArmor key from a container
# config to "make it work". Those would silence the symptom by disabling the
# confinement that protects the host. If a container's own config is the cause,
# this script stops and prints the exact key so a human decides.
#
# Usage
#   --diagnose            (default) read-only: labels, config keys, exec test
#   --apply --window      run the repair; --window is the operator's assertion
#                         that a maintenance window is open (services restart)
#   --only NAME           act on one container (repeatable)
#   --verify              exec test only, across every container
#   --stop-timeout N      seconds to let a container shut down (default 120)
#   --reload              also `systemctl reload apparmor` between stop and start
#   --quiet
#
# Exit: 0 every target repaired and verified; 2 something still broken (never a
# silent pass — the estate convention is that unreachable is a failure).

set -u -o pipefail

TS=$(date +%Y%m%dT%H%M%S)
BACKUP=/var/backups/apparmor-exec-repair
LOG="$BACKUP/repair-$TS.log"

# Least critical first. regcache is a registry cache and sage is small; ontrak
# hosts 17 sub-containers; atheniq is the LMS, so it goes last. If the repair is
# going to break something, it should break the cheap thing.
TARGETS_DEFAULT="regcache sage ontrak atheniq"

JUNK='</var/lib/incus>'      # the malformed fragment that must not appear
MODE=diagnose
APPLY=0
WINDOW=0
RELOAD=0
QUIET=0
STOP_TIMEOUT=120
ONLY_LIST=""

say()  { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }
log()  { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG" 2>/dev/null; }
head2() { say ""; say "== $*"; }
die()  { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --diagnose)       MODE=diagnose ;;
    --apply)          MODE=apply; APPLY=1 ;;
    --window)         WINDOW=1 ;;
    --verify)         MODE=verify ;;
    --only)           shift; ONLY_LIST="$ONLY_LIST $1" ;;
    --stop-timeout)   shift; STOP_TIMEOUT="$1" ;;
    --reload)         RELOAD=1 ;;
    --quiet)          QUIET=1 ;;
    -h|--help)        usage ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "run as root on the incus host"
command -v incus >/dev/null || die "incus not found"
mkdir -p "$BACKUP" || die "cannot create $BACKUP"
: >>"$LOG" || die "cannot write $LOG"
log "mode=$MODE apply=$APPLY window=$WINDOW reload=$RELOAD only=[$ONLY_LIST]"

if [ -n "$ONLY_LIST" ]; then TARGETS="$ONLY_LIST"; else TARGETS="$TARGETS_DEFAULT"; fi

# ---------------------------------------------------------------- primitives

state_of() { incus list "$1" --format csv 2>/dev/null | head -1 | cut -d, -f2; }

pids_of() {
  local c=$1 f
  for f in "/sys/fs/cgroup/lxc.payload.$c/pids.current" "/sys/fs/cgroup/lxc/$c/pids.current"; do
    [ -r "$f" ] && { cat "$f" 2>/dev/null; return 0; }
  done
  echo "?"
}

mem_of() {
  local c=$1 f
  for f in "/sys/fs/cgroup/lxc.payload.$c/memory.current"; do
    [ -r "$f" ] && awk '{printf "%.0fM", $1/1048576}' "$f" 2>/dev/null
  done
}

# Profile names mentioning this container. NOTE: aa-status reports the kernel's
# loaded profiles (not only live processes) and truncates long names, so this is
# informational only. Never use it to decide whether the repair succeeded.
labels_for() { aa-status 2>/dev/null | grep -F "incus-${1}_" | sed 's/^.*) //' | sort -u; }

label_is_junk() { labels_for "$1" | grep -qF "$JUNK"; }

apparmor_dirs() {
  for d in /var/lib/incus/security/apparmor/lxc /var/lib/incus/security/apparmor; do
    [ -d "$d" ] && printf '%s\n' "$d"
  done
}

profile_files_for() {
  local c=$1 d
  while IFS= read -r d; do
    find "$d" -maxdepth 1 \( -name "incus-${c}_*" -o -name "lxc-${c}_*" \) 2>/dev/null
  done < <(apparmor_dirs)
}

# Config keys that mean "someone already tried to fix this by hand".
risky_keys() {
  local c=$1
  incus config show "$c" 2>/dev/null | grep -nE 'raw\.lxc|raw\.apparmor|security\.privileged|security\.nesting|lxc\.apparmor\.profile' \
    | sed 's/^/      /'
}

exec_ok() {
  local c=$1
  timeout 25 incus exec -n "$c" -- /bin/sh -c 'exit 0' </dev/null >/dev/null 2>&1
}

exec_reason() {
  local c=$1
  timeout 25 incus exec -n "$c" -- /bin/sh -c 'exit 0' </dev/null 2>&1 | head -1
}

wait_running() {
  local c=$1 i=0 limit=${2:-90}
  while [ "$i" -lt "$limit" ]; do
    [ "$(state_of "$c")" = "RUNNING" ] && return 0
    sleep 2; i=$((i+2))
  done
  return 1
}

# A container is "healthy enough" if it is RUNNING, has processes, and stays
# RUNNING for a settle period without the daemon restarting it.
settle_check() {
  local c=$1 before after
  before=$(pids_of "$c")
  sleep 12
  [ "$(state_of "$c")" = "RUNNING" ] || return 1
  after=$(pids_of "$c")
  [ "$after" != "?" ] && [ "${after:-0}" -gt 0 ] || return 1
  return 0
}

# ---------------------------------------------------------------- diagnosis

diagnose_one() {
  local c=$1 st
  st=$(state_of "$c")
  head2 "$c"
  say "  state:            ${st:-UNKNOWN}"
  say "  pids:             $(pids_of "$c")   mem: $(mem_of "$c")"
  if [ "$st" != "RUNNING" ]; then
    say "  -> not running; exec repair does not apply"
    return 3
  fi

  local lab
  lab=$(labels_for "$c")
  if [ -z "$lab" ]; then
    say "  label:            (no incus-${c}_ label found in aa-status)"
  else
    say "  label:"
    printf '%s\n' "$lab" | sed 's/^/      /'
  fi

  if label_is_junk "$c"; then
    say "  old fragment:     present in loaded profile names (informational only)"
  else
    say "  old fragment:     absent"
  fi

  local files
  files=$(profile_files_for "$c")
  if [ -n "$files" ]; then
    say "  profile files:"
    printf '%s\n' "$files" | sed 's/^/      /'
  else
    say "  profile files:    none on disk (generated in memory)"
  fi

  local rk
  rk=$(risky_keys "$c")
  if [ -n "$rk" ]; then
    say "  config keys of interest:"; printf '%s\n' "$rk"
  else
    say "  config keys:      clean (no raw.apparmor / raw.lxc / apparmor profile override)"
  fi

  if exec_ok "$c"; then
    say "  exec:             OK"
    return 0
  fi
  say "  exec:             FAILS -> $(exec_reason "$c")"
  return 2
}

run_diagnose() {
  local rc=0 r
  head2 "AppArmor / exec diagnosis on $(hostname) ($(date -Is))"
  say "  kernel: $(uname -r)   incus: $(incus version 2>/dev/null | awk -F': ' '/Server version/{print $2}')"
  say "  incus profiles loaded: $(aa-status 2>/dev/null | grep -c 'incus-')"
  say "  profile files on disk: $(ls /etc/apparmor.d 2>/dev/null | grep -c incus)"
  say "  apparmor dirs: $(apparmor_dirs | tr '\n' ' ')"
  for c in $TARGETS; do
    diagnose_one "$c"; r=$?
    [ "$r" -ne 0 ] && rc=2
  done
  return "$rc"
}

run_verify() {
  local rc=0 c
  head2 "exec verification across all instances"
  for c in $(incus list --format csv -c n 2>/dev/null); do
    if [ "$(state_of "$c")" != "RUNNING" ]; then continue; fi
    if exec_ok "$c"; then printf '  %-16s exec OK\n' "$c"
    else printf '  %-16s exec FAILS\n' "$c"; rc=2; fi
  done
  return "$rc"
}

# ---------------------------------------------------------------- repair

repair_one() {
  local c=$1 st
  st=$(state_of "$c")
  head2 "repair $c (state: ${st:-unknown})"

  if [ "$st" != "RUNNING" ]; then
    say "  skipped: not running"
    return 3
  fi

  # Guard 1: a hand-written AppArmor override in the container config means a
  # restart will regenerate the same broken label. Refuse and report.
  local rk
  rk=$(incus config show "$c" 2>/dev/null | grep -E 'raw\.lxc|raw\.apparmor|lxc\.apparmor\.profile')
  if [ -n "$rk" ]; then
    say "  REFUSING: the container config carries an AppArmor override:"
    printf '%s\n' "$rk" | sed 's/^/      /'
    say "  A restart would regenerate the same label. Clear the key deliberately, e.g."
    say "      incus config unset $c raw.lxc     # after reading the value above"
    say "  ...then re-run this script. Not doing it automatically: unsetting could"
    say "  re-enable confinement a service depends on being released from."
    log "refused $c: config override present"
    return 4
  fi

  # Guard 2: capture everything needed to reason about it afterwards.
  incus config show "$c" > "$BACKUP/$c.$TS.config.yaml" 2>/dev/null
  labels_for "$c"        > "$BACKUP/$c.$TS.label.txt"  2>/dev/null
  profile_files_for "$c" > "$BACKUP/$c.$TS.files.txt"  2>/dev/null
  say "  captured: $BACKUP/$c.$TS.{config.yaml,label.txt,files.txt}"

  local before_pids before_label
  before_pids=$(pids_of "$c"); before_label=$(labels_for "$c" | head -1)

  # Stop. A forced stop is logged, not hidden: incusd force-kills after its own
  # timeout, and that is exactly how monarch was lost on 2026-11-16.
  say "  stopping (timeout ${STOP_TIMEOUT}s)..."
  local t0 t1 rc
  t0=$(date +%s)
  if ! incus stop "$c" --timeout="$STOP_TIMEOUT" >>"$LOG" 2>&1; then
    say "  stop did not complete cleanly; state is now $(state_of "$c")"
    log "stop returned nonzero for $c"
    if [ "$(state_of "$c")" = "RUNNING" ]; then
      say "  container is still running — leaving it alone"
      return 5
    fi
  fi
  t1=$(date +%s)
  say "  stopped in $((t1-t0))s"

  # Remove the stale generated profile so the next start writes a fresh one.
  local f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    apparmor_parser -R "$f" >>"$LOG" 2>&1 || true
    rm -f "$f" && say "  removed stale profile: $f"
  done < <(profile_files_for "$c")

  if [ "$RELOAD" = "1" ]; then
    say "  reloading apparmor"
    systemctl reload apparmor >>"$LOG" 2>&1 || say "  (reload not available; continuing)"
  fi

  say "  starting..."
  if ! incus start "$c" >>"$LOG" 2>&1; then
    say "  START FAILED — it is down. Investigate before continuing."
    log "start failed for $c"
    return 6
  fi
  if ! wait_running "$c" 120; then
    say "  did not reach RUNNING within 120s — state $(state_of "$c")"
    log "no RUNNING for $c"
    return 7
  fi
  say "  running (pids before=$before_pids after=$(pids_of "$c"))"

  # Exec is the only authority — deliberately NOT the label string.
  #
  # `aa-status` lists every profile still loaded in the kernel, including ones
  # whose processes have all exited, and it truncates long names in its display.
  # So the old name lingering there is expected after a successful repair and is
  # not evidence of failure. Gating on it (as an earlier version of this script
  # did) reports a false negative on a repair that actually worked.
  if label_is_junk "$c"; then
    say "  note: the previous profile name is still loaded in the kernel (informational):"
    labels_for "$c" | sed 's/^/      /'
  fi

  local i=0
  while [ "$i" -lt 4 ]; do
    if exec_ok "$c"; then
      say "  exec:             OK (repaired)"
      settle_check "$c" || say "  WARNING: running but did not settle"
      log "repaired $c (was: $before_label)"
      return 0
    fi
    i=$((i+1)); sleep 3
  done

  say "  exec still fails -> $(exec_reason "$c")"
  say "  the container is RUNNING; services matter more than the shell, so it stays up."
  log "exec still broken for $c"
  return 2
}

run_apply() {
  [ "$WINDOW" = "1" ] || die "--apply requires --window (services restart; this is a maintenance action)"
  head2 "restart window — $(hostname) $(date -Is)"
  say "  targets, least critical first: $TARGETS"
  say "  each container stops for roughly 20-90s; atheniq is the LMS"
  say "  log: $LOG"
  local c r rc=0
  for c in $TARGETS; do
    repair_one "$c"; r=$?
    case "$r" in
      0)  say "  RESULT: repaired" ;;
      3)  say "  RESULT: skipped (not running)" ;;
      4)  say "  RESULT: blocked on config override" ; rc=2 ;;
      *)  say "  RESULT: FAILED (rc=$r)" ; rc=2 ;;
    esac
  done
  head2 "summary"
  for c in $TARGETS; do
    printf '  %-16s state=%-9s exec=%s\n' "$c" "$(state_of "$c")" \
      "$(exec_ok "$c" && echo OK || echo FAILS)"
  done
  return "$rc"
}

case "$MODE" in
  diagnose) run_diagnose; rc=$? ;;
  verify)   run_verify;   rc=$? ;;
  apply)    run_apply;    rc=$? ;;
  *)        die "unknown mode $MODE" ;;
esac

say ""
if [ "$rc" -eq 0 ]; then say "result: OK"; else say "result: INCOMPLETE (rc=$rc) — see $LOG"; fi
exit "$rc"
