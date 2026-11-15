#!/usr/bin/env python3
"""Unit tests for check-unit-drift.py — the hosts' units against the repository's.

The property: every unit the estate keeps in `systemd/` and a host actually carries is the
revision's own copy. A unit edited on a host during an incident leaves the job running and
the repository wrong, and nothing else in the estate compares the two.

Three silences matter as much as the comparison itself:
  * a unit in `systemd/` that a host does not carry is another host's job;
  * a unit a host carries that the estate does not keep is Docker's or systemd's;
  * a host that cannot be read is a *finding*, not a pass — that is the whole point of
    reaching it.

Nothing here reaches a network: the unit directory is a temporary directory, the revision is
a throwaway repository, and `ssh` is faked where a host has to answer.
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


class FakeSSH:
    """Stands in for `subprocess` while a host answers — or does not."""

    def __init__(self, *, stdout: str = "", returncode: int = 0, error: Exception | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[list[str]] = []

    def __enter__(self):
        self._real = check.subprocess
        check.subprocess = self
        return self

    def __exit__(self, *exc):
        check.subprocess = self._real
        return False

    TimeoutExpired = subprocess.TimeoutExpired

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "no route to host")


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


class Hosts(unittest.TestCase):
    def test_a_host_spec_carries_a_name_and_an_address(self):
        self.assertEqual(check.parse_hosts(["i1=192.168.1.51", "i2=192.168.1.52"]),
                         [("i1", "192.168.1.51"), ("i2", "192.168.1.52")])

    def test_a_bare_address_is_its_own_name(self):
        self.assertEqual(check.parse_hosts(["192.168.1.51"]), [("192.168.1.51", "192.168.1.51")])

    def test_the_listing_becomes_digests(self):
        # `sha256sum` prints `<64 hex>  <name>`; the comparison uses the first 12, the same
        # width the local half reports, so the two read alike in a finding.
        body = "a" * 64 + "  thing.service\n" + "b" * 64 + "  other.timer\n"
        with FakeSSH(stdout=body):
            hashes = check.host_hashes("i1", "192.168.1.51", ["thing.service", "other.timer"])
        self.assertEqual(hashes, {"thing.service": "a" * 12, "other.timer": "b" * 12})

    def test_a_host_that_does_not_answer_cannot_be_read(self):
        # The failure this check must not swallow: an unreachable host has no verdict.
        with FakeSSH(error=subprocess.TimeoutExpired(["ssh"], 8)):
            with self.assertRaises(check.Unreadable):
                check.host_hashes("i1", "192.168.1.51", ["thing.service"])

    def test_a_host_that_answers_with_an_error_cannot_be_read(self):
        with FakeSSH(returncode=255):
            with self.assertRaises(check.Unreadable):
                check.host_hashes("i1", "192.168.1.51", ["thing.service"])

    def test_a_host_digest_that_differs_is_drift_on_that_host(self):
        found = check.compare_hashes("i1", {"thing.service": "aaaa"},
                                     reader({"systemd/thing.service": SERVICE.encode()}))
        self.assertEqual([(m.host, m.name) for m in found], [("i1", "thing.service")])
        self.assertEqual(found[0].label, "i1:thing.service")

    def test_a_host_digest_that_matches_the_revision_is_silent(self):
        revision = check.digest(SERVICE.encode())
        self.assertEqual(check.compare_hashes(
            "i1", {"thing.service": revision},
            reader({"systemd/thing.service": SERVICE.encode()})), [])


class Verdict(unittest.TestCase):
    def test_a_difference_is_a_failure_naming_the_unit_and_both_digests(self):
        result = check.audit([check.Mismatch("thing.service", "aaaa", "bbbb", "i1")], 3, [])
        self.assertFalse(result.ok)
        finding = result.failures[0]
        self.assertEqual(finding.code, "drifted")
        self.assertIn("i1:thing.service", finding.message)
        self.assertIn("aaaa", finding.message)
        self.assertIn("bbbb", finding.message)

    def test_an_unreachable_host_is_a_failure_of_its_own(self):
        result = check.audit([], 2, ["i2 (192.168.1.52) could not be read: timeout"])
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["unreachable"])
        self.assertIn("were not compared", result.failures[0].message)

    def test_a_host_that_agrees_publishes_how_many_it_compared(self):
        result = check.audit([], 3, [])
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.findings], ["units_match"])
        self.assertIn("3 unit(s)", result.notes[0].message)

    def test_the_report_names_the_unit_the_host_and_the_count(self):
        body = check.report([check.Mismatch("thing.service", "aaaa", "bbbb", "i1")], 2, [],
                            "origin/main", ["i1", "i2"])
        self.assertIn("i1:thing.service", body)
        self.assertIn("1 finding(s) of 2 unit(s)", body)
        self.assertIn("this host, i1, i2", body)

    def test_a_clean_report_says_which_hosts_were_compared(self):
        body = check.report([], 2, [], "origin/main", ["i1"])
        self.assertIn("2 unit(s) on this host, i1 match", body)


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
        self.assertIn("1 unit(s) on this host match", out)

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
