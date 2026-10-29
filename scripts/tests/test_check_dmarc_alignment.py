#!/usr/bin/env python3
"""Unit tests for check-dmarc-alignment.py — the streams `p=reject` would drop.

The failure this check exists to answer is a question, not an error: "can
`_dmarc.atheniq.courses` be tightened to `p=reject`?" `p=reject` fails **silently**
— an unaligned message is dropped by the receiver, so the symptom is a reset link
that never arrives. The aggregate reports are the only evidence, which makes three
things worth locking here:

* `policy_evaluated` is already the *aligned* verdict, so a stream failing both
  SPF and DKIM is exactly the mail a tightened policy loses — and it is a
  **failure**, not a warning, or the answer to the question is flattering.
* A stream aligning by only one of the two is a warning: mail survives, but a
  failing SPF is the shape of the missing include that `docs/Deployment.md`
  §9.11 already had to fix once.
* No reports at all is exit 2, never a quiet pass — "could not answer" is the
  state that let the decision go unmade for as long as it did.

The mailbox is never contacted: `--reports-dir` and the pure `fold`/`evaluate`
split are what the tests drive.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-dmarc-alignment.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_dmarc_alignment", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dmarc = _load()


def report(
    org: str = "google.com",
    domain: str = "atheniq.courses",
    policy: str = "quarantine",
    records=(("73.68.203.71", 5, "none", "pass", "pass", "atheniq.courses"),),
) -> bytes:
    """One aggregate report body. `records` is `(ip, count, disposition, dkim, spf, from)`."""
    rows = "".join(
        f"""
  <record>
    <row>
      <source_ip>{ip}</source_ip>
      <count>{count}</count>
      <policy_evaluated>
        <disposition>{disposition}</disposition>
        <dkim>{dkim}</dkim>
        <spf>{spf}</spf>
      </policy_evaluated>
    </row>
    <identifiers>
      <header_from>{header_from}</header_from>
    </identifiers>
  </record>"""
        for ip, count, disposition, dkim, spf, header_from in records
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feedback>
  <report_metadata>
    <org_name>{org}</org_name>
    <email>noreply-dmarc-support@google.com</email>
    <report_id>1</report_id>
    <date_range><begin>1761000000</begin><end>1761086399</end></date_range>
  </report_metadata>
  <policy_published>
    <domain>{domain}</domain>
    <p>{policy}</p>
    <pct>100</pct>
  </policy_published>{rows}
</feedback>
""".encode()


class ParseCase(unittest.TestCase):
    def test_a_report_reads_its_org_its_policy_and_its_records(self):
        policy, records = dmarc.parse_report(report())
        self.assertEqual(policy["org"], "google.com")
        self.assertEqual(policy["domain"], "atheniq.courses")
        self.assertEqual(policy["p"], "quarantine")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source_ip"], "73.68.203.71")
        self.assertEqual(records[0]["count"], "5")
        self.assertEqual(records[0]["dkim"], "pass")
        self.assertEqual(records[0]["header_from"], "atheniq.courses")

    def test_a_missing_auth_result_is_a_failure_not_a_pass(self):
        # The dangerous default: an absent <dkim> must never read as aligned, or the
        # check answers "yes, tighten it" on incomplete evidence.
        policy, records = dmarc.parse_report(report(records=(("198.51.100.9", 2, "none", "", "", "atheniq.courses"),)))
        self.assertEqual(records[0]["dkim"], "fail")
        self.assertEqual(records[0]["spf"], "fail")

    def test_something_that_is_xml_but_not_a_report_is_refused(self):
        # A `.xml` that is not <feedback> must not be counted as a clean report.
        with self.assertRaises(ValueError):
            dmarc.parse_report(b'<?xml version="1.0"?><note><to>someone</to></note>')

    def test_malformed_xml_raises_so_the_caller_can_skip_it(self):
        with self.assertRaises(Exception):
            dmarc.parse_report(b"<feedback><record>")


class ExpandCase(unittest.TestCase):
    def test_a_zipped_report_is_opened(self):
        body = report()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("google.com!atheniq.courses!1.xml", body)
        expanded = dmarc.expand("report.zip", buffer.getvalue())
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0][1], body)

    def test_a_gzipped_report_is_opened(self):
        import gzip

        body = report()
        expanded = dmarc.expand("report.xml.gz", gzip.compress(body))
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0][1], body)

    def test_raw_xml_passes_through(self):
        body = report()
        self.assertEqual(dmarc.expand("report.xml", body), [("report.xml", body)])

    def test_something_that_is_not_a_report_yields_nothing(self):
        self.assertEqual(dmarc.expand("logo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64), [])


class FoldCase(unittest.TestCase):
    def test_one_stream_per_org_and_address(self):
        # Two receivers reporting the same address are two streams, not one: the org
        # is the axis a person reads when deciding whether a sender is real.
        google = report(org="google.com", records=(("73.68.203.71", 5, "none", "pass", "pass", "atheniq.courses"),))
        yahoo = report(org="yahoo.com", records=(("73.68.203.71", 3, "none", "pass", "pass", "atheniq.courses"),))
        streams, policies, skipped = dmarc.fold([("a.zip", google), ("b.zip", yahoo)], dmarc.DOMAINS)
        self.assertEqual(len(streams), 2)
        self.assertEqual({s.org for s in streams.values()}, {"google.com", "yahoo.com"})
        self.assertEqual(policies["atheniq.courses"]["p"], "quarantine")
        self.assertEqual(skipped.findings, [])

    def test_the_same_stream_across_reports_is_summed(self):
        first = report(records=(("73.68.203.71", 5, "none", "pass", "pass", "atheniq.courses"),))
        second = report(records=(("73.68.203.71", 7, "none", "pass", "pass", "atheniq.courses"),))
        streams, _, _ = dmarc.fold([("a", first), ("b", second)], dmarc.DOMAINS)
        self.assertEqual(len(streams), 1)
        self.assertEqual(next(iter(streams.values())).messages, 12)

    def test_another_domains_report_in_the_same_mailbox_is_not_ours(self):
        # The mailbox also collects reports for domains this check does not speak
        # about; they must not be reported as an unaligned stream of ours.
        theirs = report(domain="customer.example", records=(("203.0.113.5", 9, "none", "fail", "fail", "customer.example"),))
        ours = report(records=(("73.68.203.71", 5, "none", "pass", "pass", "atheniq.courses"),))
        streams, policies, _ = dmarc.fold([("a", theirs), ("b", ours)], dmarc.DOMAINS)
        self.assertEqual(set(policies), {"atheniq.courses"})
        self.assertEqual({s.header_from for s in streams.values()}, {"atheniq.courses"})

    def test_nothing_parsable_is_unreadable_rather_than_clean(self):
        with self.assertRaises(dmarc.ReportsUnreadable):
            dmarc.fold([("junk.zip", b"PK not really")], dmarc.DOMAINS)


class EvaluateCase(unittest.TestCase):
    def _streams(self, *specs):
        streams: dict[tuple[str, str, str], dmarc.Stream] = {}
        for ip, count, dkim, spf in specs:
            records = [(ip, count, "none", dkim, spf, "atheniq.courses")]
            published, parsed = dmarc.parse_report(report(records=records))
            for record in parsed:
                record["org"] = published["org"]
            dmarc.collect(streams, published, parsed)
        return list(streams.values())

    def test_a_stream_failing_both_is_a_failure(self):
        # This is the whole point: these are the messages p=reject drops.
        streams = self._streams(("203.0.113.9", 4, "fail", "fail"))
        result = dmarc.evaluate(streams, {"atheniq.courses": {"p": "quarantine"}}, threshold=1)
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["stream-misaligned"])
        self.assertIn("p=reject would drop these", result.failures[0].message)

    def test_a_stream_below_the_threshold_is_noted_not_failed(self):
        streams = self._streams(("203.0.113.9", 1, "fail", "fail"))
        result = dmarc.evaluate(streams, {"atheniq.courses": {"p": "quarantine"}}, threshold=5)
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.warnings], [])

    def test_aligning_by_one_half_is_a_warning(self):
        # dkim pass + spf fail: mail survives, and the failing half is the shape of
        # the missing SPF include §9.11 had to fix.
        streams = self._streams(("73.68.203.71", 6, "pass", "fail"))
        result = dmarc.evaluate(streams, {"atheniq.courses": {"p": "quarantine"}}, threshold=1)
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.warnings], ["stream-half-aligned"])
        self.assertIn("SPF", result.warnings[0].message)

    def test_a_fully_aligned_stream_passes(self):
        streams = self._streams(("73.68.203.71", 6, "pass", "pass"))
        result = dmarc.evaluate(streams, {"atheniq.courses": {"p": "quarantine"}}, threshold=1)
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.findings if f.level != "note"], [])

    def test_p_none_is_a_warning_because_nothing_is_enforced(self):
        streams = self._streams(("73.68.203.71", 6, "pass", "pass"))
        result = dmarc.evaluate(streams, {"atheniq.courses": {"p": "none"}}, threshold=1)
        self.assertEqual([f.code for f in result.warnings], ["policy-none"])

    def test_no_streams_at_all_is_a_failure(self):
        result = dmarc.evaluate([], {"atheniq.courses": {"p": "quarantine"}}, threshold=1)
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["no-streams"])


class MainCase(unittest.TestCase):
    """The exit codes the unit and the alerts read."""

    def _dir(self, *bodies):
        directory = Path(tempfile.mkdtemp())
        for index, body in enumerate(bodies):
            (directory / f"report-{index}.xml").write_bytes(body)
        return directory

    def test_an_unaligned_stream_exits_one(self):
        directory = self._dir(report(records=(("203.0.113.9", 4, "none", "fail", "fail", "atheniq.courses"),)))
        self.assertEqual(dmarc.main(["--reports-dir", str(directory)]), 1)

    def test_an_aligned_window_exits_zero(self):
        directory = self._dir(report())
        self.assertEqual(dmarc.main(["--reports-dir", str(directory)]), 0)

    def test_an_empty_window_exits_two_rather_than_passing(self):
        # No reports means the question cannot be answered, which is exactly the
        # silence this check exists to break.
        self.assertEqual(dmarc.main(["--reports-dir", str(Path(tempfile.mkdtemp()))]), 2)

    def test_no_credential_and_no_directory_exits_two(self):
        # The check must carry no mailbox password of its own.
        self.assertEqual(dmarc.main([]), 2)

    def test_old_reports_fall_outside_the_window(self):
        import os

        directory = self._dir(report(records=(("203.0.113.9", 4, "none", "fail", "fail", "atheniq.courses"),)))
        stale = datetime.combine(date.today() - timedelta(days=40), datetime.min.time()).timestamp()
        for entry in directory.iterdir():
            os.utime(entry, (stale, stale))
        self.assertEqual(dmarc.main(["--reports-dir", str(directory), "--days", "7"]), 2)

    # -- silence: waiting for the first report is not a broken reporting path ----------

    def _mailbox_run(self, wired: str, *extra, today_patch=None):
        """`main` against a mailbox that has no reports in it — no network.

        The decision this covers only exists on the mailbox path (a `--reports-dir` with
        nothing in it is a question about an archive, and stays exit 2).
        """
        import os

        directory = Path(tempfile.mkdtemp())
        textfile = directory / "out.prom"
        old_user, old_password = os.environ.get("DMARC_USER"), os.environ.get("DMARC_PASSWORD")
        os.environ["DMARC_USER"], os.environ["DMARC_PASSWORD"] = "check@example.invalid", "x"
        old_fetch = dmarc.fetch_from_mailbox
        dmarc.fetch_from_mailbox = lambda *a, **k: []
        try:
            code = dmarc.main(["--wired", wired, "--prom", str(textfile)] + list(extra))
        finally:
            dmarc.fetch_from_mailbox = old_fetch
            if old_user is None:
                os.environ.pop("DMARC_USER", None)
            else:
                os.environ["DMARC_USER"] = old_user
            if old_password is None:
                os.environ.pop("DMARC_PASSWORD", None)
            else:
                os.environ["DMARC_PASSWORD"] = old_password
        return code, textfile.read_text(encoding="utf-8")

    def test_a_fresh_rua_with_no_reports_yet_is_a_note_not_a_failure(self):
        # The state that made this check permanently red: a rua published today has
        # nothing to report on, and "could not answer" is not a finding about the estate.
        code, body = self._mailbox_run(date.today().isoformat(), "--grace-days", "3")
        self.assertEqual(code, 0)
        self.assertIn('innotel_estate_check_last_status{check="dmarc_alignment"} 1', body)
        self.assertIn('innotel_estate_check_failures{check="dmarc_alignment"} 0', body)
        self.assertIn('innotel_estate_check_notes{check="dmarc_alignment"} 1', body)
        self.assertIn("awaiting-first-report", body)
        # And the panel can tell "nothing reported on" from "nothing dropped".
        self.assertIn('innotel_estate_check_dmarc_reports{check="dmarc_alignment"} 0', body)

    def test_silence_past_the_grace_window_is_a_broken_reporting_path(self):
        code, body = self._mailbox_run((date.today() - timedelta(days=30)).isoformat())
        self.assertEqual(code, 1)
        self.assertIn('innotel_estate_check_last_status{check="dmarc_alignment"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="dmarc_alignment"} 1', body)
        # A failure is counted, not narrated: only notes carry their message into the
        # textfile. The wording is locked on the pure function below.
        self.assertIn('innotel_estate_check_notes{check="dmarc_alignment"} 0', body)
        self.assertNotIn("awaiting-first-report", body)

    def test_the_grace_window_is_counted_from_the_day_given(self):
        # Three days is the default; the day the rua was published is the input, so a
        # deadline set for a future cutover does not read as a failure today.
        finding, broken = dmarc.no_reports_finding(
            wired=date(2026, 10, 29), grace=3, window=7, today=date(2026, 10, 31))
        self.assertFalse(broken)
        self.assertEqual(finding.level, "note")
        finding, broken = dmarc.no_reports_finding(
            wired=date(2026, 10, 29), grace=3, window=7, today=date(2026, 11, 5))
        self.assertTrue(broken)
        self.assertEqual(finding.code, "no-reports")
        self.assertIn("the reporting path is broken", finding.message)
        self.assertEqual(finding.level, "fail")

    def test_an_empty_reports_directory_is_still_exit_two(self):
        # Unchanged on purpose: an archive someone pointed at is not the rua's age.
        self.assertEqual(dmarc.main(["--reports-dir", str(Path(tempfile.mkdtemp()))]), 2)

    def test_it_writes_the_shared_family_and_its_own_series(self):
        directory = self._dir(report(records=(("203.0.113.9", 4, "none", "fail", "fail", "atheniq.courses"),)))
        textfile = directory / "out.prom"
        dmarc.main(["--reports-dir", str(directory), "--prom", str(textfile)])
        body = textfile.read_text(encoding="utf-8")
        self.assertIn('innotel_estate_check_last_status{check="dmarc_alignment"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="dmarc_alignment"} 1', body)
        self.assertIn("innotel_estate_check_dmarc_messages_both_fail", body)
        self.assertRegex(body, r'innotel_estate_check_dmarc_messages_both_fail\{check="dmarc_alignment"\} 4')
        self.assertRegex(body, r'innotel_estate_check_dmarc_reports\{check="dmarc_alignment"\} 1')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
