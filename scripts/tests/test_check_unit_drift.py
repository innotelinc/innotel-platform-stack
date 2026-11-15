#!/usr/bin/env python3
"""Unit tests for check-unit-drift.py — the host's units against the repository's.

The property: every unit the estate keeps in `systemd/` and this host actually carries is the
revision's own copy. A unit edited on a host during an incident leaves the job running and the
repository wrong, and nothing else in the estate compares the two.

Two directions that matter as much as the comparison itself:
  * a unit in `systemd/` that is not installed here is another host's job, so silence;
  * a unit installed here that the estate does not keep is Docker's or systemd's, so silence.

Nothing here reaches a host: the unit directory is a temporary directory and the revision is
a throwaway git repository.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-unit-drift.py"

SERVICE = "[Service]\nExecStart=/usr/local/sbin/thing\n"
TIMER = "[Timer]\nOnCalendar=*:0/30\n"


def _load():
    spec = importlib.util.spec_from_file_location("check_unit_drift", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def reader(mapping: dict[str, bytes]):
    return lambda path: mapping.get(path)


class Comparison(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.unit_dir = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def put(self, name: str, body: str) -> None:
        (self.unit_dir / name).write_text(body, encoding="utf-8")

    def test_an_identical_unit_produces_nothing(self):
        self.put("thing.service", SERVICE)
        found, compared = check.compare(
            self.unit_dir, ["thing.service"], reader({"systemd/thing.service": SERVICE.encode()}))
        self.assertEqual((found, compared), ([], 1))

    def test_a_unit_edited_on_the_host_carries_both_digests(self):
        self.put("thing.service", SERVICE + "# edited during an incident\n")
        found, compared = check.compare(
            self.unit_dir, ["thing.service"], reader({"systemd/thing.service": SERVICE.encode()}))
        self.assertEqual(compared, 1)
        self.assertEqual([m.name for m in found], ["thing.service"])
        self.assertNotEqual(found[0].installed, found[0].revision)

    def test_a_unit_the_estate_keeps_but_this_host_does_not_carry_is_silent(self):
        # Every unit in systemd/ belongs to some host; this check runs on one of them.
        found, compared = check.compare(
            self.unit_dir, ["thing.service"], reader({"systemd/thing.service": SERVICE.encode()}))
        self.assertEqual((found, compared), ([], 0))

    def test_a_unit_the_host_carries_that_the_estate_does_not_keep_is_silent(self):
        # Docker's units, systemd's generators: installed here, not this repository's.
        self.put("docker.service", "[Service]\nExecStart=/usr/bin/dockerd\n")
        found, compared = check.compare(self.unit_dir, [], reader({}))
        self.assertEqual((found, compared), ([], 0))

    def test_a_unit_the_revision_dropped_between_listing_and_reading_is_skipped(self):
        self.put("thing.service", SERVICE)
        found, compared = check.compare(self.unit_dir, ["thing.service"], reader({}))
        self.assertEqual((found, compared), ([], 1))

    def test_a_missing_unit_directory_cannot_be_read(self):
        with self.assertRaises(check.Unreadable):
            check.compare(self.unit_dir / "nope", [], reader({}))


class Verdict(unittest.TestCase):
    def test_a_difference_is_a_failure_naming_the_unit_and_both_digests(self):
        result = check.audit([check.Mismatch("thing.service", "aaaa", "bbbb")], 3)
        self.assertFalse(result.ok)
        finding = result.failures[0]
        self.assertEqual(finding.code, "drifted")
        self.assertIn("thing.service", finding.message)
        self.assertIn("aaaa", finding.message)
        self.assertIn("bbbb", finding.message)

    def test_a_host_that_agrees_publishes_how_many_it_compared(self):
        result = check.audit([], 3)
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.findings], ["units_match"])
        self.assertIn("3 unit(s)", result.notes[0].message)

    def test_the_report_names_the_unit_and_the_count(self):
        body = check.report([check.Mismatch("thing.service", "aaaa", "bbbb")], 2, "origin/main")
        self.assertIn("DRIFT", body)
        self.assertIn("1 finding(s) of 2 unit(s)", body)
        self.assertIn("origin/main", body)

    def test_a_clean_report_says_how_many_matched(self):
        self.assertIn("2 unit(s) installed here match", check.report([], 2, "origin/main"))


class EndToEnd(unittest.TestCase):
    """`main` against a real repository, because the listing is `git ls-tree`."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.repo = root / "repo"
        self.unit_dir = root / "systemd"
        (self.repo / "systemd").mkdir(parents=True)
        (self.repo / "systemd" / "thing.service").write_text(SERVICE, encoding="utf-8")
        (self.repo / "systemd" / "other.service").write_text("[Service]\n", encoding="utf-8")
        # --no-verify: the estate installs its own commit hooks, and this repository exists
        # only to be read back by `git show` and `git ls-tree`.
        for args in (["init", "-q"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "add", "."],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                      "--no-verify", "-m", "x"]):
            done = subprocess.run(["git", "-C", str(self.repo), *args],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        self.unit_dir.mkdir()
        (self.unit_dir / "thing.service").write_text(SERVICE, encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def run_check(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(["--unit-dir", str(self.unit_dir), "--repo", str(self.repo),
                               "--ref", "HEAD", *args])
        return code, out.getvalue(), err.getvalue()

    def test_a_host_holding_the_revision_s_copy_exits_zero(self):
        code, out, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("1 unit(s) installed here match", out)

    def test_a_unit_edited_on_the_host_exits_one_and_is_named(self):
        (self.unit_dir / "thing.service").write_text(SERVICE + "# hand edit\n", encoding="utf-8")
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("DRIFT", out)
        self.assertIn("thing.service", out)

    def test_a_revision_it_cannot_read_is_two_not_a_pass(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(["--unit-dir", str(self.unit_dir), "--repo", str(self.repo),
                               "--ref", "no-such-revision"])
        self.assertEqual(code, 2)
        self.assertIn("no no-such-revision", err.getvalue())


if __name__ == "__main__":
    unittest.main()
