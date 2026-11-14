#!/bin/bash
#
# Keep monarch's Transmission category folders writable by the user that owns
# them.
#
# Why this exists: when the download folders under /data/torrents are deleted,
# whatever recreates them (the stack, or an interactive session) does so as
# root:root 755. transmission-daemon runs as uid 1000 (PUID), so every category
# folder then refuses writes and the WebUI reports
#
#   Couldn't get '<file>': Permission denied (13)
#
# The fault appears while the stack is running, not at start, so a start-time
# hook cannot catch it: a timer can. Only mismatched entries are touched, so the
# steady-state cost is a metadata walk of a small tree.
#
# It runs through `incus exec` rather than the storage-pool path: a running
# container's rootfs is not reliably visible at its dataset mountpoint, and the
# container's own view is the only one that matters.
#
# Run ON i1 (the Incus host that carries monarch). Install:
#
#   install -m 0755 monarch-torrent-perms.sh /usr/local/sbin/
#   install -m 0644 monarch-torrent-perms.service /etc/systemd/system/
#   install -m 0644 monarch-torrent-perms.timer   /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now monarch-torrent-perms.timer
#
# Exit codes: 0 nothing to do or everything repaired, 1 the tree is there but
# repairs did not stick, 2 this host does not have the container or the tree
# (not i1, or monarch moved) - deliberately not 0, so a run on the wrong host
# says so instead of looking clean.

set -u

CONTAINER=${MONARCH_CONTAINER:-monarch}
OWNER=${MONARCH_PUID:-1000}
GROUP=${MONARCH_PGID:-1000}
DIR=/data/torrents

if ! incus info "$CONTAINER" >/dev/null 2>&1; then
  echo "no container '$CONTAINER' on this host" >&2
  exit 2
fi

if ! incus exec "$CONTAINER" -- test -d "$DIR" >/dev/null 2>&1; then
  echo "'$CONTAINER' has no $DIR" >&2
  exit 2
fi

counts=$(incus exec "$CONTAINER" -- sh -c "
set -u
cd $DIR || exit 2
before=\$(find . \( ! -uid $OWNER -o ! -gid $GROUP \) | wc -l)
if [ \"\$before\" -gt 0 ]; then
  find . \( ! -uid $OWNER -o ! -gid $GROUP \) -exec chown $OWNER:$GROUP {} + 2>/dev/null
fi
modes=\$(find . -type d ! -perm 2775 | wc -l)
if [ \"\$modes\" -gt 0 ]; then
  find . -type d ! -perm 2775 -exec chmod 2775 {} + 2>/dev/null
fi
files=\$(find . -type f ! -perm 664 | wc -l)
if [ \"\$files\" -gt 0 ]; then
  find . -type f ! -perm 664 -exec chmod 664 {} + 2>/dev/null
fi
after=\$(find . \( ! -uid $OWNER -o ! -gid $GROUP \) | wc -l)
echo \"\$before \$modes \$files \$after\"
") || {
  echo "could not run the repair inside '$CONTAINER'" >&2
  exit 1
}

read -r owner_fixed mode_fixed file_fixed remaining <<<"$counts"

if [ "${remaining:-1}" -gt 0 ]; then
  echo "repair incomplete: $remaining entries still not $OWNER:$GROUP under $CONTAINER:$DIR" >&2
  exit 1
fi

if [ "${owner_fixed:-0}" -gt 0 ] || [ "${mode_fixed:-0}" -gt 0 ] || [ "${file_fixed:-0}" -gt 0 ]; then
  logger -t monarch-torrent-perms \
    "repaired $OWNER:$GROUP entries under $CONTAINER:$DIR (owner=$owner_fixed dirs=$mode_fixed files=$file_fixed)"
  echo "repaired owner=$owner_fixed dirs=$mode_fixed files=$file_fixed"
else
  echo "clean"
fi
