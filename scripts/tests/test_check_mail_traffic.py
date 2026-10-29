#!/usr/bin/env python3
"""Unit tests for check-mail-traffic.py — the numbers the mail dashboard is made of.

What is worth locking here is not "a line was counted". It is the three things that
made this check hard to get right against the real server:

- **The log is coloured.** Stalwart writes ANSI escapes because it is meant to be
  read in a terminal, so every line is `\\x1b[37m2026-10-29T18:32:46Z\\x1b[0m …`.
  A parser that anchors on the timestamp matched **zero** of a 2 826-line sample
  until the escapes were stripped, and the dashboard would have shown a busy mail
  server as a dead one.
- **A quiet estate and a broken queue must not read the same.** Attempts with no
  delivery is a failure; no attempts at all is a warning, because the alert on a
  quiet Sunday would be noise and the alert on a stuck queue would be the point.
- **The textfile must stay valid.** The shared contract (`last_status` and friends)
  is what every rule keys on, and the check's own series are appended to it; a
  family written with no samples is invalid text and would take the whole file —
  including the rules — down with it.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-mail-traffic.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_mail_traffic", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()

#: One delivered message, as the server writes it: coloured, RFC 3339 UTC, and with
#: the fields after the event name.
DELIVERED = (
    "\x1b[37m2026-10-29T18:32:46Z\x1b[0m \x1b[32;1mINFO\x1b[0m "
    "\x1b[37;1mDelivery completed\x1b[0m (delivery.completed) "
    "queueId = 1, \x1b[36mqueueName\x1b[0m = \"local\", "
    "\x1b[36mfrom\x1b[0m = \"alertmanager@innotel.us\", "
    "\x1b[36mto\x1b[0m = [\"admin@innotel.us\"], \x1b[36msize\x1b[0m = 3002"
)
QUEUED = (
    "2026-10-29T18:32:46Z INFO Queued message for delivery (queue.message-queued) "
    "listenerId = \"smtp\", queueId = 2, from = \"bounce@mailjet.com\", "
    "to = [\"admin@innotel.us\"], size = 8108"
)
INGEST_HAM = (
    "2026-10-29T18:32:46Z INFO Message ingested (message-ingest.ham) queueId = 1, "
    "from = \"alertmanager@innotel.us\", to = [\"admin@innotel.us\"], size = 3002"
)
INGEST_SPAM = INGEST_HAM.replace("message-ingest.ham", "message-ingest.spam")
AUTH_OK = ("2026-10-29T18:32:46Z INFO Authentication successful (auth.success) "
           "listenerId = \"submission\", localPort = 587, accountName = \"alertmanager\"")
AUTH_BAD = ("2026-10-29T18:32:46Z ERROR Authentication error (auth.error) "
            "listenerId = \"imaps\", reason = \"Bad credentials\"")
BLOCKED = ("2026-10-29T07:33:46Z INFO Blocked IP address (security.ip-blocked) "
           "listenerId = \"pop3s\", remoteIp = 46.105.162.95")
MISSING = ("2026-10-29T07:33:46Z INFO Mailbox does not exist (smtp.mailbox-does-not-exist) "
           "from = \"x@y.z\", to = \"nobody@innotel.us\"")
BOUNCE = ("2026-10-29T07:33:46Z WARN Delivery failed (delivery.dsn-perm-fail) "
          "queueName = \"remote\", from = \"a@innotel.us\", to = [\"bad@example.com\"]")
NOISE = ("2026-10-29T18:30:05Z INFO Metrics collected (telemetry.metrics-collected) "
         "elapsed = 7ms")


def _runner(lines: list[str], hosts: str = "distro,RUNNING\nmail,RUNNING\n"):
    """A stand-in for the estate's ssh: host discovery, then the log.

    `estate_check.read_host` calls the runner as `runner(target, command)`, so the
    command is what tells the two reads apart.
    """

    def run(target, command):
        return hosts if "incus list" in command else "\n".join(lines)

    return run


def _audit(lines, now=1793300000.0, window=24, hosts="distro,RUNNING\nmail,RUNNING\n"):
    return probe.audit(now, window, _runner(lines, hosts))


class ParserCase(unittest.TestCase):
    def test_coloured_lines_are_counted(self):
        # The regression that mattered: an ANSI-coloured line parsed as nothing, so a
        # busy server read as a silent one.
        out = probe.measure([DELIVERED, DELIVERED])
        self.assertEqual(out.lines, 2)
        self.assertEqual(out.delivered["local"], 2)

    def test_a_line_it_does_not_recognise_is_skipped_not_fatal(self):
        # The window holds more than traffic (scheduled tasks, collected metrics), so
        # an unknown event must count as a line and contribute to nothing else — and
        # a line that is not a log record at all must not raise.
        out = probe.measure([NOISE, "garbage", DELIVERED])
        self.assertEqual(out.lines, 2)
        self.assertEqual(dict(out.delivered), {"local": 1})
        self.assertEqual(sum(out.queued.values()), 0)

    def test_queue_entry_is_read_as_submission_or_inbound(self):
        out = probe.measure([QUEUED])
        self.assertEqual(dict(out.queued), {"inbound": 1})

    def test_ingest_separates_spam_from_ham(self):
        out = probe.measure([INGEST_HAM, INGEST_SPAM, INGEST_SPAM])
        self.assertEqual(dict(out.ingested), {"ham": 1, "spam": 2})

    def test_auth_is_read_per_listener_and_result(self):
        out = probe.measure([AUTH_OK, AUTH_BAD, AUTH_BAD])
        self.assertEqual(out.auth[("submission", "success")], 1)
        self.assertEqual(out.auth[("imaps", "failure")], 2)
        self.assertEqual(out.auth_failures, 2)

    def test_sender_and_recipient_domains_come_from_the_addresses(self):
        out = probe.measure([INGEST_HAM, QUEUED])
        self.assertEqual(out.senders["innotel.us"], 1)
        self.assertEqual(out.senders["mailjet.com"], 1)
        self.assertEqual(out.recipients["innotel.us"], 2)

    def test_the_refusals_are_counted_apart(self):
        out = probe.measure([BLOCKED, MISSING])
        self.assertEqual(out.blocked_ips, 1)
        self.assertEqual(out.mailbox_missing, 1)

    def test_the_last_delivery_is_the_newest_one(self):
        older = DELIVERED.replace("18:32:46Z", "09:00:00Z")
        out = probe.measure([older, DELIVERED])
        self.assertEqual(out.last_delivery, probe._epoch("2026-10-29T18:32:46Z"))


class WindowCase(unittest.TestCase):
    def test_lines_before_the_cutoff_are_dropped_on_the_way_in(self):
        # The filter is string comparison on RFC 3339 UTC, which is why the window can
        # be applied without parsing every line.
        cutoff = probe.cutoff_for(1793300000.0, 24)
        self.assertRegex(cutoff, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        kept = [line for line in [DELIVERED, DELIVERED.replace("2026-10-29", "2026-09-01")]
                if probe.ANSI.sub("", line)[:20] >= cutoff]
        self.assertEqual(len(kept), 1)


class RuleCase(unittest.TestCase):
    def test_a_delivering_window_is_clean(self):
        result, out = _audit([DELIVERED])
        self.assertTrue(result.ok)
        self.assertEqual(out.delivered["local"], 1)

    def test_attempts_with_nothing_delivered_is_a_failure(self):
        # Accepted and never delivered: the quiet outage this check exists for.
        result, _ = _audit([QUEUED])
        self.assertFalse(result.ok)
        self.assertIn("nothing-delivered", [f.code for f in result.failures])

    def test_no_traffic_at_all_is_a_warning_not_a_failure(self):
        # A quiet estate must not page; that is what makes the failure above readable.
        result, _ = _audit([AUTH_OK])
        self.assertTrue(result.ok)
        self.assertIn("no-traffic", [f.code for f in result.warnings])

    def test_a_bounce_is_a_warning_that_does_not_fail_the_run(self):
        result, _ = _audit([DELIVERED, BOUNCE])
        self.assertTrue(result.ok)
        self.assertIn("bounced", [f.code for f in result.warnings])

    def test_it_says_what_it_saw_in_a_note(self):
        result, _ = _audit([DELIVERED, INGEST_HAM])
        note = next(f for f in result.notes if f.code == "window")
        self.assertIn("1 delivered", note.message)
        self.assertIn("24h", note.message)

    def test_no_host_running_the_mail_container_is_exit_2(self):
        # A container that is somewhere else, or nowhere, is never a quiet pass.
        with self.assertRaises(probe.HostUnreadable):
            probe.audit(1793300000.0, 24, _runner([DELIVERED], hosts="distro,RUNNING\n"))


class TextfileCase(unittest.TestCase):
    def test_the_series_carry_the_shared_contract_too(self):
        result, out = _audit([DELIVERED, AUTH_BAD, BLOCKED])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.prom"
            probe.write_prom(str(path), probe.CHECK, result, ran_ok=True,
                             now=1793300000.0, extra=probe.series(out))
            text = path.read_text(encoding="utf-8")
        self.assertIn('innotel_estate_check_last_status{check="mail_traffic"} 1', text)
        self.assertIn('innotel_estate_check_mail_delivered{queue="local"} 1', text)
        self.assertIn('innotel_estate_check_mail_auth{listener="imaps",result="failure"} 1', text)
        self.assertIn('innotel_estate_check_mail_blocked_ips 1', text)

    def test_a_family_with_no_samples_is_omitted(self):
        # An empty family written bare is invalid text and would take `last_status`
        # down with it, so a quiet dimension must not appear at all.
        text = probe.series(probe.Traffic())
        self.assertNotIn("innotel_estate_check_mail_delivered{", text)
        self.assertIn("innotel_estate_check_mail_lines 0", text)

    def test_the_tail_of_the_domain_ranking_is_summed_into_other(self):
        out = probe.Traffic()
        for i in range(probe.TOP_DOMAINS + 5):
            out.senders[f"d{i}.example"] = 1
        text = probe.series(out)
        self.assertIn('domain="other"} 5', text)

    def test_a_run_that_could_not_read_writes_zero_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.prom"
            probe.write_prom(str(path), probe.CHECK, None, ran_ok=False)
            text = path.read_text(encoding="utf-8")
        self.assertIn('innotel_estate_check_last_status{check="mail_traffic"} 0', text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
