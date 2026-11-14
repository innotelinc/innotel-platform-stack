#!/bin/bash
# swap-ceiling-check — flags an Incus instance that is filling its *enforced*
# memory or swap ceiling. Runs on each estate host from a timer; prints nothing
# when everything is comfortable, so a non-empty log is the alert.
#
# Why: on 2026-10-25 atheniq sat at 1.57 GB of its 2 GiB swap for five hours after
# a boot burst, and nothing noticed. A hard ceiling is a bound — when the instance
# reaches it the next allocation is an OOM kill inside its own cgroup (the
# intended, local failure, audit §15.1) — so it has to be watched *before* it is
# reached, not explained afterwards.
#
# The ceiling is read from the cgroup, not from `incus config get`: with
# `limits.memory.enforce: soft` Incus programs no memory.max/memory.swap.max at
# all, and the configured number is a target, not a bound. The previous version
# compared swap usage against the configured number regardless, so an instance
# with soft enforcement read as "122 % of ceiling — an OOM kill is one burst away"
# every ten minutes while its cgroup limits were literally `max`. A check that
# cries wolf about an unenforced limit trains people to ignore it.
#
# Memory is measured as the **working set** — anon + slab + shmem + sock from
# memory.stat — not memory.current, which counts reclaimable page cache and would
# cry wolf on a host that is merely warm.
#
# Thresholds, per instance (only where the cgroup actually enforces a cap):
#   swap    >= 50 % of memory.swap.max   WARN   (>= 80 %: CRIT)
#   working >= 90 % of memory.max        WARN   (>= 97 %: CRIT)
# And for the host, because a soft-capped instance can still eat the host's swap:
#   host swap >= 75 % used               WARN   (>= 90 %: CRIT), naming the top
#                                        consumer
# Soft-capped instances with unlimited cgroups are not alerts; they are reported
# in the docs as capacity notes. The check never changes or restarts anything.
set -uo pipefail

# Overridable so the branches can be exercised on a comfortable host (see the
# verification note in docs/incus-ceiling-check.md); production uses the defaults.
WARN_PCT=${WARN_PCT:-50}
CRIT_PCT=${CRIT_PCT:-80}
MEM_WARN_PCT=${MEM_WARN_PCT:-90}
MEM_CRIT_PCT=${MEM_CRIT_PCT:-97}
HOST_WARN_PCT=${HOST_WARN_PCT:-75}
HOST_CRIT_PCT=${HOST_CRIT_PCT:-90}
LOG=${LOG:-/var/log/swap-ceiling-check.log}

say() { printf '%s %s %s\n' "$(date -Is)" "$(hostname)" "$*"; }

# cgroup v2 writes "max" for unlimited; that is not a ceiling of zero bytes.
cgroup_num() {
  case "${1:-}" in
    ''|max) echo 0 ;;
    *[0-9]) echo "$1" ;;
    *) echo 0 ;;
  esac
}

pct() { # $1 = used, $2 = limit
  [ "${2:-0}" -gt 0 ] 2>/dev/null || { echo 0; return; }
  awk -v u="$1" -v l="$2" 'BEGIN{printf "%d", (u*100)/l}'
}

alerts=0
top_name=""; top_swap=0

for c in $(incus list --format csv -c n,s 2>/dev/null | awk -F, '$2=="RUNNING"{print $1}'); do
  mem_max=$(cgroup_num "$(incus exec "$c" -- cat /sys/fs/cgroup/memory.max 2>/dev/null | tr -d '\r')")
  sw_max=$(cgroup_num "$(incus exec "$c" -- cat /sys/fs/cgroup/memory.swap.max 2>/dev/null | tr -d '\r')")
  stats=$(incus exec "$c" -- cat /sys/fs/cgroup/memory.stat 2>/dev/null | tr -d '\r')
  sw_cur=$(incus exec "$c" -- cat /sys/fs/cgroup/memory.swap.current 2>/dev/null | tr -d '\r')
  [ -n "$sw_cur" ] || sw_cur=0
  # cgroup v2 memory.stat reports bytes: anon + slab + shmem + sock
  working=$(printf '%s\n' "$stats" | awk '
    $1=="anon"||$1=="slab"||$1=="shmem"||$1=="sock" {s+=$2}
    END{printf "%.0f", s}')
  working=${working:-0}

  if [ "$sw_cur" -gt "$top_swap" ] 2>/dev/null; then top_swap=$sw_cur; top_name=$c; fi

  if [ "$sw_max" -gt 0 ] && [ "$sw_cur" -gt 0 ]; then
    p=$(pct "$sw_cur" "$sw_max")
    if [ "$p" -ge "$CRIT_PCT" ]; then
      say "CRIT  $c swap ${p}% of the enforced ceiling ($((sw_cur/1048576)) MiB of $((sw_max/1048576)) MiB) — an OOM kill is one burst away"; alerts=$((alerts+1))
    elif [ "$p" -ge "$WARN_PCT" ]; then
      say "WARN  $c swap ${p}% of the enforced ceiling ($((sw_cur/1048576)) MiB of $((sw_max/1048576)) MiB)"; alerts=$((alerts+1))
    fi
  fi

  if [ "$mem_max" -gt 0 ] && [ "$working" -gt 0 ]; then
    p=$(pct "$working" "$mem_max")
    if [ "$p" -ge "$MEM_CRIT_PCT" ]; then
      say "CRIT  $c working set ${p}% of the enforced cap ($((working/1048576)) MiB of $((mem_max/1048576)) MiB)"; alerts=$((alerts+1))
    elif [ "$p" -ge "$MEM_WARN_PCT" ]; then
      say "WARN  $c working set ${p}% of the enforced cap ($((working/1048576)) MiB of $((mem_max/1048576)) MiB)"; alerts=$((alerts+1))
    fi
  fi
done

# A soft-capped instance has no instance ceiling to approach, but it can still
# exhaust the one shared resource it cannot see: the host's swap.
# /proc/meminfo has no "Swap:" line; it reports SwapTotal and SwapFree only.
read -r host_used host_total < <(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{if (t) printf "%s %s\n", t-f, t}' /proc/meminfo 2>/dev/null)
host_used=${host_used:-0}
host_total=${host_total:-0}
if [ "${host_total:-0}" -gt 0 ] 2>/dev/null; then
  p=$(pct "$host_used" "$host_total")
  consumer=""
  [ -n "$top_name" ] && consumer="; top consumer ${top_name} $((top_swap/1048576)) MiB"
  if [ "$p" -ge "$HOST_CRIT_PCT" ]; then
    say "CRIT  host swap ${p}% used ($((host_used/1024)) MiB of $((host_total/1024)) MiB)${consumer}"; alerts=$((alerts+1))
  elif [ "$p" -ge "$HOST_WARN_PCT" ]; then
    say "WARN  host swap ${p}% used ($((host_used/1024)) MiB of $((host_total/1024)) MiB)${consumer}"; alerts=$((alerts+1))
  fi
fi

[ "$alerts" -gt 0 ] && exit 1
exit 0
