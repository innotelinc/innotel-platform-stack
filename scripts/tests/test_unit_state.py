#!/usr/bin/env python3
"""`systemctl show` is the only witness for a timer that has stopped.

The estate's companion jobs — the console's AMI reconciler, the music-on-hold config
reconciler and the monthly break-glass drill — are read by `estate_check.unit_state`, and
the fields it returns are
the whole verdict. These cases pin the parsing to the text a systemd 249 host actually
prints (the edge's version, captured 2026-11-25), because a parser that quietly returns
`None` for the last run reads exactly like a job that has never run.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from estate_check import systemd_time, unit_state  # noqa: E402

SHOW = """NextElapseUSecRealtime=Wed 2026-11-25 14:10:16 EST
LoadState=loaded
ActiveState=active
UnitFileState=enabled
Result=success
ExecMainExitTimestamp=Wed 2026-11-25 14:00:23 EST
ExecMainStatus=0
"""


class FakeDone:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class TimeCase(unittest.TestCase):
    def test_a_systemd_timestamp_is_local_seconds(self):
        # The value is compared with its neighbour rather than to a fixed epoch: `mktime`
        # reads the parsed struct as *this* host's local time, so the absolute number
        # depends on where the test runs and the ordering does not.
        earlier = systemd_time("Wed 2026-11-25 14:00:23 EST")
        later = systemd_time("Wed 2026-11-25 15:00:23 EST")
        self.assertIsNotNone(earlier)
        self.assertIsNotNone(later)
        self.assertEqual(int(later - earlier), 3600)
        self.assertGreater(earlier, 1_700_000_000)

    def test_never_is_none_rather_than_an_age_of_zero(self):
        # `n/a` is what a unit that has not run reports; reading it as 1970 would make the
        # job look infinitely stale instead of unstarted.
        for text in ("", "n/a", "never", "0"):
            self.assertIsNone(systemd_time(text), text)

    def test_a_timestamp_without_a_zone_still_parses(self):
        self.assertIsNotNone(systemd_time("Wed 2026-11-25 14:00:23"))


class StateCase(unittest.TestCase):
    def test_the_fields_systemd_prints_are_read(self):
        seen = {}

        def runner(command, **kwargs):
            seen["command"] = command
            return FakeDone(SHOW)

        state = unit_state("console-ami-pin.timer", runner=runner)
        self.assertEqual(
            (state.load, state.active, state.enabled, state.result, state.exit_status),
            ("loaded", "active", "enabled", "success", "0"))
        self.assertTrue(state.enabled_and_active)
        self.assertTrue(state.last_run_ok)
        self.assertIsNotNone(state.exited_at)
        self.assertIn("ExecMainExitTimestamp", seen["command"])

    def test_a_timer_that_is_not_enabled_is_not_scheduled(self):
        text = SHOW.replace("UnitFileState=enabled", "UnitFileState=disabled")
        state = unit_state("console-ami-pin.timer", runner=lambda *a, **k: FakeDone(text))
        self.assertFalse(state.enabled_and_active)

    def test_a_failed_run_is_not_clean(self):
        text = SHOW.replace("Result=success", "Result=exit-code")
        state = unit_state("pbx-breakglass-drill.service",
                           runner=lambda *a, **k: FakeDone(text))
        self.assertFalse(state.last_run_ok)

    def test_an_unreadable_unit_raises_rather_than_looking_healthy(self):
        def runner(command, **kwargs):
            return FakeDone("", returncode=1, stderr="Failed to connect to bus")

        with self.assertRaises(RuntimeError):
            unit_state("console-ami-pin.timer", runner=runner)

    def test_a_missing_systemctl_is_an_error_not_a_pass(self):
        def runner(command, **kwargs):
            raise FileNotFoundError("systemctl")

        with self.assertRaises(RuntimeError):
            unit_state("console-ami-pin.timer", runner=runner)

    def test_the_real_command_is_the_one_a_person_would_run(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["timeout"] = kwargs.get("timeout")
            return FakeDone(SHOW)

        unit_state("pbx-breakglass-drill.timer", runner=runner)
        self.assertEqual(captured["command"][:3], ["systemctl", "show", "pbx-breakglass-drill.timer"])
        self.assertIsInstance(captured["timeout"], int)
        # The subprocess contract is real: these are the keywords `subprocess.run` takes.
        self.assertTrue(issubclass(type(subprocess.CompletedProcess([], 0)), object))


if __name__ == "__main__":
    unittest.main()
