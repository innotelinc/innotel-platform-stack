#!/usr/bin/env python3
"""Unit tests for check-stalwart-oidc-cache.py — the copy Stalwart reads at start.

The check exists because the 2026-11-09 fix moved a failure without removing it: the local
copy keeps its last good bytes when a refresh fails, so an ageing document, a dead proxy, an
uninstalled DNAT and a permission regression all look like a healthy mail server until
somebody tries to sign in. These tests pin each of those as a finding, and pin the exact
things that are *not* findings — a copy one cycle old, a clean run that must still say how
old the answer is.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-stalwart-oidc-cache.py"
NOW = 1_790_000_000


def _load():
    spec = importlib.util.spec_from_file_location("check_stalwart_oidc_cache", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def facts(**overrides):
    base = {
        "proxy": "active",
        "redirect": "active",
        "mtime": NOW - 60,
        "readable": True,
        "cache_readable": True,
        "issuer": check.ISSUER,
        "document_ok": True,
        "now": NOW,
    }
    base.update(overrides)
    return check.Facts(**base)


class TheHealthyCopy(unittest.TestCase):
    def test_a_fresh_copy_is_clean_and_says_how_old_it_is(self):
        report = check.audit(facts())

        self.assertTrue(report.ok)
        self.assertEqual(report.failures, [])
        self.assertEqual([f.code for f in report.notes], ["cache_current"])

    def test_one_missed_refresh_is_still_a_pass(self):
        # 1500 s is one and a bit cycles: a refresh may be in flight, and the copy is the
        # same document either way.
        report = check.audit(facts(mtime=NOW - 1500))

        self.assertTrue(report.ok)
        self.assertEqual([f.code for f in report.warnings], [])


class TheCopyThatIsGoingStale(unittest.TestCase):
    def test_two_missed_refreshes_warn_without_failing(self):
        report = check.audit(facts(mtime=NOW - 2 * check.REFRESH_SECONDS - 1))

        self.assertTrue(report.ok, "an ageing copy is still serving the right document")
        self.assertEqual([f.code for f in report.warnings], ["cache_aging"])

    def test_four_missed_refreshes_fail(self):
        report = check.audit(facts(mtime=NOW - 4 * check.REFRESH_SECONDS - 1))

        self.assertFalse(report.ok)
        self.assertIn("cache_stale", [f.code for f in report.failures])

    def test_a_missing_copy_fails(self):
        report = check.audit(facts(mtime=None))

        self.assertFalse(report.ok)
        self.assertIn("cache_missing", [f.code for f in report.failures])

    def test_a_truncated_copy_fails_rather_than_reading_as_current(self):
        report = check.audit(facts(document_ok=False, document_error="Expecting value: line 1"))

        self.assertFalse(report.ok)
        self.assertIn("cache_unreadable", [f.code for f in report.failures])

    def test_a_document_for_another_issuer_fails(self):
        # Stalwart rejects an issuer mismatch exactly like a failed fetch, so this is not a
        # degraded state — it is the outage.
        report = check.audit(facts(issuer="https://auth.example/application/o/other/"))

        self.assertFalse(report.ok)
        self.assertIn("issuer_mismatch", [f.code for f in report.failures])


class ThePiecesAroundTheCopy(unittest.TestCase):
    def test_a_dead_proxy_fails(self):
        report = check.audit(facts(proxy="inactive"))

        self.assertFalse(report.ok)
        self.assertIn("proxy_inactive", [f.code for f in report.failures])

    def test_an_uninstalled_dnat_fails(self):
        # The copy is fine today and the next start fetches upstream, which is the
        # 2026-11-06 fault waiting for its next blip.
        report = check.audit(facts(redirect="failed"))

        self.assertFalse(report.ok)
        self.assertIn("redirect_inactive", [f.code for f in report.failures])

    def test_a_bundle_the_server_cannot_read_fails(self):
        report = check.audit(facts(readable=False))

        self.assertFalse(report.ok)
        self.assertIn("bundle_unreadable", [f.code for f in report.failures])

    def test_a_copy_the_proxy_cannot_read_fails(self):
        report = check.audit(facts(cache_readable=False))

        self.assertFalse(report.ok)
        self.assertIn("cache_unreadable_by_proxy", [f.code for f in report.failures])

    def test_a_copy_with_no_mtime_does_not_invent_one(self):
        parsed = check.parse("proxy:active\nredirect:active\nmtime:unknown\nnow:%d\n" % NOW)

        self.assertIsNone(parsed.mtime)
        self.assertIsNone(parsed.age_seconds)


class ReadingTheContainer(unittest.TestCase):
    def test_the_document_is_taken_from_between_the_markers(self):
        output = (
            "proxy:active\n"
            "redirect:active\n"
            f"mtime:{NOW - 30}\n"
            "bundle_readable:yes\n"
            "cache_readable:yes\n"
            "document_begin\n"
            '{"issuer": "%s", "jwks_uri": "x"}\n' % check.ISSUER
            + "document_end\n"
            f"now:{NOW}\n"
        )

        parsed = check.parse(output)

        self.assertTrue(parsed.document_ok)
        self.assertEqual(parsed.issuer, check.ISSUER)
        self.assertEqual(parsed.age_seconds, 30)
        self.assertTrue(parsed.readable)
        self.assertTrue(parsed.cache_readable)

    def test_a_document_naming_no_issuer_is_not_a_pass(self):
        parsed = check.parse("proxy:active\nredirect:active\ndocument_begin\n{}\ndocument_end\nnow:%d\n" % NOW)

        self.assertTrue(parsed.document_ok)
        self.assertIsNone(parsed.issuer)
        self.assertFalse(check.audit(parsed).ok)

    def test_a_report_that_stopped_early_does_not_read_as_a_verdict(self):
        # No `now`: the remote script did not finish, so nothing it printed is trustworthy.
        parsed = check.parse("proxy:active\n")

        self.assertEqual(parsed.now, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
