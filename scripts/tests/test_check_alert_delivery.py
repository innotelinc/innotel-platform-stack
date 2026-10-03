#!/usr/bin/env python3
"""Unit tests for check-alert-delivery.py — proving the receiver *talks*, not just parses.

The failure this check exists to catch has no symptom: Alertmanager passes
`amtool check-config` with `SUCCESS` while delivering nowhere, so the whole estate is
silent and the UI looks healthy. What is worth locking here is exactly that shape —
that "no delivery in the window" is a **failure** (never a quiet pass), that the probe
is injected and attributed to this run, and that the receiver's own last notify error
rides along in the finding so the reason is not a second investigation. The mail-log
parser also has to survive the ANSI colour escapes Stalwart writes ahead of every
stamp; without that the check read a delivering receiver as silent (measured
2026-10-02), which is the same bug in the other direction.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-alert-delivery.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_alert_delivery", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()

#: A log line as Stalwart writes it: the RFC3339 UTC stamp leads, but an ANSI colour
#: escape usually precedes it — the exact thing that made an unanchored match necessary.
DELIVERED = '\x1b[37m2026-10-03T02:00:05Z\x1b[0m INFO Delivery completed (delivery.completed) queueId = q1, from = "alertmanager@innotel.us", to = ["admin@innotel.us"]\n'
OLD_DELIVERY = "2020-01-01T00:00:00Z INFO Delivery completed (delivery.completed) queueId = q0, from = \"alertmanager@innotel.us\", to = [\"admin@innotel.us\"]\n"


class CommandCase(unittest.TestCase):
    def test_the_probe_name_is_unique_per_run(self):
        self.assertNotEqual(probe.probe_name("a"), probe.probe_name("b"))

    def test_the_amtool_time_is_seconds_only(self):
        # A fractional-second stamp (what `datetime.isoformat()` produces) is accepted by
        # amtool but misread — the probe's startsAt came out equal to its end, so the alert
        # never fired and a working receiver looked silent (measured 2026-10-02).
        moment = datetime(2026, 10, 3, 2, 0, 5, 123456, tzinfo=timezone.utc)
        self.assertEqual(probe._amtool_time(moment), "2026-10-03T02:00:05Z")

    def test_the_injection_goes_through_alertmanagers_own_api(self):
        now = datetime(2026, 10, 3, 2, 0, 0, tzinfo=timezone.utc)
        command = probe.inject_command(probe.probe_name("x"), now, now)
        self.assertIn("amtool alert add", command)
        self.assertIn("--alertmanager.url=", command)
        self.assertIn(probe.probe_name("x"), command)

    def test_the_last_error_read_never_fails_on_a_receiver_with_no_error(self):
        # A receiver that has never failed has no such log line; "nothing to show" must be
        # an empty answer, not an ssh failure, or a healthy receiver reads as unreadable.
        self.assertIn("|| true", probe.last_error_command())

    def test_the_mail_log_read_never_fails_on_a_quiet_day(self):
        self.assertIn("|| true", probe.mail_log_command())


class ParseCase(unittest.TestCase):
    def test_a_stamp_survives_the_ansi_colour_escape(self):
        self.assertTrue(probe.delivery_times(DELIVERED))

    def test_an_unparseable_line_is_skipped(self):
        self.assertEqual(probe.delivery_times("not a log line at all\n"), [])

    def test_stamps_come_back_newest_last(self):
        times = probe.delivery_times(OLD_DELIVERY + DELIVERED)
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), 2)


class AuditCase(unittest.TestCase):
    def test_a_delivered_probe_is_clean(self):
        result = probe.audit(True, "")
        self.assertTrue(result.ok)
        self.assertEqual(result.failures, [])

    def test_no_delivery_is_a_failure_and_carries_the_reason(self):
        result = probe.audit(False, "Notify attempt failed: connection refused")
        self.assertFalse(result.ok)
        self.assertIn("connection refused", result.failures[0].message)

    def test_no_delivery_with_no_evidence_is_still_a_failure(self):
        result = probe.audit(False, "")
        self.assertEqual(len(result.failures), 1)


class SeriesCase(unittest.TestCase):
    def test_the_delivered_metric_is_one_when_delivered(self):
        body = probe.delivery_series(True, 3.0)
        self.assertIn('innotel_estate_check_alert_delivered{check="alert_delivery"} 1', body)

    def test_a_silent_receiver_publishes_zero(self):
        body = probe.delivery_series(False, 90.0)
        self.assertIn('innotel_estate_check_alert_delivered{check="alert_delivery"} 0', body)

    def test_the_wait_is_published_only_when_measured(self):
        self.assertNotIn("alert_delivery_seconds", probe.delivery_series(False, None))


class RunProbeCase(unittest.TestCase):
    """The live path, with ssh stubbed: inject first, then read the mail server."""

    def _runner(self, mail_text: str, notify_error: str = "", seen: list | None = None):
        def runner(target, command):
            if seen is not None:
                seen.append((target, command))
            if "amtool alert add" in command:
                return ""
            if "Notify attempt failed" in command:
                return notify_error
            return mail_text

        return runner

    def test_a_delivery_in_the_window_is_reported_delivered(self):
        delivered, evidence, waited = probe.run_probe(
            am_host="i3", am_target="root@192.168.1.53",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=5, runner=self._runner(DELIVERED),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertTrue(delivered)
        self.assertEqual(evidence, "")
        self.assertGreaterEqual(waited, 0.0)

    def test_no_delivery_in_the_window_reads_the_receivers_own_error(self):
        delivered, evidence, _ = probe.run_probe(
            am_host="i3", am_target="root@192.168.1.53",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._runner(OLD_DELIVERY, notify_error="Notify attempt failed: refused"),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertFalse(delivered)
        self.assertIn("refused", evidence)

    def test_the_probe_is_injected_before_the_mail_is_read(self):
        seen: list = []
        probe.run_probe(
            am_host="i3", am_target="root@192.168.1.53",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._runner(DELIVERED, seen=seen),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertIn("amtool alert add", seen[0][1])
        # The second read is the mail server, and it is the mail server that is asked.
        self.assertIn("stalwart", seen[1][1])
        self.assertEqual(seen[1][0], "root@192.168.1.51")

    def test_an_unreadable_host_is_never_a_silent_receiver(self):
        from estate_check import HostUnreadable

        def runner(target, command):
            raise OSError("ssh: connect to host port 22: No route to host")

        with self.assertRaises(HostUnreadable):
            probe.run_probe(
                am_host="i3", am_target="root@192.168.1.53",
                mail_host="i1", mail_target="root@192.168.1.51",
                wait=0, runner=runner, clock=lambda: 1_760_000_000.0,
                sleeper=lambda _s: None, nonce="x",
            )


class MainCase(unittest.TestCase):
    """The exit code and the textfile — what systemd and the alerts actually read."""

    def _run(self, delivered, evidence="", waited=1.0):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory) / "alert-delivery.prom"
            with mock.patch.object(probe, "run_probe", return_value=(delivered, evidence, waited)):
                code = probe.main(["--prom", str(prom)])
            body = prom.read_text(encoding="utf-8") if prom.exists() else ""
        return code, body

    def test_a_delivered_probe_exits_zero_and_publishes_status_one(self):
        code, body = self._run(True)
        self.assertEqual(code, 0)
        self.assertIn('innotel_estate_check_last_status{check="alert_delivery"} 1', body)
        self.assertIn('innotel_estate_check_alert_delivered{check="alert_delivery"} 1', body)

    def test_a_silent_receiver_exits_one_and_publishes_status_zero(self):
        code, body = self._run(False, "Notify attempt failed")
        self.assertEqual(code, 1)
        self.assertIn('innotel_estate_check_last_status{check="alert_delivery"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="alert_delivery"} 1', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
