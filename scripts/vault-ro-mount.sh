#!/bin/bash
# i1: create/refresh the read-only view of the vault at boot.
#
# ontrak's `vault` device points at /srv/vault-ro, which is a bind of /srv/vault with
# the ro flag -- that is what makes "read-only" a kernel guarantee instead of a hope.
# A bind mount does not survive a reboot, and /srv/vault-ro would otherwise be an empty
# directory that ontrak happily binds, so this runs before incus starts.
#
# On 2026-11-14 the drive dropped off the USB bus, re-enumerated as /dev/sdc and left
# `srv-vault.mount` inactive. /srv/vault was then an *empty directory*, this script's
# `mountpoint` test passed the same check it would pass for a mounted drive only by
# being false, and the ro view -- plus nginx and the NFS export behind it -- served
# nothing while looking healthy. `nofail` is the right fstab option (a boot must not
# wait for a USB disk) but it makes "absent" indistinguishable from "fine" for
# everything downstream. The sentinel below is what tells them apart: it lives on the
# filesystem, so it is present only when the filesystem is.
set -u
# Overridable so the guard can be exercised against a scratch bind mount rather than
# by unmounting the real vault: production sets nothing and gets these paths.
VAULT=${VAULT:-/srv/vault}
ROVIEW=${ROVIEW:-/srv/vault-ro}
SENTINEL="$VAULT/${SENTINEL_NAME:-.vault-id}"
SENTINEL_TEXT=${SENTINEL_TEXT:-innotel-vault-v1}
log() { echo "$*"; }

# The drive may be absent (nofail in fstab). Nothing is served in that case, because
# the read-only view is not created either -- say so and leave the path as a plain dir.
if ! mountpoint -q "$VAULT"; then
  # ...but do not leave an old ro view behind: that bind would now be serving the
  # empty directory under the vault's path.
  if mountpoint -q "$ROVIEW"; then
    umount "$ROVIEW" 2>/dev/null && log "removed stale read-only view: $ROVIEW (vault not mounted)"
  fi
  log "SKIP: $VAULT is not mounted (drive absent) -- no read-only view created"
  exit 0
fi

# Mounted is not the same as *the vault*. A filesystem mounted at the path that is not
# ours (a wrong label, a blank replacement disk) would pass every check below.
if [ "$(cat "$SENTINEL" 2>/dev/null)" != "$SENTINEL_TEXT" ]; then
  mountpoint -q "$ROVIEW" && umount "$ROVIEW" 2>/dev/null
  log "ERROR: $VAULT is mounted but carries no '$SENTINEL_TEXT' sentinel ($SENTINEL)"
  log "ERROR: refusing to publish $ROVIEW -- the mount is not the artifact vault"
  exit 1
fi

mkdir -p "$ROVIEW"
mountpoint -q "$ROVIEW" && umount "$ROVIEW" 2>/dev/null

mount --bind "$VAULT" "$ROVIEW" || { log "ERROR: bind failed"; exit 1; }
mount -o remount,bind,ro "$ROVIEW" || { log "ERROR: remount ro failed"; exit 1; }

# Prove it rather than assume it.
if touch "$ROVIEW/.write-probe" 2>/dev/null; then
  rm -f "$ROVIEW/.write-probe"
  log "ERROR: $ROVIEW is writable -- expected read-only"
  exit 1
fi
log "read-only view ready: $ROVIEW ($(findmnt -no OPTIONS "$ROVIEW"))"
