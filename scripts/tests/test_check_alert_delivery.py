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
DELIVERED = '\x1b[37m2026-10-03T02:00:05Z\x1b[0m INFO Delivery completed (delivery.completed) queueId = q1, from = "alertmanager@innotel.us", to = ["alertmanager@innotel.us"]\n'
OLD_DELIVERY = "2020-01-01T00:00:00Z INFO Delivery completed (delivery.completed) queueId = q0, from = \"alertmanager@innotel.us\", to = [\"alertmanager@innotel.us\"]\n"
#: The same line as Stalwart actually writes it, with its field names coloured.
TEXT_COLOURED = (
    "\x1b[37m2026-10-03T02:00:05Z\x1b[0m \x1b[32;1mINFO\x1b[0m "
    "\x1b[37;1mDelivery completed\x1b[0m (delivery.completed) "
    '\x1b[36mqueueId\x1b[0m = q1, \x1b[36mqueueName\x1b[0m = "local", '
    '\x1b[36mfrom\x1b[0m = "alertmanager@innotel.us", '
    '\x1b[36mto\x1b[0m = ["alertmanager@innotel.us"]\n'
)


#: A delivery to a mailbox on this server: Stalwart logs the ingest, never a delivery.
LOCAL_INGEST = (
    '2026-11-15T02:29:57Z INFO Message ingested (message-ingest.ham) '
    'queueId = q2, queueName = "local", from = "alertmanager@innotel.us", '
    'to = ["alertmanager@innotel.us"]'
)
LOCAL_INGEST_ELSEWHERE = (
    '2026-11-15T02:29:58Z INFO Message ingested (message-ingest.ham) '
    'queueId = q3, queueName = "local", from = "alertmanager@innotel.us", '
    'to = ["admin@innotel.us"]'
)

#: The queue record and the ingest name the same queueId, so the pair says when a message
#: was submitted. A late delivery from an *earlier* run was queued before this run began.
QUEUED_BEFORE_THIS_RUN = (
    '2025-10-09T08:40:00Z INFO Queued message for delivery (queue.message-queued) '
    'queueId = 111, from = "alertmanager@innotel.us", to = ["alertmanager@innotel.us"]\n'
)
INGEST_LATE_BUT_FRESH = (
    '2025-10-09T08:55:00Z INFO Message ingested (message-ingest.ham) '
    'queueId = 111, queueName = "local", from = "alertmanager@innotel.us", '
    'to = ["alertmanager@innotel.us"]\n'
)
QUEUED_THIS_RUN = QUEUED_BEFORE_THIS_RUN.replace("08:40:00", "08:54:00").replace("111", "222")
INGEST_THIS_RUN = INGEST_LATE_BUT_FRESH.replace("111", "222")

#: The second shape Stalwart writes for a message reaching a mailbox here, taken from the
#: run this check failed on (2026-11-19 21:01:09Z): the ingest happens inside the
#: submission transaction, so it names no sender, recipient or queueId.
INLINE_SUBMISSION = (
    '2026-11-19T21:01:09Z INFO Queued message submission for delivery '
    '(queue.authenticated-message-queued) listenerId = "submission", localPort = 587, '
    'remoteIp = 192.168.1.44, remotePort = 55128, queueId = 341504572684272128, '
    'from = "alertmanager@innotel.us", to = ["alertmanager@innotel.us"], size = 9902\n'
)
INLINE_INGEST = (
    '2026-11-19T21:01:09Z INFO Message ingested (message-ingest.ham) accountId = 2, '
    'documentId = 2216, mailboxId = [0], blobId = "2ea29ba9", changeId = 2221, '
    'messageId = "1795122069952768866.1234@41594547654d", size = 10757, elapsed = 0ms\n'
)
#: The probe that run submitted, as this clock sees it (one second later).
INLINE_START = datetime(2026, 11, 19, 21, 1, 10, tzinfo=timezone.utc).timestamp()


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

    def test_the_probe_goes_to_the_alerts_account_not_the_human_inbox(self):
        self.assertNotEqual(probe.MAIL_TO, "admin@innotel.us")

    def test_the_log_filter_is_applied_after_the_colour_escapes_are_gone(self):
        # Stalwart colours its field names (`\x1b[36mfrom\x1b[0m = "…"`), so a grep for
        # `from = "` on the raw line matches nothing; the field filter has to run on the
        # stripped text (measured 2026-10-24, with a probe already delivered).
        coloured = TEXT_COLOURED
        self.assertEqual(probe.delivery_times(coloured), probe.delivery_times(DELIVERED))
        self.assertEqual(len(probe.delivery_times(coloured, from_address=probe.MAIL_FROM,
                                                  to_address=probe.MAIL_TO)), 1)

    def test_a_delivery_from_the_alerts_account_to_someone_else_does_not_count(self):
        other = DELIVERED.replace("to = [\"alertmanager@innotel.us\"]", "to = [\"admin@innotel.us\"]")
        self.assertEqual(
            probe.delivery_times(other, from_address=probe.MAIL_FROM, to_address=probe.MAIL_TO),
            [],
        )


class ParseCase(unittest.TestCase):
    def test_a_stamp_survives_the_ansi_colour_escape(self):
        self.assertTrue(probe.delivery_times(DELIVERED))

    def test_an_unparseable_line_is_skipped(self):
        self.assertEqual(probe.delivery_times("not a log line at all\n"), [])

    def test_stamps_come_back_newest_last(self):
        times = probe.delivery_times(OLD_DELIVERY + DELIVERED)
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), 2)


    def test_a_local_ingest_to_the_alerts_account_counts_as_delivery(self):
        times = probe.delivery_times(
            LOCAL_INGEST, from_address="alertmanager@innotel.us",
            to_address="alertmanager@innotel.us")
        self.assertEqual(len(times), 1)

    def test_a_local_ingest_to_someone_else_does_not_count(self):
        times = probe.delivery_times(
            LOCAL_INGEST_ELSEWHERE, from_address="alertmanager@innotel.us",
            to_address="alertmanager@innotel.us")
        self.assertEqual(times, [])

    def test_the_log_read_asks_for_local_ingests_too(self):
        command = probe.mail_log_command()
        self.assertIn("message-ingest", command)
        self.assertIn("dsn-success", command)

    def test_the_log_read_asks_for_submission_records_too(self):
        # An ingest logged without a sender is attributed through the submission written
        # beside it, so the queue records have to be in the read at all.
        command = probe.mail_log_command()
        # The pattern is a grep alternation, so it reaches the shell escaped.
        self.assertIn(r"queue\.(message-queued|authenticated-message-queued)", command)

    def test_an_ingest_logged_without_a_sender_is_found(self):
        self.assertEqual(len(probe.inline_ingest_times(INLINE_INGEST)), 1)

    def test_a_structured_ingest_is_not_read_as_an_inline_one(self):
        # The sender/recipient shape is already attributed by address; counting it twice
        # would let a delivery to someone else ride in as this probe's.
        self.assertEqual(probe.inline_ingest_times(LOCAL_INGEST), [])


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

    def _runner(
        self,
        mail_text: str,
        notify_error: str = "",
        seen: list | None = None,
        before_text: str = "",
    ):
        """Stub ssh: the first mail read is the baseline, later ones are the probe's window."""
        reads = {"mail": 0}

        def runner(target, command):
            if seen is not None:
                seen.append((target, command))
            if "amtool alert add" in command:
                return ""
            if "alert query" in command:
                return "1\n"
            if "Notify attempt failed" in command:
                return notify_error
            reads["mail"] += 1
            return before_text if reads["mail"] == 1 else mail_text

        return runner

    def test_a_delivery_in_the_window_is_reported_delivered(self):
        delivered, evidence, waited = probe.run_probe(
            am_host="i2", am_target="root@192.168.1.52",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=5, runner=self._runner(DELIVERED, before_text=OLD_DELIVERY),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertTrue(delivered)
        self.assertEqual(evidence, "")
        self.assertGreaterEqual(waited, 0.0)

    def test_no_delivery_in_the_window_reads_the_receivers_own_error(self):
        delivered, evidence, _ = probe.run_probe(
            am_host="i2", am_target="root@192.168.1.52",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._runner(OLD_DELIVERY, notify_error="Notify attempt failed: refused"),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertFalse(delivered)
        self.assertIn("refused", evidence)

    def test_the_baseline_is_read_before_the_probe_exists(self):
        # Sample the log before injecting, or the probe's own delivery becomes the
        # baseline and nothing can ever be newer than it.
        seen: list = []
        probe.run_probe(
            am_host="i2", am_target="root@192.168.1.52",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._runner(DELIVERED, seen=seen, before_text=OLD_DELIVERY),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertIn("stalwart", seen[0][1])
        self.assertEqual(seen[0][0], "root@192.168.1.51")
        self.assertIn("amtool alert add", seen[1][1])

    def test_a_delivery_already_in_the_log_does_not_prove_this_probe(self):
        # Measured 2026-11-19: most runs finished in ~1.3 s, far inside Alertmanager's
        # 30 s group_wait — they were claiming a delivery that predated the probe.
        delivered, _, _ = probe.run_probe(
            am_host="i2", am_target="root@192.168.1.52",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._runner(DELIVERED, before_text=DELIVERED),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertFalse(delivered)

    def test_the_window_outlasts_the_receivers_measured_retry_storm(self):
        # Every failing run measured 2026-11-19 stopped at 181.8–182.1 s: the window.
        self.assertGreaterEqual(probe.DEFAULT_WAIT_SECONDS, 300)

    def test_a_delivery_queued_before_this_run_is_not_this_run(self):
        # Measured 2026-11-19: a failing run began at 16:16:24 and the mail server logged
        # its probe ingested at 16:20:26, after the window had already closed. A message
        # queued by an *earlier* run lands the same way, and freshness alone would take it.
        delivered, _, _ = probe.run_probe(
            am_host="i2", am_target="root@192.168.1.52",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0,
            runner=self._runner(
                QUEUED_BEFORE_THIS_RUN + INGEST_LATE_BUT_FRESH, before_text=OLD_DELIVERY
            ),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertFalse(delivered)

    def test_a_delivery_queued_during_this_run_is_this_run(self):
        delivered, _, _ = probe.run_probe(
            am_host="i2", am_target="root@192.168.1.52",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0,
            runner=self._runner(QUEUED_THIS_RUN + INGEST_THIS_RUN, before_text=OLD_DELIVERY),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertTrue(delivered)

    def test_a_delivery_with_no_queue_line_falls_back_to_freshness(self):
        # A long tail read can lose the queue record; refusing every delivery without one
        # would turn a delivering receiver into a silent one.
        delivered, _, _ = probe.run_probe(
            am_host="i2", am_target="root@192.168.1.52",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._runner(INGEST_LATE_BUT_FRESH, before_text=OLD_DELIVERY),
            clock=lambda: 1_760_000_000.0, sleeper=lambda _s: None, nonce="x",
        )
        self.assertTrue(delivered)

    def test_an_alert_that_never_registers_is_the_injections_failure(self):
        # Measured 2026-11-18: `amtool alert add` returned without registering the alert,
        # and the check called that a silent receiver. Those are different defects.
        def runner(target, command):
            if "amtool alert add" in command:
                return ""
            if "alert query" in command:
                return "0\n"
            if "Notify attempt failed" in command:
                return ""
            return ""

        delivered, evidence, _ = probe.run_probe(
            am_host="i3", am_target="root@192.168.1.53",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=runner, clock=lambda: 1_760_000_000.0,
            sleeper=lambda _s: None, nonce="x",
        )
        self.assertFalse(delivered)
        self.assertIn("never appeared in Alertmanager", evidence)
        self.assertIn("injection failed", evidence)

    #: The inline shape, measured 2026-11-19 on the run this check failed on: the message
    #: was in the mailbox at the same second as its submission, and the check called the
    #: receiver silent because the ingest named neither address nor queueId.
    def _inline_runner(self, mail_text: str):
        return self._runner(mail_text, before_text=OLD_DELIVERY)

    def test_an_ingest_logged_without_a_sender_beside_its_submission_is_delivered(self):
        delivered, evidence, _ = probe.run_probe(
            am_host="i3", am_target="root@192.168.1.53",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._inline_runner(INLINE_SUBMISSION + INLINE_INGEST),
            clock=lambda: INLINE_START, sleeper=lambda _s: None, nonce="x",
        )
        self.assertTrue(delivered, evidence)

    def test_an_ingest_logged_without_a_sender_and_no_submission_does_not_count(self):
        delivered, _, _ = probe.run_probe(
            am_host="i3", am_target="root@192.168.1.53",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._inline_runner(INLINE_INGEST),
            clock=lambda: INLINE_START, sleeper=lambda _s: None, nonce="x",
        )
        self.assertFalse(delivered)

    def test_an_ingest_logged_without_a_sender_must_sit_beside_that_submission(self):
        # A different message arriving half a minute into the window must not be read as
        # this probe's: the inline ingest is immediate by construction.
        late = INLINE_INGEST.replace("21:01:09", "21:01:39")
        delivered, _, _ = probe.run_probe(
            am_host="i3", am_target="root@192.168.1.53",
            mail_host="i1", mail_target="root@192.168.1.51",
            wait=0, runner=self._inline_runner(INLINE_SUBMISSION + late),
            clock=lambda: INLINE_START, sleeper=lambda _s: None, nonce="x",
        )
        self.assertFalse(delivered)

    def test_an_unreadable_host_is_never_a_silent_receiver(self):
        from estate_check import HostUnreadable

        def runner(target, command):
            raise OSError("ssh: connect to host port 22: No route to host")

        with self.assertRaises(HostUnreadable):
            probe.run_probe(
                am_host="i2", am_target="root@192.168.1.52",
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
