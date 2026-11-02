#!/usr/bin/env python3
"""Unit tests for check-media-doors.py — the two doors Gizmo was reduced to.

The property that matters is the one a person would get wrong by eye, and it flipped on
2026-09-18: `media.innotel.us` answering with **Jellyfin's own page** is the success, and a
redirect to Authentik is now the failure — gating the front page sent TV clients into a
webview flow they cannot complete, so the gate was removed and Jellyfin serves its own
sign-in. `/` answers `302 web/`, which is the app's page one relative hop away; the check
follows exactly that hop. The watch door still has to be Jellyfin rather than an error page
that happens to return 200. Everything here runs against a fake probe.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-media-doors.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_media_doors", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

AUTH = "https://auth.cerulean.innotel.us/application/o/authorize/?client_id=x"
JELLYFIN_PAGE = b"<!doctype html><html><head><title>Jellyfin</title></head></html>"
SPLASH = b"\x89PNG\r\n\x1a\n" + b"0" * 60_000


def probe_for(*, media=(302, {"Location": "web/"}, b""), web=(200, {}, JELLYFIN_PAGE),
              splash=(200, {"Content-Type": "image/png"}, SPLASH),
              front=(200, {}, JELLYFIN_PAGE)):
    answers = {
        "https://media.innotel.us/": media,
        "https://media.innotel.us/web/": front,
        "https://tv.innotel.us/web/": web,
        "https://tv.innotel.us/Branding/Splashscreen": splash,
    }

    def probe(url, timeout=20.0):
        return answers[url]

    return probe


def codes(result) -> list[str]:
    return [finding.code for finding in result.findings]


class FrontPage(unittest.TestCase):
    def test_the_app_relative_hop_to_jellyfin_is_the_working_answer(self):
        # `/` answers `302 web/` and `web/` is Jellyfin: one hop, the app's own page.
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for())
        self.assertTrue(result.ok)
        self.assertEqual(codes(result), [])

    def test_a_direct_200_from_jellyfin_is_also_the_working_answer(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(media=(200, {}, JELLYFIN_PAGE)))
        self.assertTrue(result.ok)

    def test_a_redirect_to_authentik_is_now_the_regression(self):
        # The gate itself was the outage: a TV client cannot complete an OIDC webview flow.
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(media=(302, {"Location": AUTH}, b"")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["gate-restored"])

    def test_a_200_that_is_not_jellyfin_is_named(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(media=(200, {}, b"<html>Welcome to nginx</html>")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["front-not-jellyfin"])

    def test_the_hop_landing_on_a_non_jellyfin_page_is_named(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(front=(200, {}, b"<html>proxy error</html>")))
        self.assertEqual(codes(result), ["front-not-jellyfin"])

    def test_a_redirect_off_the_app_is_named_not_passed(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(media=(301, {"Location": "https://example.org/"}, b"")))
        self.assertEqual(codes(result), ["front-wrong-target"])

    def test_a_five_hundred_is_the_app_being_down(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(media=(502, {}, b"<html>502</html>")))
        self.assertEqual(codes(result), ["front-status"])

    def test_no_answer_is_reported_as_unreachable(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(media=(None, {}, b"")))
        self.assertEqual(codes(result), ["front-unreachable"])


class WatchDoor(unittest.TestCase):
    def test_an_error_page_that_is_200_is_not_jellyfin(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(web=(200, {}, b"<html>Welcome to nginx</html>")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["watch-not-jellyfin"])

    def test_a_status_that_is_not_200_fails(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(web=(404, {}, b"")))
        self.assertEqual(codes(result), ["watch-status"])

    def test_the_watch_door_being_down_fails_the_check(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(web=(None, {}, b"")))
        self.assertFalse(result.ok)
        self.assertEqual([f.level for f in result.findings], ["fail"])


class Splash(unittest.TestCase):
    def test_missing_branding_is_a_warning_not_a_failure(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(splash=(404, {"Content-Type": "text/html"}, b"")))
        self.assertTrue(result.ok)
        self.assertEqual([(f.code, f.level) for f in result.findings],
                         [("branding-missing", "warn")])

    def test_a_placeholder_image_is_under_the_floor(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(splash=(200, {"Content-Type": "image/png"}, b"x" * 900)))
        self.assertEqual(codes(result), ["branding-small"])

    def test_the_wrong_content_type_is_named(self):
        result = check.audit("https://media.innotel.us/", "https://tv.innotel.us",
                             probe=probe_for(splash=(200, {"Content-Type": "text/html"}, SPLASH)))
        self.assertEqual(codes(result), ["branding-type"])


class ExitCodes(unittest.TestCase):
    def _run(self, probe, *args):
        original = check.fetch
        check.fetch = probe
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(list(args))
            return code, out.getvalue(), err.getvalue()
        finally:
            check.fetch = original

    def test_clean_run_exits_zero(self):
        code, out, _ = self._run(probe_for())
        self.assertEqual(code, 0)
        self.assertIn("0 failure(s)", out)

    def test_a_failure_exits_one(self):
        code, out, _ = self._run(probe_for(media=(302, {"Location": AUTH}, b"")))
        self.assertEqual(code, 1)
        self.assertIn("the OIDC gate is back in front of the front page", out)

    def test_neither_door_answering_is_exit_two(self):
        probe = probe_for(media=(None, {}, b""), web=(None, {}, b""))
        code, _, err = self._run(probe)
        self.assertEqual(code, 2)
        self.assertIn("cannot see the names", err)

    def test_the_textfile_carries_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media-doors.prom"
            code, _, _ = self._run(probe_for(media=(302, {"Location": AUTH}, b"")), "--prom", str(path))
            self.assertEqual(code, 1)
            body = path.read_text()
        self.assertIn('innotel_estate_check_last_status{check="media-doors"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="media-doors"} 1', body)
        self.assertNotIn("last_success_timestamp", body)


if __name__ == "__main__":
    unittest.main()
