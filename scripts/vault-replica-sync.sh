#!/bin/bash
# i1: keep /srv/vault-replica (zfs, internal disk) as a mirror of /srv/vault (the USB).
#
# This is the answer to "one spinning USB disk is a single point of failure for every
# template": if the drive dies, the replica still has the last good copy.
#
# The dangerous case is the USB being absent: /srv/vault is then just an empty
# directory on the root fs, and a careless rsync --delete would wipe the replica.
# The mountpoint test below makes that impossible.
set -uo pipefail
VAULT=/srv/vault
REPLICA=/srv/vault-replica
# Not /root: a /root cleanup on 2026-10-25 removed both this script and its log,
# and the timer then failed at exec for three weeks without anyone noticing.
LOG=/var/log/vault-replica-sync.log
lock=/run/vault-replica-sync.lock

exec 9>"$lock" || exit 1
flock -n 9 || { echo "already running"; exit 0; }

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >>"$LOG"; }

mountpoint -q "$VAULT" || { log "SKIP: $VAULT is not a mountpoint (drive absent) — replica untouched"; exit 0; }
[ -d "$REPLICA" ] || { log "ERROR: $REPLICA missing"; exit 1; }
[ -n "$(ls -A "$VAULT" 2>/dev/null)" ] || { log "SKIP: $VAULT is empty — refusing to mirror an empty vault"; exit 1; }

before=$(df --output=used -k "$VAULT" | tail -1)
log "sync start: vault $(du -sh "$VAULT" 2>/dev/null | cut -f1) -> replica"
# -S keeps the sparse holes of the disk images instead of expanding them on zfs
rsync -aS --delete --info=stats2 "$VAULT"/ "$REPLICA"/ >>"$LOG" 2>&1
rc=$?
# 24 = "some files vanished before they could be transferred", which is normal
# against a vault that is being written to while it is mirrored. It is a warning,
# not a failed mirror; anything else still fails hard.
if [ "$rc" -eq 24 ]; then
  log "rsync rc=24 (files vanished mid-transfer) — treated as a warning"
  rc=0
fi
log "rsync rc=$rc; replica now $(du -sh "$REPLICA" 2>/dev/null | cut -f1); vault used ${before}K"
exit $rc
