#!/usr/bin/env python3
"""Take verified SQLite snapshots of the media app databases.

WHY THIS EXISTS
---------------
On 2026-11-17 every Sonarr backup written that day was not a zip: 98 files in
`Backups/scheduled` all failed `zipfile.ZipFile` with "File is not a zip file",
and the newest restorable backup was 2026-11-10. The apps' own backup writer is
therefore not something to rely on, and nothing watched it.

WHAT IT DOES
------------
For each media app that keeps a SQLite database it uses SQLite's own online
backup API (`Connection.backup`), not a file copy: copying a WAL database can
capture a torn state, which is how a corrupt backup looks healthy right up until
the day it is needed. The snapshot is then reopened and `PRAGMA integrity_check`
must return `ok`; only then is it gzipped to its final name. A snapshot that
fails the check is deleted and never published.

Runs INSIDE the monarch container, where /docker/appdata is the appdata tree and
python3 carries sqlite3 (the host cannot see a running instance's rootfs):

    incus exec monarch -- python3 /usr/local/sbin/backup-media-databases.py

Exit codes: 0 every database snapshotted and verified, 1 at least one failed,
2 no database was found at all (wrong host, or the apps moved) - deliberately
not 0, so a broken install does not read as a clean run.
"""

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import time

DEFAULT_APPS = ("autobrr", "bazarr", "lidarr", "prowlarr", "radarr", "sonarr", "whisparr")


def database_path(root, app):
    """Where an app keeps its database inside the appdata tree."""
    return os.path.join(root, app, "{0}.db".format(app))


def snapshot(source_path, snapshot_path):
    """Write a consistent copy of a live database using SQLite's backup API."""
    source = sqlite3.connect("file:{0}?mode=ro".format(source_path), uri=True, timeout=30)
    try:
        target = sqlite3.connect(snapshot_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def integrity(path):
    """Return integrity_check's verdict, or an `unreadable: ...` reason."""
    try:
        connection = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True, timeout=30)
    except sqlite3.Error as error:
        return "unreadable: {0}".format(error)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as error:
        return "unreadable: {0}".format(error)
    finally:
        connection.close()


def publish(snapshot_path, dest_dir, app, stamp):
    """Gzip a verified snapshot into place atomically; returns the final path."""
    final = os.path.join(dest_dir, "{0}-{1}.db.gz".format(app, stamp))
    partial = final + ".part"
    with open(snapshot_path, "rb") as raw, gzip.open(partial, "wb") as compressed:
        shutil.copyfileobj(raw, compressed)
    os.replace(partial, final)
    return final


def prune(dest_dir, app, keep):
    """Delete all but the newest `keep` snapshots for one app; returns removed paths."""
    if keep <= 0:
        return []
    prefix = app + "-"
    names = sorted(
        name for name in os.listdir(dest_dir)
        if name.startswith(prefix) and name.endswith(".db.gz")
    )
    removed = []
    for name in names[:-keep]:
        path = os.path.join(dest_dir, name)
        os.remove(path)
        removed.append(path)
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verified SQLite snapshots of the media app databases")
    parser.add_argument("--root", default=os.environ.get("MEDIA_APPDATA", "/docker/appdata"),
                        help="appdata tree holding <app>/<app>.db (default: /docker/appdata)")
    parser.add_argument("--dest", default=os.environ.get("MEDIA_BACKUP_DIR", "/var/backups/media"),
                        help="where snapshots are written (default: /var/backups/media)")
    parser.add_argument("--keep", type=int, default=14, help="snapshots to keep per app (default: 14)")
    parser.add_argument("--apps", default=",".join(DEFAULT_APPS),
                        help="comma-separated app names to snapshot")
    parser.add_argument("--dry-run", action="store_true", help="report what would be written")
    args = parser.parse_args(argv)

    apps = [name.strip() for name in args.apps.split(",") if name.strip()]
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    if not args.dry_run:
        os.makedirs(args.dest, exist_ok=True)

    failures = 0
    found = 0
    for app in apps:
        source_path = database_path(args.root, app)
        if not os.path.exists(source_path):
            print("NOTE {0}: no database at {1} (skipped)".format(app, source_path))
            continue
        found += 1
        if args.dry_run:
            print("NOTE {0}: would snapshot {1} ({2} bytes)".format(
                app, source_path, os.path.getsize(source_path)))
            continue
        partial = os.path.join(args.dest, ".{0}-{1}.tmp".format(app, stamp))
        try:
            snapshot(source_path, partial)
            verdict = integrity(partial)
            if verdict != "ok":
                raise RuntimeError("snapshot failed integrity_check: {0}".format(verdict))
            final = publish(partial, args.dest, app, stamp)
            removed = prune(args.dest, app, args.keep)
            print("NOTE {0}: {1} verified ({2} bytes), pruned {3}".format(
                app, os.path.basename(final), os.path.getsize(final), len(removed)))
        except (OSError, RuntimeError, sqlite3.Error) as error:
            failures += 1
            print("FAIL {0}: {1}".format(app, error))
            if os.path.exists(partial):
                os.remove(partial)

    print("{0} failure(s)".format(failures))
    if failures:
        return 1
    return 0 if found else 2


if __name__ == "__main__":
    sys.exit(main())
