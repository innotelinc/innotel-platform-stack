#!/usr/bin/env python3
"""The estate checks run side by side, and the unit still lists every one of them.

Two things are being defended here, and the second is the one worth the tests.

The runner: `add` records a command, `drain` runs the queue concurrently, and nothing about
that may change a check's verdict — a check that fails still leaves its textfile, still does
not stop the others, and still does not fail the unit (the unit's own comment says the
finding is the textfile; the `-` on every `ExecStart=` says the same thing). A timeout has to
be *reported* rather than left running, because the whole point of the ceiling is a check
that wedges and would otherwise hold the queue open for ever.

The unit: its `ExecStart=` lines are the estate's install inventory. `install-estate-checks.sh`
copies the files those lines name and `check-installed-checks.py` holds them to `origin/main`
the same way, so a check that stopped being named — or a queue that swallowed the names into
an argument list — would install a directory that no longer contains the checks. That is a
`203/EXEC` on every run with every file, unit and timer in place, which is the trap the
installer exists for. `UnitKeepsItsInventory` asserts the names survive on the lines, not
merely in the file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-estate-checks.py"
UNIT = ROOT / "systemd" / "estate-checks.service"


def load():
    """The runner as a module. Its filename is a command name, not an identifier."""
    spec = importlib.util.spec_from_file_location("run_estate_checks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerCase(unittest.TestCase):
    """A spool and an invocation of our own, so no test sees another's queue."""

    def setUp(self) -> None:
        self.module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = Path(self.directory.name) / "spool"
        self.spool.mkdir()
        self.patch_env("ESTATE_CHECK_SPOOL", str(self.spool))
        self.patch_env("INVOCATION_ID", "test-run")

    def patch_env(self, key: str, value: str) -> None:
        saved = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.pop(key, None) if saved is None
                        else os.environ.__setitem__(key, saved))

    def quiet(self) -> None:
        """`drain` prints as it goes; keep the test output readable."""
        self.patch_env("_", "")
        saved = sys.stdout
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(lambda: (sys.stdout.close(), setattr(sys, "stdout", saved)))

    # -- the queue ---------------------------------------------------------------

    def test_drain_runs_every_queued_command(self) -> None:
        out = self.spool / "marks"
        for name in ("check-one.py", "check-two.py"):
            self.assertEqual(0, self.module.add(
                [sys.executable, "-c", f"open({str(out)!r}, 'a').write('{name}\\n')"]))
        self.assertEqual(0, self.module.drain(2, 60))
        self.assertEqual(["check-one.py", "check-two.py"],
                         sorted(out.read_text().split()))

    def test_a_check_that_fails_does_not_stop_the_others(self) -> None:
        """The finding is the textfile, and every check publishes every run."""
        out = self.spool / "ran"
        self.module.add([sys.executable, "-c", "raise SystemExit('no')"])
        self.module.add([sys.executable, "-c", f"open({str(out)!r}, 'w').write('ran')"])
        self.assertEqual(0, self.module.drain(2, 60))
        self.assertEqual("ran", out.read_text(), "the failing check stopped the queue")

    def test_a_wedged_check_is_reported_rather_than_left_running(self) -> None:
        self.module.add([sys.executable, "-c", "import time; time.sleep(30)"])
        started = time.monotonic()
        self.assertEqual(0, self.module.drain(1, timeout=0.4))
        self.assertLess(time.monotonic() - started, 10, "drain outlived its own timeout")
        # The queue is gone either way, so the next run cannot replay the wedged command.
        self.assertFalse(self.module.run_dir().exists())

    def test_jobs_is_a_ceiling_rather_than_a_decorative_number(self) -> None:
        """Six at a time on the edge, and never more however many checks are queued."""
        events = self.spool / "events"
        script = (
            "import time, pathlib\n"
            f"log = pathlib.Path({str(events)!r})\n"
            "log.open('a').write(f's {time.time()}\\n')\n"
            "time.sleep(0.3)\n"
            "log.open('a').write(f'e {time.time()}\\n')\n"
        )
        for _ in range(9):
            self.module.add([sys.executable, "-c", script])
        self.assertEqual(0, self.module.drain(3, 60))
        marks = sorted((float(stamp), kind)
                       for kind, stamp in (line.split() for line in events.read_text().splitlines()))
        running = peak = 0
        for _, kind in marks:
            running += 1 if kind == "s" else -1
            peak = max(peak, running)
        self.assertLessEqual(peak, 3, f"{peak} checks ran together with --jobs 3")
        self.assertGreaterEqual(peak, 2, "the queue never ran anything side by side")

    def test_two_invocations_do_not_read_each_others_lists(self) -> None:
        out = self.spool / "who"
        self.module.add([sys.executable, "-c", f"open({str(out)!r}, 'a').write('first\\n')"])
        self.patch_env("INVOCATION_ID", "other-run")
        self.module.add([sys.executable, "-c", f"open({str(out)!r}, 'a').write('second\\n')"])
        self.assertEqual(0, self.module.drain(1, 60))
        self.assertEqual(["second"], out.read_text().split())
        self.patch_env("INVOCATION_ID", "test-run")
        self.assertEqual(0, self.module.drain(1, 60))
        self.assertEqual(["second", "first"], out.read_text().split())

    def test_a_crashed_runs_queue_is_pruned_rather_than_replayed(self) -> None:
        """A `systemctl restart` mid-drain leaves a directory nobody will ever drain."""
        stale = self.spool / "crashed"
        stale.mkdir(parents=True)
        (stale / "0000.json").write_text(json.dumps({"argv": [sys.executable, "-c", "pass"]}))
        old = time.time() - 3600
        os.utime(stale, (old, old))
        fresh = self.spool / "in-flight"
        fresh.mkdir()
        self.module.add([sys.executable, "-c", "pass"])
        self.assertFalse(stale.exists(), "a stale queue was left to be replayed")
        self.assertTrue(fresh.exists(), "a run still filling its queue was deleted")

    def test_a_drain_with_nothing_queued_says_so_rather_than_passing(self) -> None:
        self.assertEqual(2, self.module.drain(2, 10))

    def test_add_refuses_an_empty_command(self) -> None:
        self.assertEqual(2, self.module.add([]))

    def test_a_queued_command_lands_private(self) -> None:
        self.module.add([sys.executable, "-c", "pass"])
        queued = next(self.module.run_dir().glob("*.json"))
        self.assertEqual(0o600, stat.S_IMODE(queued.stat().st_mode))


class UnitKeepsItsInventory(unittest.TestCase):
    """What the installer and `check-installed-checks.py` derive the file list from."""

    def setUp(self) -> None:
        self.text = UNIT.read_text(encoding="utf-8")
        self.execs = [line for line in self.text.splitlines() if line.startswith("ExecStart=")]

    def test_every_check_is_still_named_on_its_own_execstart(self) -> None:
        named = {name for line in self.execs
                 for name in re.findall(r"/opt/innotel/estate-checks/(check-[A-Za-z0-9._-]+)", line)}
        self.assertGreater(len(named), 30, "the unit lost its inventory of checks")
        for name in named:
            line = next(l for l in self.execs if name in l)
            self.assertIn("run-estate-checks.py add", line,
                          f"{name} runs in order again: {line}")

    def test_the_drain_is_the_last_execstart_and_the_only_one(self) -> None:
        drains = [line for line in self.execs if "run-estate-checks.py drain" in line]
        self.assertEqual(1, len(drains), "more than one drain would run the queue twice")
        self.assertEqual(drains[0], self.execs[-1], "a check submitted after the drain never runs")

    def test_the_queue_is_bounded_and_does_not_outlive_a_reboot(self) -> None:
        jobs = re.search(r"^Environment=ESTATE_CHECK_JOBS=(\d+)$", self.text, re.M)
        self.assertIsNotNone(jobs, "the drain has no jobs setting")
        self.assertLessEqual(int(jobs.group(1)), 8,
                             "the edge is 2 vCPU and every check measures it")
        spool = re.search(r"^Environment=ESTATE_CHECK_SPOOL=(\S+)$", self.text, re.M)
        self.assertIsNotNone(spool, "the drain and the submits would disagree about the spool")
        self.assertTrue(spool.group(1).startswith("/run/"),
                        "a queue on disk is replayed after a reboot")

    def test_every_submit_keeps_the_checks_dash(self) -> None:
        """A submit without `-` makes one unhappy check stop the whole run."""
        for line in self.execs:
            if "run-estate-checks.py add" in line:
                self.assertTrue(line.startswith("ExecStart=-"),
                                f"a check that is unhappy today would stop the run: {line}")


if __name__ == "__main__":
    unittest.main()
