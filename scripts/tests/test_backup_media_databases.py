#!/usr/bin/env python3
"""Tests for scripts/backup-media-databases.py."""

import contextlib
import gzip
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "backup-media-databases.py")

spec = importlib.util.spec_from_file_location("backup_media_databases", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def make_database(path, rows=3):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE Series (Id INTEGER PRIMARY KEY, Title TEXT)")
        connection.executemany("INSERT INTO Series (Title) VALUES (?)",
                               [("Show {0}".format(i),) for i in range(rows)])
        connection.commit()
    finally:
        connection.close()


class DatabasePathTests(unittest.TestCase):
    def test_database_path_is_app_rooted(self):
        self.assertEqual(backup.database_path("/appdata", "sonarr"), "/appdata/sonarr/sonarr.db")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_copies_rows_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "sonarr.db")
            copy = os.path.join(tmp, "copy.db")
            make_database(source, rows=5)
            backup.snapshot(source, copy)
            self.assertEqual(backup.integrity(copy), "ok")
            connection = sqlite3.connect(copy)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM Series").fetchone()[0], 5)
            finally:
                connection.close()

    def test_snapshot_of_wal_database_is_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "radarr.db")
            connection = sqlite3.connect(source)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE Movie (Id INTEGER PRIMARY KEY)")
                connection.executemany("INSERT INTO Movie (Id) VALUES (?)", [(i,) for i in range(50)])
                connection.commit()
                # Rows still sitting in the WAL are the case a plain copy loses.
                backup.snapshot(source, os.path.join(tmp, "copy.db"))
            finally:
                connection.close()
            copy = sqlite3.connect(os.path.join(tmp, "copy.db"))
            try:
                self.assertEqual(copy.execute("SELECT COUNT(*) FROM Movie").fetchone()[0], 50)
            finally:
                copy.close()

    def test_integrity_reports_garbage_instead_of_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken.db")
            with open(broken, "wb") as handle:
                handle.write(b"this is not a sqlite database at all, not even close")
            self.assertNotEqual(backup.integrity(broken), "ok")
            self.assertTrue(backup.integrity(broken).startswith("unreadable:")
                            or backup.integrity(broken) != "ok")


class PublishAndPruneTests(unittest.TestCase):
    def test_publish_gzips_a_readable_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "sonarr.db")
            make_database(source, rows=2)
            snapshot = os.path.join(tmp, "snap.tmp")
            backup.snapshot(source, snapshot)
            final = backup.publish(snapshot, tmp, "sonarr", "20261117T120000")
            self.assertTrue(final.endswith("sonarr-20261117T120000.db.gz"))
            restored = os.path.join(tmp, "restored.db")
            with gzip.open(final, "rb") as compressed, open(restored, "wb") as raw:
                raw.write(compressed.read())
            self.assertEqual(backup.integrity(restored), "ok")

    def test_prune_keeps_the_newest_and_removes_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stamp in ("20261115T000000", "20261116T000000", "20261117T000000"):
                with open(os.path.join(tmp, "sonarr-{0}.db.gz".format(stamp)), "wb") as handle:
                    handle.write(b"x")
            removed = backup.prune(tmp, "sonarr", 2)
            self.assertEqual(len(removed), 1)
            self.assertTrue(removed[0].endswith("20261115T000000.db.gz"))
            remaining = sorted(name for name in os.listdir(tmp) if name.endswith(".db.gz"))
            self.assertEqual(remaining, ["sonarr-20261116T000000.db.gz",
                                         "sonarr-20261117T000000.db.gz"])

    def test_prune_leaves_other_apps_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("sonarr-20261115T000000.db.gz", "radarr-20261115T000000.db.gz",
                         "sonarr-20261116T000000.db.gz"):
                with open(os.path.join(tmp, name), "wb") as handle:
                    handle.write(b"x")
            backup.prune(tmp, "sonarr", 1)
            self.assertIn("radarr-20261115T000000.db.gz", os.listdir(tmp))


class MainTests(unittest.TestCase):
    def test_missing_databases_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = backup.main(["--root", tmp, "--dest", os.path.join(tmp, "out")])
            self.assertEqual(code, 2)
            self.assertIn("no database", output.getvalue())

    def test_real_database_is_snapshotted_and_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = os.path.join(tmp, "appdata")
            os.makedirs(os.path.join(appdata, "sonarr"))
            make_database(os.path.join(appdata, "sonarr", "sonarr.db"), rows=4)
            dest = os.path.join(tmp, "backups")
            os.makedirs(dest)
            for stamp in ("20261115T000000", "20261116T000000"):
                with open(os.path.join(dest, "sonarr-{0}.db.gz".format(stamp)), "wb") as handle:
                    handle.write(b"x")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = backup.main(["--root", appdata, "--dest", dest, "--keep", "2",
                                    "--apps", "sonarr"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(len([n for n in os.listdir(dest) if n.endswith(".db.gz")]), 2)
            self.assertIn("verified", output.getvalue())

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = os.path.join(tmp, "appdata")
            os.makedirs(os.path.join(appdata, "lidarr"))
            make_database(os.path.join(appdata, "lidarr", "lidarr.db"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = backup.main(["--root", appdata, "--dest", os.path.join(tmp, "out"),
                                    "--apps", "lidarr", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "out")))
            self.assertIn("would snapshot", output.getvalue())


if __name__ == "__main__":
    unittest.main()
