#!/bin/bash
#
# restore-proxy.sh — rebuild `proxy` from a restore point written by
# backup-proxy.sh.
#
# Usage (as root, on i2):
#   bash platform-stack/scripts/restore-proxy.sh /var/lib/incus/backups/proxy/<stamp> --replace
#
# Without --replace this refuses to touch an existing `proxy`: a restore is not
# something to do to a live edge by accident. With it, the existing container is
# stopped and deleted first.
#
# The order here is the whole point. The rootfs tarball carries proxy's config,
# including three disk devices (`raw-mount` → /data, `site`, `sitedeploy`) whose
# host paths must exist before the container starts, and those paths must be
# owned by the idmap base rather than root. Restore them out of order, or as
# root, and proxy comes up with an empty /data: NPM serves no vhosts, and every
# symptom points at NPM instead of at the restore.

set -uo pipefail

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf '[%s] FATAL: %s\n' "$(date '+%F %T')" "$*" >&2; exit 1; }

RP=""
REPLACE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --replace) REPLACE=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) [ -z "$RP" ] || die "only one restore point at a time"; RP="$1"; shift ;;
  esac
done
[ -n "$RP" ] || die "usage: restore-proxy.sh <restore-point-dir> [--replace]"
[ -d "$RP" ] || die "no such restore point: $RP"

[ "$(id -u)" -eq 0 ] || die "run as root on the Incus host that holds proxy"
for c in incus tar zstd sha256sum; do
  command -v "$c" >/dev/null 2>&1 || die "missing required command: $c"
done

for f in proxy.tar.zst data.tar.zst deploy.tar.zst units.tar.zst config.yaml manifest.txt; do
  [ -s "$RP/$f" ] || die "restore point is incomplete: $f missing or empty"
done

# Verify before destroying anything: a corrupt tarball discovered halfway through
# a --replace leaves the estate with no edge at all.
log "verifying the restore point against its own manifest"
( cd "$RP" && grep -E '^[0-9a-f]{64}  \./' manifest.txt | sha256sum -c --quiet - ) \
  || die "restore point failed checksum verification"

EXPECT_CONF="$(sed -n 's/^npm proxy_host confs: //p' "$RP/manifest.txt" | tr -d ' ')"
EXPECT_DOCKER="$(sed -n 's/^docker containers: //p' "$RP/manifest.txt" | tr -d ' ')"
EXPECT_CONF="${EXPECT_CONF:-0}"
EXPECT_DOCKER="${EXPECT_DOCKER:-0}"

# The idmap base is what proxy's root maps to on the host; the device sources
# must be owned by it, not by root, or the container cannot write into them.
HOSTID="$(grep -o '"Hostid":[0-9]*' "$RP/config.yaml" | head -1 | cut -d: -f2)"
HOSTID="${HOSTID:-1000000}"

if incus info proxy >/dev/null 2>&1; then
  [ "$REPLACE" -eq 1 ] || die "proxy already exists — pass --replace to rebuild it"
  log "stopping and deleting the existing proxy"
  incus stop proxy --timeout 180 >/dev/null 2>&1 || incus stop proxy --force >/dev/null 2>&1
  [ "$(incus list proxy -c s --format csv | tr -d ' \r')" = "STOPPED" ] \
    || die "could not stop proxy"
  incus delete proxy --force >/dev/null 2>&1 || die "could not delete proxy"
fi

log "importing rootfs and config"
incus import "$RP/proxy.tar.zst" proxy >/dev/null 2>&1 || die "incus import failed"

log "restoring /data (NPM proxy_host confs, custom_ssl, keys.json, database)"
rm -rf /usr/src/proxy/data
mkdir -p /usr/src/proxy
tar --zstd -xpf "$RP/data.tar.zst" -C /usr/src/proxy --numeric-owner \
  || die "could not restore /usr/src/proxy/data"

log "restoring the deploy and site directories"
tar --zstd -xpf "$RP/deploy.tar.zst" -C /usr/src --numeric-owner \
  || die "could not restore /usr/src/innotel-deploy and /usr/src/innotel-site"

log "mapping the restored host paths to the idmap base ($HOSTID)"
chown -R "$HOSTID:$HOSTID" /usr/src/proxy/data /usr/src/innotel-site /usr/src/innotel-deploy \
  || die "could not set ownership on the restored host paths"

# The devices come back inside the tarball, but a hand-built restore point (or a
# config edited since) must not be able to skip them.
ensure_disk() {
  local name="$1" path="$2" src="$3"
  if incus config device get proxy "$name" path >/dev/null 2>&1; then
    incus config device set proxy "$name" source "$src" path "$path" >/dev/null \
      || die "could not point device $name at $src"
  else
    incus config device add proxy "$name" disk source="$src" path="$path" >/dev/null \
      || die "could not add device $name"
  fi
}
ensure_disk raw-mount /data /usr/src/proxy/data
ensure_disk site /usr/src/innotel-site /usr/src/innotel-site
ensure_disk sitedeploy /usr/src/innotel-deploy /usr/src/innotel-deploy

# The export is taken from a clone, which the backup script keeps off autostart.
AUTOSTART="$(sed -n 's/^ *boot.autostart: *//p' "$RP/config.yaml" | head -1 | tr -d '"')"
incus config set proxy boot.autostart "${AUTOSTART:-true}" >/dev/null 2>&1 \
  || log "warning: could not set boot.autostart"

log "starting proxy"
incus start proxy >/dev/null 2>&1 || die "proxy did not start"

for _ in $(seq 1 60); do
  [ "$(incus list proxy -c s --format csv | tr -d ' \r')" = "RUNNING" ] && break
  sleep 3
done
[ "$(incus list proxy -c s --format csv | tr -d ' \r')" = "RUNNING" ] \
  || die "proxy is not RUNNING after start"

# ── What a restored edge has to answer for ───────────────────────────────────
FAIL=0
report() { # label expected actual
  if [ "$2" = "$3" ]; then
    printf '  ok    %-28s %s\n' "$1" "$3"
  else
    printf '  FAIL  %-28s %s (expected %s)\n' "$1" "$3" "$2"
    FAIL=1
  fi
}

addr=""
for _ in $(seq 1 30); do
  addr="$(incus exec proxy -- ip -4 -br addr show eth0 2>/dev/null | grep -o '192\.168\.1\.[0-9]*' | head -1)"
  [ -n "$addr" ] && break
  sleep 3
done
report "edge address" "192.168.1.71" "${addr:-none}"

conf="$(incus exec proxy -- sh -c 'ls /data/nginx/proxy_host/*.conf 2>/dev/null | wc -l' 2>/dev/null | tr -d ' \r')"
report "NPM host confs" "$EXPECT_CONF" "${conf:-0}"

log "waiting for the container's own services"
for _ in $(seq 1 60); do
  n="$(incus exec proxy -- sh -c 'docker ps -q | wc -l' 2>/dev/null | tr -d ' \r')"
  [ "${n:-0}" -ge "$EXPECT_DOCKER" ] && break
  sleep 5
done
report "docker containers" "$EXPECT_DOCKER" "${n:-0}"

site_unit="$(incus exec proxy -- systemctl is-enabled innotel-site.service 2>/dev/null | tr -d '\r')"
report "innotel-site.service" "enabled" "${site_unit:-missing}"
timer="$(incus exec proxy -- systemctl is-active innotel-site-reconcile.timer 2>/dev/null | tr -d '\r')"
report "reconcile timer" "active" "${timer:-inactive}"

code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 20 \
  --resolve innotel.us:443:192.168.1.71 https://innotel.us/ 2>/dev/null)"
report "innotel.us" "200" "${code:-000}"

echo
if [ "$FAIL" -eq 0 ]; then
  log "restore complete: proxy answers as the edge again"
else
  log "restore finished but checks failed — the edge is not fully back"
  exit 1
fi
