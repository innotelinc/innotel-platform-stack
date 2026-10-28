#!/bin/bash
# Move proxy (the estate edge: technitium DNS on .71, NPM ingress, authentik,
# vault, prometheus/grafana, ntfy, omniroute) from i4 to i2.
#
# Supersedes /root/proxy-to-i2.sh on i4. That version predates the /data fix:
# proxy's NPM config lives in a host directory bind-mounted at /data (215
# proxy_host/*.conf, custom_ssl, keys.json, logs). The rootfs rsync cannot carry
# it — `-x` stops at the mount — so this script stages /usr/src/proxy/data
# separately and reproduces the disk device on the destination. Without that,
# proxy would boot on i2 with an empty /data and NPM would serve no vhosts.
#
# 192.168.1.71 is a static address in the container's own netplan and the MAC is
# kept, so DNS answers and NPM's ingress are unchanged after the move.
#
# Usage (on i4): bash platform-stack/scripts/move-proxy-i4-to-i2.sh
#        Log:   /root/move-proxy-i4-to-i2.log

set -u
LOG=/root/move-proxy-i4-to-i2.log
I2=root@192.168.1.52
SRC=/var/lib/incus/storage-pools/tank/containers/proxy/rootfs/
DST=/var/lib/incus/storage-pools/fast/containers/proxy/rootfs/
DATA_SRC=/usr/src/proxy/data/
DATA_DST=/usr/src/proxy/data/
# The password is read from SSHPASS, never written down here: this file is
# committed, and a credential in a tracked script is a credential in every
# clone and every backup of it.
[ -n "${SSHPASS:-}" ] || { echo "FATAL: set SSHPASS before running this" >&2; exit 1; }
RS="sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

log "==================== proxy i4 -> i2: begin ===================="

# ── Preflight ─────────────────────────────────────────────────────────────
incus list proxy -c n,s --format csv | grep -q '^proxy,RUNNING' \
  || { log "FATAL: proxy is not RUNNING on i4"; exit 1; }
$RS $I2 'incus list proxy -c n,s --format csv' 2>>"$LOG" | grep -q '^proxy,STOPPED' \
  || { log "FATAL: destination proxy on i2 is not STOPPED"; exit 1; }
$RS $I2 'mount | grep -q "storage-pools/fast/containers/proxy"' 2>>"$LOG" \
  || { log "FATAL: destination dataset is not mounted"; exit 1; }
[ -d /usr/src/proxy/data ] || { log "FATAL: /usr/src/proxy/data missing on i4"; exit 1; }
log "preflight: source RUNNING, destination STOPPED and mounted, /data present"

# ── Stage the /data tree (small, 140M) ────────────────────────────────────
log "staging /usr/src/proxy/data -> i2 (140M)"
$RS $I2 'mkdir -p /usr/src/proxy/data' >>"$LOG" 2>&1
rsync -aHAX --numeric-ids --delete -e "$RS" "$DATA_SRC" "$I2:$DATA_DST" >>"$LOG" 2>&1
rc=$?
log "data staging rc=$rc"
[ "$rc" -eq 0 ] || { log "FATAL: /data staging failed; nothing stopped"; exit 1; }
# Container root maps host 1000000 -> container 0, so the tree must be owned by
# 1000000 on the host or proxy sees nobody:nogroup and cannot write its config.
$RS $I2 'chown -R 1000000:1000000 /usr/src/proxy/data' >>"$LOG" 2>&1
log "data staging done, ownership remapped"

# ── Stage the rootfs while the edge keeps serving ─────────────────────────
log "staging rsync (live source, LAN, 25G)"
rsync -aHAXx --numeric-ids --delete --stats -e "$RS" "$SRC" "$I2:$DST" >>"$LOG" 2>&1
rc=$?
log "rootfs staging rc=$rc"
if [ "$rc" -ne 0 ]; then
  log "FATAL: rootfs staging failed; nothing stopped, destination left partial"
  exit 1
fi

# ── Cut over: stop, final delta, start on i2 ──────────────────────────────
log "stopping proxy on i4 (the edge goes dark until the destination answers)"
incus config set proxy boot.autostart=false 2>>"$LOG"
incus stop proxy --timeout 300 >>"$LOG" 2>&1 || incus stop proxy --force >>"$LOG" 2>&1
st=$(incus list proxy -c s --format csv)
log "source state: $st"
[ "$st" = "STOPPED" ] || { log "FATAL: could not stop proxy"; exit 1; }

log "final delta (rootfs)"
rsync -aHAXx --numeric-ids --delete --stats -e "$RS" "$SRC" "$I2:$DST" >>"$LOG" 2>&1
rc=$?
log "delta rc=$rc"
if [ "$rc" -ne 0 ]; then
  log "delta failed — restarting proxy on i4"
  incus start proxy >>"$LOG" 2>&1 && log "proxy back on i4" || log "RESTART FAILED — needs a hand"
  exit 1
fi

log "final delta (/data)"
rsync -aHAX --numeric-ids --delete -e "$RS" "$DATA_SRC" "$I2:$DATA_DST" >>"$LOG" 2>&1
log "data delta rc=$?"
$RS $I2 'chown -R 1000000:1000000 /usr/src/proxy/data' >>"$LOG" 2>&1

# ── Reproduce the /data disk device on the destination ────────────────────
log "ensuring raw-mount device on i2 proxy"
$RS $I2 'incus config device show proxy | grep -q "^raw-mount:" || \
  incus config device add proxy raw-mount disk source=/usr/src/proxy/data path=/data' >>"$LOG" 2>&1
log "device: $($RS $I2 'incus config device show proxy' 2>/dev/null | sed -n "/^raw-mount:/,/^[^ ]/p" | tr "\n" " " )"

log "starting proxy on i2"
$RS $I2 'incus start proxy' >>"$LOG" 2>&1 || log "  start returned non-zero"
for i in $(seq 1 30); do
  st=$($RS $I2 'incus list proxy -c s --format csv' 2>/dev/null)
  [ "$st" = "RUNNING" ] && break
  sleep 5
done
log "destination state: ${st:-<none>}"
if [ "$st" != "RUNNING" ]; then
  log "FATAL: destination did not start — proxy left STOPPED on i4 (rollback: incus start proxy)"
  exit 1
fi

# ── Verify the things the estate actually dials ───────────────────────────
ip=""
for i in $(seq 1 24); do
  ip=$($RS $I2 'incus exec proxy -- ip -4 -o addr show eth0 2>/dev/null' 2>/dev/null | grep -oE '192\.168\.1\.[0-9]+' | head -1)
  [ -n "$ip" ] && break
  sleep 5
done
log "destination proxy ipv4: ${ip:-<none>}"

log "checking /data survived the move"
n_conf=$($RS $I2 'incus exec proxy -- sh -c "ls /data/nginx/proxy_host/*.conf 2>/dev/null | wc -l"' 2>/dev/null | tr -d ' \r')
log "  proxy_host conf files: ${n_conf:-0}"

for i in $(seq 1 30); do
  n=$($RS $I2 'incus exec proxy -- docker ps -q 2>/dev/null | wc -l' 2>/dev/null | tr -d ' \r')
  log "  containers up: ${n:-0}"
  [ "${n:-0}" -ge 20 ] && break
  sleep 10
done

log "-- DNS on the moved edge --"
dig +short +time=3 +tries=2 @192.168.1.71 innotel.us A >>"$LOG" 2>&1 || log "  dig failed"
log "-- ingress on the moved edge --"
curl -sk -o /dev/null -w '  npm https 192.168.1.71 -> %{http_code}\n' --max-time 10 https://192.168.1.71 >>"$LOG" 2>&1
curl -sk -o /dev/null -w '  atheniq.innotel.us -> %{http_code}\n' --max-time 10 --resolve atheniq.innotel.us:443:192.168.1.71 https://atheniq.innotel.us >>"$LOG" 2>&1
curl -sk -o /dev/null -w '  classroom.innotel.us -> %{http_code}\n' --max-time 10 --resolve classroom.innotel.us:443:192.168.1.71 https://classroom.innotel.us >>"$LOG" 2>&1
log "-- edge stack --"
$RS $I2 'incus exec proxy -- docker ps --format "{{.Names}}|{{.Status}}" | sort' >>"$LOG" 2>&1

if [ "${n:-0}" -ge 20 ] && [ "$ip" = "192.168.1.71" ] && [ "${n_conf:-0}" -ge 200 ]; then
  log "==================== proxy cutover: done (i4 copy kept STOPPED) ===================="
else
  log "WARNING: containers=${n:-0} ip=${ip:-<none>} confs=${n_conf:-0} — i4 copy left STOPPED for rollback; review before keeping"
fi
