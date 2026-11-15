#!/usr/bin/env python3
"""The installer's list must be the unit's list — plus the files its checks read.

`scripts/install-estate-checks.sh` fills `/opt/innotel/estate-checks/` from this checkout,
and the interesting property is not that it copies files: it is that the set it copies is
derived from `systemd/estate-checks.service` rather than typed beside it. So the assertion
is the derivation — every script an `ExecStart=` names is installed, every absolute path the
unit passes as an argument is installed, and the shared module the checks import is
installed.

The last part is the one that fails loudest if it is missed. `check-stalwart-oidc-preflight.py`
streams `stalwart-oidc-discovery-setup.sh` into the mail container instead of reimplementing
the runbook's rules, which is why it needs a sibling that is not a `check-*.py` and not
named in the unit. That dependency is invisible to the derivation, so it is held here: every
`with_name(...)` literal in the checks has to appear in the installer's list.

`--print` is the interface the test uses, so the list is read from the script rather than
reimplemented in the test — a test that reimplemented it would pass over the same mistake
twice.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UNIT = ROOT / "systemd" / "estate-checks.service"
INSTALLER = SCRIPTS / "install-estate-checks.sh"

INSTALL_DIR = "/opt/innotel/estate-checks"
#: The one argument that makes the installer list its work instead of doing it.
PRINT = "--print"


def printed() -> list[str]:
    """What the installer would copy, as basenames."""
    done = subprocess.run(["bash", str(INSTALLER), PRINT], capture_output=True, text=True,
                          check=False)
    if done.returncode != 0:
        raise AssertionError(f"{INSTALLER.name} {PRINT} exited {done.returncode}: "
                             f"{done.stderr.strip()}")
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


class InstallerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = printed()
        cls.unit = UNIT.read_text(encoding="utf-8")

    def test_the_list_is_not_empty_and_has_no_duplicates(self):
        # Everything below passes trivially over an empty list, and duplicates would mean
        # the deduplication broke and one file is installed twice.
        self.assertGreater(len(self.files), 10)
        self.assertEqual(sorted(set(self.files)), sorted(self.files))

    def test_every_script_the_unit_runs_is_installed(self):
        self.assertEqual(sorted(set(re.findall(
            rf"{re.escape(INSTALL_DIR)}/(check-[a-z0-9-]+\.py)", self.unit)) - set(self.files)),
            [], "the unit runs a check the installer does not copy")

    def test_every_path_the_unit_passes_to_a_check_is_installed(self):
        # `--pins`, `--redirect-pins` and `--expect` name tables inside the install
        # directory. A check that starts without its table is a check that fails every run
        # with nothing wrong on the host, which is the class of mistake this file closes.
        named = set(re.findall(rf"{re.escape(INSTALL_DIR)}/([A-Za-z0-9._-]+\.tsv)", self.unit))
        self.assertTrue(named, "the unit passes no table — has the parser drifted?")
        self.assertEqual(sorted(named - set(self.files)), [],
                         "the unit hands a check a table the installer does not copy")

    def test_the_shared_module_travels_with_the_checks(self):
        # Every check imports `estate_check` from its own directory, so a copy without it
        # is 29 scripts that all fail to import — and the unit's `-` on each ExecStart
        # turns that into 29 silent textfiles rather than one error.
        self.assertIn("estate_check.py", self.files)

    def test_a_sibling_a_check_streams_is_installed_too(self):
        # The specific one, named rather than merely derived: the preflight check exits 2
        # without this file beside it, and this is the file that made a deploy step
        # necessary in the first place.
        self.assertIn("stalwart-oidc-discovery-setup.sh", self.files)

    def test_every_sibling_the_checks_load_is_installed(self):
        # The general case behind the one above. `with_name(...)` in a check means "a file
        # beside me"; the installer's list is the only thing that puts one there.
        wanted = {
            name
            for path in sorted(SCRIPTS.glob("check-*.py"))
            for name in re.findall(r'with_name\(\s*"([^"]+)"\s*\)', path.read_text(encoding="utf-8"))
        }
        self.assertEqual(sorted(wanted - set(self.files)), [],
                         "a check loads a sibling file the installer does not copy")

    def test_every_listed_file_exists_in_the_checkout(self):
        # The other direction: a list entry with no source is a deploy that prints MISSING
        # and exits 1 — better caught here than on the edge at apply time.
        self.assertEqual(sorted(name for name in self.files if not (SCRIPTS / name).is_file()),
                         [])

    def test_it_resolves_its_own_checkout_from_any_cwd(self):
        # The derivation reads the unit from *this* repository, so where it is run from
        # must not matter: `sudo` runs it from `/` and a deploy step that only works from
        # the repo root is a deploy step that fails at the moment it is used. (The old
        # assertion here was the opposite — that running from `/` is refused — which was
        # wrong about the script: it derives `root` from `BASH_SOURCE`, so it does work.)
        done = subprocess.run(["bash", str(INSTALLER), PRINT], capture_output=True, text=True,
                              check=False, cwd="/", env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), self.files)


class DriftGuard(unittest.TestCase):
    """What lands has to be the revision's copy, not merely *a* copy of it.

    A copy can be right in every run and still be the wrong file — an uncommitted edit, a
    checkout on a branch, or a deployed check silently replaced an hour after it landed.
    None of that shows up in the checks themselves, so the installer holds every file it
    lands to `scripts/<name>` at the revision, and `--verify` repeats the comparison later.
    These tests drive both halves against a throwaway install directory.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.dest = Path(self.dir.name) / "installed"
        # The unit goes somewhere that is not /etc during a test: installing it into the live
        # directory would make the suite rewrite the host's own job.
        self.unit_dest = Path(self.dir.name) / "systemd"

    def tearDown(self):
        self.dir.cleanup()

    def run_installer(self, *args: str) -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ, DEST=str(self.dest), UNIT_DEST=str(self.unit_dest))
        return subprocess.run(["bash", str(INSTALLER), *args], capture_output=True,
                              text=True, check=False, env=env)

    def install(self) -> None:
        done = self.run_installer("--ref", "HEAD")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_fresh_install_verifies_clean(self):
        self.install()
        done = self.run_installer("--verify", "--ref", "HEAD")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("DRIFT", done.stderr)

    def test_a_check_that_changed_after_the_install_is_named(self):
        self.install()
        target = self.dest / "check-alert-delivery.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# hand edit\n", encoding="utf-8")
        done = self.run_installer("--verify", "--ref", "HEAD")
        self.assertEqual(done.returncode, 1)
        self.assertIn("DRIFT", done.stderr)
        self.assertIn("check-alert-delivery.py", done.stderr)

    def test_a_check_the_revision_dropped_is_drift_too(self):
        self.install()
        (self.dest / "check-retired-thing.py").write_text("# retired\n", encoding="utf-8")
        done = self.run_installer("--verify", "--ref", "HEAD")
        self.assertEqual(done.returncode, 1)
        self.assertIn("check-retired-thing.py", done.stderr)

    def test_drift_is_accepted_only_when_said_out_loud(self):
        self.install()
        (self.dest / "check-alert-delivery.py").write_text("# replaced\n", encoding="utf-8")
        accepted = self.run_installer("--verify", "--ref", "HEAD", "--allow-drift")
        self.assertEqual(accepted.returncode, 0)
        self.assertIn("allow-drift", accepted.stderr)

    def test_reinstalling_clears_the_drift(self):
        self.install()
        (self.dest / "check-alert-delivery.py").write_text("# replaced\n", encoding="utf-8")
        self.install()
        done = self.run_installer("--verify", "--ref", "HEAD")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_revision_it_cannot_resolve_is_refused(self):
        # Failing open would print `ok` for a copy it never compared, which is worse than
        # having no guard at all.
        done = self.run_installer("--verify", "--ref", "no-such-revision")
        self.assertEqual(done.returncode, 2)
        self.assertIn("cannot resolve", done.stderr)

    def test_verify_without_an_install_directory_is_refused(self):
        done = self.run_installer("--verify", "--ref", "HEAD")
        self.assertEqual(done.returncode, 2)
        self.assertIn("no install directory", done.stderr)

    def test_the_unit_is_installed_beside_the_checks(self):
        # The unit and the checks are one list in two files. A unit left in a checkout is a
        # timer running yesterday's ExecStart lines beside today's scripts, and every check
        # still reports: the drift this whole file is about, one file over.
        self.install()
        installed = self.unit_dest / "estate-checks.service"
        self.assertEqual(installed.read_bytes(), UNIT.read_bytes())
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o644)

    def test_the_timer_travels_with_the_unit(self):
        # A service installed without its timer never runs, and that is silent.
        self.install()
        self.assertEqual((self.unit_dest / "estate-checks.timer").read_bytes(),
                         (ROOT / "systemd" / "estate-checks.timer").read_bytes())

    def test_a_unit_that_changed_after_the_install_is_named(self):
        self.install()
        target = self.unit_dest / "estate-checks.service"
        target.write_text(target.read_text(encoding="utf-8") + "\n# hand edit\n",
                          encoding="utf-8")
        done = self.run_installer("--verify", "--ref", "HEAD")
        self.assertEqual(done.returncode, 1)
        self.assertIn("DRIFT", done.stderr)
        self.assertIn("estate-checks.service", done.stderr)

    def test_a_unit_missing_beside_the_checks_is_named(self):
        self.install()
        (self.unit_dest / "estate-checks.service").unlink()
        done = self.run_installer("--verify", "--ref", "HEAD")
        self.assertEqual(done.returncode, 1)
        self.assertIn("MISSING", done.stderr)
        self.assertIn("estate-checks.service", done.stderr)

    def test_an_install_that_changes_nothing_leaves_the_unit_alone(self):
        # Rewriting identical bytes bumps the mtime, systemd reads that as a changed unit,
        # and `NeedDaemonReload=yes` on a host where nothing moved is the kind of standing
        # warning that teaches people to ignore the warning.
        self.install()
        installed = self.unit_dest / "estate-checks.service"
        before = installed.stat()
        second = self.run_installer("--ref", "HEAD")
        self.assertEqual(second.returncode, 0, second.stderr)
        after = installed.stat()
        self.assertEqual((after.st_mtime_ns, after.st_ino), (before.st_mtime_ns, before.st_ino))
        self.assertIn("(unchanged)", second.stdout)

    def test_an_install_starts_nothing(self):
        # Reloading re-reads the text; starting is what would hide the 203/EXEC the checklist
        # is about, and that stays a person's step. A fake `systemctl` on PATH is the only way
        # to see a call the script makes without waiting for one it does not.
        fake = Path(self.dir.name) / "bin"
        fake.mkdir()
        marker = Path(self.dir.name) / "systemctl-called"
        (fake / "systemctl").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {marker}\n",
                                          encoding="utf-8")
        (fake / "systemctl").chmod(0o755)
        env = dict(os.environ, DEST=str(self.dest), UNIT_DEST=str(self.unit_dest),
                   PATH=f"{fake}:{os.environ['PATH']}")
        done = subprocess.run(["bash", str(INSTALLER), "--ref", "HEAD"], capture_output=True,
                              text=True, check=False, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(marker.exists(), "the installer called systemctl")
        self.assertIn("systemctl start estate-checks.service", done.stdout)

    def test_an_unknown_argument_is_refused(self):
        done = self.run_installer("--yes-please")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown argument", done.stderr)


if __name__ == "__main__":
    unittest.main()
