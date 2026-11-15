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

import re
import subprocess
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


if __name__ == "__main__":
    unittest.main()
