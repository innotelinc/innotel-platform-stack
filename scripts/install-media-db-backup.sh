#!/bin/bash
#
# Install the verified media database snapshots.
#
# Why this exists: on 2026-11-17 every Sonarr backup written that day was not a
# zip (98 files, all rejected by `zipfile.ZipFile`) and the newest restorable
# backup was 2026-11-10, so the apps' own backup writer is not a backup. This
# installs the replacement: SQLite's online backup API, an integrity_check on the
# copy, gzip only after it passes, and a keep-N prune.
#
# Run ON the Incus host that carries monarch, from a platform-stack checkout:
#
#   scripts/install-media-db-backup.sh
#
# The snapshot script itself must live inside monarch (the host cannot see a
# running instance's rootfs), so it is pushed there as well as installed here.
#
# Exit codes: 0 installed and the timer is enabled, 1 the container or the
# source script is missing - deliberately not 0, so a run on the wrong host says
# so instead of looking clean.

set -u

CONTAINER=${MONARCH_CONTAINER:-monarch}
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/backup-media-databases.py"
DEST=/usr/local/sbin/backup-media-databases.py

if [ ! -f "$SCRIPT" ]; then
  echo "FAIL cannot find $SCRIPT" >&2
  exit 1
fi
if ! incus info "$CONTAINER" >/dev/null 2>&1; then
  echo "FAIL this host does not carry the $CONTAINER container" >&2
  exit 1
fi

install -m 0755 "$SCRIPT" "$DEST" || exit 1
if ! incus exec "$CONTAINER" -- install -D -m 0755 /dev/stdin "$DEST" < "$SCRIPT"; then
  echo "FAIL could not install the snapshot script inside $CONTAINER" >&2
  exit 1
fi

install -m 0644 "$HERE/../systemd/media-db-backup.service" /etc/systemd/system/ || exit 1
install -m 0644 "$HERE/../systemd/media-db-backup.timer" /etc/systemd/system/ || exit 1
systemctl daemon-reload || exit 1
systemctl enable --now media-db-backup.timer || exit 1

echo "NOTE installed $DEST here and in $CONTAINER; timer enabled"
systemctl list-timers media-db-backup.timer --no-pager | tail -2
