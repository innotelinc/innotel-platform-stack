#!/usr/bin/env python3
"""Unit tests for check-mail-identities.py — the verdict of the mailbox mirror.

The check reads four of systemd's own facts and turns them into one reading, so the cases
here are the states the mirror can be in: a run that succeeded, a run that did not, a timer
that is not firing, and a host that could not be read.

2026-11-16 is the case that made the failure line change. The last run exited **1** — which
the check described as "`--apply` was refused for at least one user, so that mailbox still
has no identity it can sign in with" — while the journal's last line was
`TimeoutError: The read operation timed out` and every mailbox already had its identity.
Exit 1 is what the script exits with when it crashes as well as when it refuses a user, so
the finding now quotes the line the container logged instead of asserting one cause. These
tests pin both halves: the observed line is reported, and the unobserved cause is not.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(SCRIPTS))
from estate_check import prometheus_text  # noqa: E402

CHECK = SCRIPTS / "check-mail-identities.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_mail_identities", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


chk = _load()

NOW = 1_794_888_327
#: A clean report as the container prints it: `timer_active:…` lines, one per fact.
CLEAN = (
    "timer_active:active\n"
    "timer_enabled:enabled\n"
    "result:success\n"
    "status:0\n"
    f"last:{NOW - 114}\n"
    "message:every mailbox already has an identity keyed by its address\n"
    f"now:{NOW}\n"
)
#: The 2026-11-16 failure, verbatim in shape: exit 1 and a logged traceback line.
FAILED = (
    "timer_active:active\n"
    "timer_enabled:enabled\n"
    "result:exit-code\n"
    "status:1\n"
    f"last:{NOW - 628}\n"
    "message:TimeoutError: The read operation timed out\n"
    f"now:{NOW}\n"
)


def codes(result) -> list[str]:
    return [finding.code for finding in result.findings]


class Parse(unittest.TestCase):
    def test_a_clean_report_is_read_as_facts(self):
        facts = chk.parse(CLEAN)
        self.assertEqual(facts.timer_active, "active")
        self.assertEqual(facts.timer_enabled, "enabled")
        self.assertEqual(facts.result, "success")
        self.assertEqual(facts.status, 0)
        self.assertEqual(facts.last, NOW - 114)
        self.assertEqual(facts.now, NOW)
        self.assertEqual(facts.age_seconds, 114)
        self.assertEqual(facts.last_message, "every mailbox already has an identity keyed by its address")

    def test_a_message_with_colons_survives_the_split(self):
        # `TimeoutError: …` has the same `key:value` shape as the report's own lines, so the
        # parse has to split on the first colon only.
        facts = chk.parse(FAILED)
        self.assertEqual(facts.last_message, "TimeoutError: The read operation timed out")

    def test_a_time_that_could_not_be_read_is_its_own_fact(self):
        facts = chk.parse("last:unreadable\nnow:100\n")
        self.assertTrue(facts.last_unreadable)
        self.assertIsNone(facts.last)
        self.assertIsNone(facts.age_seconds)

    def test_a_missing_message_is_empty_rather_than_unknown_text(self):
        self.assertEqual(chk.parse("result:success\n").last_message, "")

    def test_the_mailboxes_the_run_named_are_read_from_its_log(self):
        # `FAILED` carries its own colon, so the lines cross ssh joined by ESC rather than
        # by newlines — the parse has to split on that, not on the first colon.
        report = FAILED.replace(
            "message:TimeoutError: The read operation timed out\n",
            "message:  FAILED b@example.com: 400 {\"username\": [\"already exists\"]}\n"
            "unmirrored:  MISSING a@example.com\x1f  FAILED b@example.com: 400 rejected\x1f"
            "  CREATED c@example.com (pk=3)\x1f  MISSING a@example.com\n",
        )
        facts = chk.parse(report)
        self.assertEqual(facts.planned, ["a@example.com"])
        self.assertEqual(facts.refused, ["b@example.com"])
        # Refused first: those are the ones still without an identity *after* --apply ran.
        self.assertEqual(facts.unmirrored, ["b@example.com", "a@example.com"])

    def test_a_created_address_is_not_a_gap(self):
        facts = chk.parse(CLEAN + "unmirrored:  CREATED c@example.com (pk=3)\n")
        self.assertEqual(facts.unmirrored, [])


class Verdict(unittest.TestCase):
    def test_a_clean_fresh_run_is_a_note_and_not_a_failure(self):
        result = chk.audit(chk.parse(CLEAN))
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual([(f.code, f.level) for f in result.findings], [("in_sync", "note")])
        self.assertIn("114 s ago", result.findings[0].message)

    def test_a_failed_run_is_a_failure_that_quotes_what_was_logged(self):
        # The observed cause, not the one the exit code cannot rule out.
        result = chk.audit(chk.parse(FAILED))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["last_run_failed"])
        message = result.findings[0].message
        self.assertIn("(1)", message)
        self.assertIn("TimeoutError", message)

    def test_a_failed_run_names_the_mailbox_it_could_not_mirror(self):
        # The sentence a person needs: which address, not how many runs went wrong.
        report = FAILED.replace(
            "message:TimeoutError: The read operation timed out\n",
            "message:  FAILED b@example.com: 400 rejected\n"
            "unmirrored:  FAILED b@example.com: 400 rejected\n",
        )
        result = chk.audit(chk.parse(report))
        self.assertEqual(codes(result), ["last_run_failed"])
        self.assertIn("b@example.com", result.findings[0].message)

    def test_a_crash_that_named_no_mailbox_says_nothing_about_one(self):
        # 2026-11-16 itself: a timeout named no address, and the check does not invent one.
        result = chk.audit(chk.parse(FAILED))
        self.assertIn("TimeoutError", result.findings[0].message)
        self.assertNotIn("mailboxes with no identity", result.findings[0].message)

    def test_a_refusal_is_named_when_the_log_says_nothing(self):
        # Without a logged line the check still fails, and names the documented reason — it
        # just says the logged line would tell them apart instead of asserting it happened.
        report = FAILED.replace("message:TimeoutError: The read operation timed out\n", "")
        result = chk.audit(chk.parse(report))
        self.assertEqual(codes(result), ["last_run_failed"])
        message = result.findings[0].message
        self.assertIn("`--apply` was refused", message)
        self.assertIn("says which", message)
        self.assertNotIn("the last line it logged was", message)

    def test_an_unreadable_api_is_named_by_its_own_exit_code(self):
        report = FAILED.replace("status:1", "status:2")
        result = chk.audit(chk.parse(report))
        self.assertIn("Stalwart admin or the Authentik token", result.findings[0].message)

    def test_a_timer_that_is_not_firing_is_a_failure(self):
        report = CLEAN.replace("timer_active:active", "timer_active:inactive").replace(
            "timer_enabled:enabled", "timer_enabled:disabled")
        result = chk.audit(chk.parse(report))
        self.assertFalse(result.ok)
        # The run itself was clean and fresh, so the age note is still there and still
        # right: what is broken is that nothing will schedule the next one.
        self.assertEqual(sorted(codes(result)), ["in_sync", "timer_disabled", "timer_inactive"])

    def test_a_run_past_two_intervals_is_not_firing(self):
        report = CLEAN.replace(f"last:{NOW - 114}", f"last:{NOW - chk.STALE_SECONDS - 1}")
        result = chk.audit(chk.parse(report))
        self.assertEqual(codes(result), ["not_firing"])

    def test_a_run_inside_two_intervals_is_still_fresh(self):
        report = CLEAN.replace(f"last:{NOW - 114}", f"last:{NOW - chk.STALE_SECONDS}")
        self.assertTrue(chk.audit(chk.parse(report)).ok)

    def test_a_unit_that_never_ran_is_a_failure(self):
        report = CLEAN.replace(f"last:{NOW - 114}", "last:never")
        result = chk.audit(chk.parse(report))
        self.assertEqual(codes(result), ["never_ran"])

    def test_a_finish_time_that_cannot_be_read_is_a_failure(self):
        report = CLEAN.replace(f"last:{NOW - 114}", "last:unreadable")
        result = chk.audit(chk.parse(report))
        self.assertEqual(codes(result), ["last_run_unreadable"])


class Signal(unittest.TestCase):
    """The series the alerts page on: one reading, true now, however many runs it took."""

    def test_a_good_run_publishes_status_one_and_the_age(self):
        text = prometheus_text(chk.CHECK, chk.audit(chk.parse(CLEAN)), ran_ok=True, now=float(NOW),
                               extra=chk.metric_age(chk.parse(CLEAN)))
        self.assertIn('innotel_estate_check_last_status{check="mail_identities"} 1', text)
        self.assertIn('innotel_estate_check_mail_identities_age_seconds{check="mail_identities"} 114', text)

    def test_a_failed_run_publishes_status_zero_and_its_age(self):
        facts = chk.parse(FAILED)
        text = prometheus_text(chk.CHECK, chk.audit(facts), ran_ok=True, now=float(NOW),
                               extra=chk.metric_age(facts))
        self.assertIn('innotel_estate_check_last_status{check="mail_identities"} 0', text)
        self.assertIn('innotel_estate_check_failures{check="mail_identities"} 1', text)
        self.assertIn('innotel_estate_check_mail_identities_age_seconds{check="mail_identities"} 628', text)

    def test_an_unreadable_run_publishes_no_age(self):
        # A run that could not happen has no age to report; inventing one would hide it.
        self.assertIsNone(chk.metric_age(chk.parse("now:100\n")))

    def test_a_failed_run_publishes_one_series_per_mailbox(self):
        facts = chk.parse(
            FAILED + "unmirrored:  FAILED b@example.com: 400 rejected\x1f  MISSING a@example.com\n")
        text = chk.metrics(facts)
        self.assertIn(
            'innotel_estate_check_mail_identities_unmirrored{check="mail_identities",'
            'mailbox="b@example.com"} 1', text)
        self.assertIn('mailbox="a@example.com"', text)
        self.assertIn('innotel_estate_check_mail_identities_age_seconds', text)

    def test_a_clean_run_publishes_no_unmirrored_series(self):
        # A clean `--apply` run prints what it created; those addresses are not gaps, and a
        # series left over from an earlier failure must not survive a green run.
        facts = chk.parse(CLEAN + "unmirrored:  CREATED c@example.com (pk=3)\x1f  MISSING d@example.com\n")
        self.assertNotIn("unmirrored", chk.metrics(facts) or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
