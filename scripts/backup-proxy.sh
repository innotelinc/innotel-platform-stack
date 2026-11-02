#!/bin/bash
#
# backup-proxy.sh — write a restore point for `proxy`, the estate edge.
#
# `proxy` (192.168.1.71, on i2) is the one container whose loss is an estate
# event: NPM ingress for every vhost, Authentik, Technitium DNS, Vault,
# Prometheus/Grafana and the units that keep innotel.us up all live inside it.
# Three of its directories are host paths rather than part of its rootfs
# (`/data` = NPM's proxy_host confs, custom_ssl, keys.json and database;
# `/usr/src/innotel-site`; `/usr/src/innotel-deploy`), so `incus export` alone
# is not a restore point — a container rebuilt from it comes up with an empty
# /data, NPM serves no vhosts, and every symptom points at NPM instead of at the
# restore.
#
# So this writes all of it to one timestamped directory, checks its own work,
# and leaves a manifest that restore-proxy.sh verifies before it rebuilds
# anything.
#
#   proxy.tar.zst    incus export: rootfs + config + devices, one consistent point
#   data.tar.zst     /usr/src/proxy/data (NPM config, custom_ssl, keys, sqlite)
#   deploy.tar.zst   /usr/src/innotel-deploy and /usr/src/innotel-site
#   units.tar.zst    /etc/systemd/system from inside proxy (the units are in the
#                    rootfs, so a hand rebuild has to know which ones mattered)
#   config.yaml      incus config show --expanded, for a rebuild without the tarball
#   inventory.txt    what was running, so a restore can be compared against it
#   manifest.txt     sizes, sha256 and the counts this script checked
#
# The export is taken from a snapshot, never from the running filesystem: the
# snapshot is copy-on-write and instant, a clone is created from it, exported,
# and deleted again. The live edge is never stopped, and what lands in the
# tarball is one point in time rather than 18G of files read while Docker writes.
#
# Usage (as root, on i2):
#   bash platform-stack/scripts/backup-proxy.sh
#   bash platform-stack/scripts/backup-proxy.sh --offsite root@192.168.1.51:/var/backups/estate/proxy
#   bash platform-stack/scripts/backup-proxy.sh --keep 5
#
# Defaults: restore points under /var/lib/incus/backups/proxy (override with
# PROXY_BACKUP_DIR), the newest 3 kept, no offsite copy.
#
# An offsite copy is what makes this a backup rather than a do-over on the same
# pool: the restore point lives on `fast`, which is where the container lives.

set -uo pipefail

BASE="${PROXY_BACKUP_DIR:-/var/lib/incus/backups/proxy}"
KEEP=3
OFFSITE=""

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf '[%s] FATAL: %s\n' "$(date '+%F %T')" "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --keep)
      [ $# -ge 2 ] || die "--keep needs a number"
      KEEP="$2"; shift 2 ;;
    --offsite)
      [ $# -ge 2 ] || die "--offsite needs root@host:/path"
      OFFSITE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,60p' "$0"; exit 0 ;;
    *)
      echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$KEEP" in ''|*[!0-9]*) die "--keep must be a positive number" ;; esac
[ "$KEEP" -ge 1 ] || die "--keep must be a positive number"

[ "$(id -u)" -eq 0 ] || die "run as root on the Incus host that holds proxy"
for c in incus tar zstd sha256sum; do
  command -v "$c" >/dev/null 2>&1 || die "missing required command: $c"
done

STATE="$(incus list proxy -c s --format csv 2>/dev/null | tr -d ' \r')"
[ "$STATE" = "RUNNING" ] || die "proxy is not RUNNING on this host (state: ${STATE:-absent})"

for d in /usr/src/proxy/data /usr/src/innotel-site /usr/src/innotel-deploy; do
  [ -d "$d" ] || die "host path missing: $d (a restore point without it is not a restore point)"
done

STAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
OUT="$BASE/$STAMP"
SNAP="rp-$STAMP"
CLONE="proxy-rp-$STAMP"

mkdir -p "$OUT" || die "cannot create $OUT"

cleanup() {
  if [ -n "$CLONE" ]; then
    incus delete "$CLONE" --force >/dev/null 2>&1 || log "warning: could not delete clone $CLONE"
    CLONE=""
  fi
  if [ -n "$SNAP" ]; then
    incus snapshot delete proxy "$SNAP" >/dev/null 2>&1 || log "warning: could not delete snapshot $SNAP"
    SNAP=""
  fi
}
trap cleanup EXIT INT TERM

log "restore point: $OUT"

# ── The rootfs, from a snapshot ───────────────────────────────────────────────
# A clone of the snapshot is a stopped instance, so the export is both
# consistent and unaffected by whatever Docker is writing while it runs. The
# autostart flag is cleared the moment the clone exists: a host reboot during
# the export must not bring up a second container claiming 192.168.1.71.
log "snapshotting proxy (copy-on-write, the edge keeps serving)"
incus snapshot create proxy "$SNAP" >/dev/null || die "could not snapshot proxy"

log "cloning the snapshot for a consistent export"
incus copy "proxy/$SNAP" "$CLONE" >/dev/null || die "could not clone the snapshot"
incus config set "$CLONE" boot.autostart=false >/dev/null 2>&1 \
  || log "warning: could not clear boot.autostart on the clone"

log "exporting rootfs + config (18G, a few minutes)"
if ! incus export "$CLONE" "$OUT/proxy.tar.zst" --compression zstd --instance-only \
     >"$OUT/export.log" 2>&1; then
  tail -20 "$OUT/export.log" >&2
  die "incus export failed (log: $OUT/export.log)"
fi
log "exported $(du -h "$OUT/proxy.tar.zst" | cut -f1)"

incus delete "$CLONE" --force >/dev/null 2>&1 || log "warning: could not delete clone $CLONE"
CLONE=""
incus snapshot delete proxy "$SNAP" >/dev/null 2>&1 || log "warning: could not delete snapshot $SNAP"
SNAP=""

# ── The host paths the rootfs does not carry ──────────────────────────────────
log "archiving /data (NPM proxy_host confs, custom_ssl, keys.json, database)"
tar --zstd -cf "$OUT/data.tar.zst" --numeric-owner -C /usr/src/proxy data \
  || die "could not archive /usr/src/proxy/data"

log "archiving the deploy and site directories"
tar --zstd -cf "$OUT/deploy.tar.zst" --numeric-owner -C /usr/src \
  innotel-deploy innotel-site || die "could not archive /usr/src/innotel-*"

# ── The units that live inside the rootfs ─────────────────────────────────────
# Pulled out deliberately: the tarball has them, but nobody unpacks 18G to read
# a unit file. This copy is what a hand rebuild compares against.
log "pulling the unit files out of proxy"
if incus exec proxy -- tar -C /etc -cf /tmp/proxy-units.tar systemd/system \
     >/dev/null 2>&1 \
   && incus file pull proxy/tmp/proxy-units.tar "$OUT/units.tar" >/dev/null 2>&1 \
   && zstd -q -f "$OUT/units.tar" -o "$OUT/units.tar.zst" \
   && rm -f "$OUT/units.tar"; then
  incus exec proxy -- rm -f /tmp/proxy-units.tar >/dev/null 2>&1 || true
else
  die "could not capture the unit files from inside proxy"
fi

# ── Reference and inventory ───────────────────────────────────────────────────
incus config show --expanded proxy >"$OUT/config.yaml" 2>/dev/null \
  || die "could not read proxy's config"
[ -s "$OUT/config.yaml" ] || die "proxy's config came back empty"

{
  echo "# proxy restore-point inventory — $STAMP (UTC)"
  echo "# host: $(hostname)"
  echo
  echo "## addresses"
  incus exec proxy -- ip -4 -br addr
  echo
  echo "## docker containers"
  incus exec proxy -- docker ps --format '{{.Names}} {{.Image}}'
  echo
  echo "## counts"
  echo "docker containers: $(incus exec proxy -- sh -c 'docker ps -q | wc -l' | tr -d ' \r')"
  echo "npm proxy_host confs: $(incus exec proxy -- sh -c 'ls /data/nginx/proxy_host/*.conf 2>/dev/null | wc -l' | tr -d ' \r')"
  echo
  echo "## unit files that matter on a rebuild"
  incus exec proxy -- systemctl list-unit-files --no-legend 'innotel*' 'authentik*'
  echo
  echo "## unit state"
  incus exec proxy -- systemctl is-enabled innotel-site.service
  incus exec proxy -- systemctl is-active innotel-site-reconcile.timer
} >"$OUT/inventory.txt" 2>&1

CONF_COUNT="$(sed -n 's/^npm proxy_host confs: //p' "$OUT/inventory.txt" | tr -d ' ')"
DOCKER_COUNT="$(sed -n 's/^docker containers: //p' "$OUT/inventory.txt" | tr -d ' ')"
CONF_COUNT="${CONF_COUNT:-0}"
DOCKER_COUNT="${DOCKER_COUNT:-0}"

# ── Check the work before claiming it is one ──────────────────────────────────
log "verifying the artifacts"
for f in proxy data deploy units; do
  [ -s "$OUT/$f.tar.zst" ] || die "$f.tar.zst is missing or empty"
  zstd -t "$OUT/$f.tar.zst" >/dev/null 2>&1 || die "$f.tar.zst fails its integrity check"
done

ARCHIVED_CONF="$(tar --zstd -tf "$OUT/data.tar.zst" 2>/dev/null \
  | grep -cE '^data/nginx/proxy_host/[^/]+\.conf$')"
[ "$ARCHIVED_CONF" -eq "$CONF_COUNT" ] \
  || die "archive holds $ARCHIVED_CONF NPM host confs, live has $CONF_COUNT"

for member in innotel-deploy/docker-compose.yml innotel-site/index.html; do
  tar --zstd -tf "$OUT/deploy.tar.zst" 2>/dev/null | grep -qx "$member" \
    || die "deploy archive is missing $member"
done
for unit in systemd/system/innotel-site.service systemd/system/innotel-site-reconcile.timer; do
  tar --zstd -tf "$OUT/units.tar.zst" 2>/dev/null | grep -qx "$unit" \
    || die "unit archive is missing $unit"
done

{
  echo "# proxy restore point $STAMP (UTC)"
  echo "# host: $(hostname) — written by scripts/backup-proxy.sh"
  echo
  echo "## artifacts"
  ( cd "$OUT" && sha256sum ./proxy.tar.zst ./data.tar.zst ./deploy.tar.zst \
      ./units.tar.zst ./config.yaml ./inventory.txt )
  echo
  echo "## sizes"
  du -h "$OUT"/proxy.tar.zst "$OUT"/data.tar.zst "$OUT"/deploy.tar.zst "$OUT"/units.tar.zst
  echo
  echo "## counts"
  echo "docker containers: $DOCKER_COUNT"
  echo "npm proxy_host confs: $CONF_COUNT"
  echo "data bytes: $(du -sb /usr/src/proxy/data | cut -f1)"
  echo
  echo "## checks passed"
  echo "zstd -t on all four archives"
  echo "archived NPM host confs == live ($CONF_COUNT)"
  echo "deploy archive holds docker-compose.yml and the site content"
  echo "unit archive holds the innotel-site units"
} >"$OUT/manifest.txt"

log "verified: $CONF_COUNT NPM host confs, $DOCKER_COUNT containers, $(du -sh "$OUT" | cut -f1)"

# ── Offsite copy ──────────────────────────────────────────────────────────────
if [ -n "$OFFSITE" ]; then
  case "$OFFSITE" in
    *:*) ;;
    *) die "--offsite must look like root@host:/var/backups/estate/proxy" ;;
  esac
  REMOTE="${OFFSITE%%:*}"
  RDIR="${OFFSITE#*:}"
  case "$RDIR" in
    */proxy|*/proxy/) ;;
    *) die "offsite path must end in /proxy, so a prune there cannot hit anything else" ;;
  esac
  command -v rsync >/dev/null 2>&1 || die "rsync is needed for --offsite"
  log "copying the restore point to $OFFSITE"
  rsync -a --partial "$OUT/" "${OFFSITE%/}/$STAMP/" || die "offsite copy failed"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$REMOTE" \
    "cd '$RDIR' && ls -1d */ 2>/dev/null | sort | head -n -$KEEP | xargs -r rm -rf" \
    || log "warning: could not prune old restore points on $REMOTE"
  log "offsite copy done"
fi

# ── Keep the newest N locally ─────────────────────────────────────────────────
mapfile -t points < <(find "$BASE" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
while [ "${#points[@]}" -gt "$KEEP" ]; do
  old="${points[0]}"
  log "pruning $BASE/$old (keeping the newest $KEEP)"
  rm -rf "${BASE:?}/$old"
  points=("${points[@]:1}")
done

log "restore point ready: $OUT"
log "rebuild with: bash platform-stack/scripts/restore-proxy.sh $OUT --replace"
