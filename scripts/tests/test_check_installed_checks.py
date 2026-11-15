#!/usr/bin/env python3
"""Unit tests for check-installed-checks.py — what runs is what the revision says.

The property is the one nothing else asserts on a timer: the files under the install
directory are the ones `scripts/` at the revision carries. A copy can be replaced after a
clean install and every check keeps publishing (measured 2026-11-15), so the failure has to
be observable, not inferred — installed vs revision, both ways, with a revision it cannot
read refused rather than treated as "nothing to compare".

Everything runs against throwaway directories and a local git repository; nothing here
reaches a host.
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

CHECK = SCRIPTS / "check-installed-checks.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_installed_checks", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

ONE = "# check one, the revision's copy\n"
TWO = "# check two, the revision's copy\n"
UNIT_TEXT = (
    "[Service]\n"
    "ExecStart=-/usr/bin/python3 /opt/innotel/estate-checks/check-one.py "
    "--prom ${TEXTFILE_DIR}/estate-check-one.prom\n")
TIMER_TEXT = "[Timer]\nOnCalendar=*:0/30\n"


def reader(mapping: dict[str, bytes]):
    return lambda name: mapping.get(name)


class Comparison(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def put(self, name: str, body: str) -> Path:
        path = self.directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_an_identical_copy_produces_nothing(self):
        self.put("check-one.py", ONE)
        self.assertEqual(check.compare(self.directory, ["check-one.py"],
                                       reader({"check-one.py": ONE.encode()})), [])

    def test_a_copy_that_changed_after_the_install_carries_both_digests(self):
        self.put("check-one.py", ONE + "# hand edit\n")
        found = check.compare(self.directory, ["check-one.py"],
                              reader({"check-one.py": ONE.encode()}))
        self.assertEqual([m.kind for m in found], ["drifted"])
        self.assertNotEqual(found[0].installed, found[0].revision)

    def test_a_file_the_revision_dropped_is_unknown(self):
        self.put("check-old.py", "# retired\n")
        found = check.compare(self.directory, [], reader({}))
        self.assertEqual([(m.kind, m.name) for m in found], [("unknown", "check-old.py")])

    def test_a_file_the_unit_names_and_the_directory_lacks_is_missing(self):
        found = check.compare(self.directory, ["check-one.py"], reader({"check-one.py": ONE.encode()}))
        self.assertEqual([(m.kind, m.name) for m in found], [("missing", "check-one.py")])

    def test_a_backup_beside_a_check_is_not_drift(self):
        self.put("check-one.py", ONE)
        self.put("check-one.py.bak-20261115", "older\n")
        self.assertEqual(check.compare(self.directory, ["check-one.py"],
                                       reader({"check-one.py": ONE.encode()})), [])

    def test_a_missing_directory_cannot_be_read(self):
        with self.assertRaises(check.Unreadable):
            check.compare(self.directory / "nope", [], reader({}))


class UnitComparison(unittest.TestCase):
    """The unit files are the other half of the list: same three kinds, one directory over."""

    SERVICE = "systemd/estate-checks.service"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.unit_dir = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def put(self, text: str) -> None:
        (self.unit_dir / "estate-checks.service").write_text(text, encoding="utf-8")

    def test_an_identical_unit_produces_nothing(self):
        self.put(UNIT_TEXT)
        self.assertEqual(check.compare_units(
            self.unit_dir, reader({self.SERVICE: UNIT_TEXT.encode()})), [])

    def test_a_unit_edited_on_the_host_is_drifted(self):
        # The mistake this closes: the install directory is right, the unit naming it is not,
        # and every check keeps publishing while systemd runs an older job.
        self.put(UNIT_TEXT + "# a hand edit on the host\n")
        found = check.compare_units(self.unit_dir, reader({self.SERVICE: UNIT_TEXT.encode()}))
        self.assertEqual([(m.kind, m.name) for m in found],
                         [("drifted", "estate-checks.service")])
        self.assertEqual(found[0].where, self.SERVICE)
        self.assertNotEqual(found[0].installed, found[0].revision)

    def test_a_unit_the_revision_has_and_the_host_does_not_is_missing(self):
        found = check.compare_units(self.unit_dir, reader({self.SERVICE: UNIT_TEXT.encode()}))
        self.assertEqual([(m.kind, m.name) for m in found],
                         [("missing", "estate-checks.service")])

    def test_a_unit_the_revision_dropped_is_unknown(self):
        self.put(UNIT_TEXT)
        found = check.compare_units(self.unit_dir, reader({}))
        self.assertEqual([(m.kind, m.name) for m in found],
                         [("unknown", "estate-checks.service")])

    def test_a_unit_neither_side_has_is_not_a_finding(self):
        self.assertEqual(check.compare_units(self.unit_dir, reader({})), [])


class Verdict(unittest.TestCase):
    def test_each_kind_is_a_failure_and_carries_the_file(self):
        result = check.audit([check.Mismatch("drifted", "check-one.py", "aaaa", "bbbb"),
                              check.Mismatch("unknown", "check-old.py", "cccc"),
                              check.Mismatch("missing", "check-two.py")])
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures],
                         ["drifted", "unknown", "missing"])
        self.assertIn("check-one.py", result.failures[0].message)
        self.assertIn("aaaa", result.failures[0].message)
        self.assertIn("bbbb", result.failures[0].message)
        self.assertIn("check-two.py", result.failures[2].message)

    def test_an_agreeing_install_is_one_note(self):
        result = check.audit([])
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.findings], ["installed_matches"])
        self.assertEqual(result.notes[0].level, "note")

    def test_the_report_names_each_kind_and_the_revision(self):
        body = check.report([check.Mismatch("drifted", "check-one.py", "aaaa", "bbbb"),
                             check.Mismatch("unknown", "check-old.py", "cccc"),
                             check.Mismatch("missing", "check-two.py")], "origin/main")
        self.assertIn("DRIFT", body)
        self.assertIn("UNKNOWN", body)
        self.assertIn("MISSING", body)
        self.assertIn("3 finding(s) against origin/main", body)

    def test_a_clean_report_says_so(self):
        self.assertIn("OK", check.report([], "origin/main"))


class EndToEnd(unittest.TestCase):
    """`main` against a real repository, because the reader is `git show` and not a stand-in."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.repo = root / "repo"
        self.installed = root / "installed"
        self.unit_dir = root / "systemd"
        self.unit_dir.mkdir()
        self.unit = self.unit_dir / "estate-checks.service"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts" / "check-one.py").write_text(ONE, encoding="utf-8")
        (self.repo / "scripts" / "estate_check.py").write_text("# shared\n", encoding="utf-8")
        (self.repo / "systemd").mkdir()
        (self.repo / "systemd" / "estate-checks.service").write_text(UNIT_TEXT, encoding="utf-8")
        (self.repo / "systemd" / "estate-checks.timer").write_text(TIMER_TEXT, encoding="utf-8")
        # --no-verify: the estate installs its own commit hooks, and this repository
        # exists only to be read back by `git show`.
        for args in (["init", "-q"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "add", "."],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-verify",
                      "-m", "x"]):
            done = subprocess.run(["git", "-C", str(self.repo), *args],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        self.installed.mkdir()
        (self.installed / "check-one.py").write_text(ONE, encoding="utf-8")
        (self.installed / "estate_check.py").write_text("# shared\n", encoding="utf-8")
        self.unit.write_text(UNIT_TEXT, encoding="utf-8")
        (self.unit_dir / "estate-checks.timer").write_text(TIMER_TEXT, encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def run_check(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(["--dir", str(self.installed), "--repo", str(self.repo),
                               "--ref", "HEAD", "--unit", str(self.unit), *args])
        return code, out.getvalue(), err.getvalue()

    def test_an_install_that_matches_exits_zero(self):
        code, out, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("every installed check and both unit files match", out)

    def test_a_changed_copy_exits_one_and_is_named(self):
        (self.installed / "check-one.py").write_text(ONE + "# edited\n", encoding="utf-8")
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("DRIFT", out)
        self.assertIn("check-one.py", out)

    def test_a_unit_edited_on_the_host_exits_one_and_is_named(self):
        self.unit.write_text(UNIT_TEXT + "# a hand edit on the host\n", encoding="utf-8")
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("DRIFT", out)
        self.assertIn("estate-checks.service", out)

    def test_a_timer_missing_from_the_host_is_named(self):
        (self.unit_dir / "estate-checks.timer").unlink()
        code, out, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)
        self.assertIn("estate-checks.timer", out)

    def test_a_revision_it_cannot_read_is_two_not_a_pass(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(["--dir", str(self.installed), "--repo", str(self.repo),
                               "--ref", "no-such-revision", "--unit", str(self.unit)])
        self.assertEqual(code, 2)
        self.assertIn("no no-such-revision", err.getvalue())

    def test_a_directory_that_is_not_there_is_two(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(["--dir", str(self.installed / "gone"), "--repo", str(self.repo),
                               "--ref", "HEAD", "--unit", str(self.unit)])
        self.assertEqual(code, 2)
        self.assertIn("no install directory", err.getvalue())


if __name__ == "__main__":
    unittest.main()
