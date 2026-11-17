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
#: The canonical identity host (`docs/identity-hosts.md`): the same gate on the other name,
#: which this check has to catch just as loudly.
AUTH_INNOTEL = "https://auth.innotel.us/application/o/authorize/?client_id=x"
JELLYFIN_PAGE = b"<!doctype html><html><head><title>Jellyfin</title></head></html>"
SPLASH = b"\x89PNG\r\n\x1a\n" + b"0" * 60_000


def probe_for(*, media=(302, {"Location": "web/"}, b""), web=(200, {}, JELLYFIN_PAGE),
              splash=(200, {"Content-Type": "image/png"}, SPLASH),
              front=(200, {}, JELLYFIN_PAGE)):
    answers = {
        "https://media.innotel.us/": media,
        "https://media.innotel.us/web/": front,
        "https://iptv.innotel.us/web/": web,
        "https://iptv.innotel.us/Branding/Splashscreen": splash,
    }

    def probe(url, timeout=20.0):
        return answers[url]

    return probe


def codes(result) -> list[str]:
    return [finding.code for finding in result.findings]


class FrontPage(unittest.TestCase):
    def test_the_app_relative_hop_to_jellyfin_is_the_working_answer(self):
        # `/` answers `302 web/` and `web/` is Jellyfin: one hop, the app's own page.
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for())
        self.assertTrue(result.ok)
        self.assertEqual(codes(result), [])

    def test_a_direct_200_from_jellyfin_is_also_the_working_answer(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(200, {}, JELLYFIN_PAGE)))
        self.assertTrue(result.ok)

    def test_a_redirect_to_authentik_is_now_the_regression(self):
        # The gate itself was the outage: a TV client cannot complete an OIDC webview flow.
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(302, {"Location": AUTH}, b"")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["gate-restored"])

    def test_the_gate_is_caught_on_the_canonical_brand_too(self):
        # The estate serves one authentik under two names, so an assertion that only knows
        # one of them is an assertion that stops working the day a gate moves.
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(302, {"Location": AUTH_INNOTEL}, b"")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["gate-restored"])

    def test_a_200_that_is_not_jellyfin_is_named(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(200, {}, b"<html>Welcome to nginx</html>")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["front-not-jellyfin"])

    def test_the_hop_landing_on_a_non_jellyfin_page_is_named(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(front=(200, {}, b"<html>proxy error</html>")))
        self.assertEqual(codes(result), ["front-not-jellyfin"])

    def test_a_redirect_off_the_app_is_named_not_passed(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(301, {"Location": "https://example.org/"}, b"")))
        self.assertEqual(codes(result), ["front-wrong-target"])

    def test_a_gateway_error_names_the_app_being_unreachable(self):
        # A 502 is the edge failing to reach Jellyfin — its start-up window looks exactly
        # like this — so it must not read the same as a wrong page on the door.
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(502, {}, b"<html>502</html>")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["front-gateway"])

    def test_a_server_error_from_the_app_itself_is_front_status(self):
        # 500 is the app answering, not the proxy failing to reach it.
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(500, {}, b"<html>500</html>")))
        self.assertEqual(codes(result), ["front-status"])

    def test_no_answer_is_reported_as_unreachable(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(media=(None, {}, b"")))
        self.assertEqual(codes(result), ["front-unreachable"])


class WatchDoor(unittest.TestCase):
    def test_an_error_page_that_is_200_is_not_jellyfin(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(web=(200, {}, b"<html>Welcome to nginx</html>")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["watch-not-jellyfin"])

    def test_a_status_that_is_not_200_fails(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(web=(404, {}, b"")))
        self.assertEqual(codes(result), ["watch-status"])

    def test_the_watch_door_being_down_fails_the_check(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(web=(None, {}, b"")))
        self.assertFalse(result.ok)
        self.assertEqual([f.level for f in result.findings], ["fail"])

    def test_a_gateway_error_names_the_restart_window(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(web=(502, {}, b"")))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["watch-gateway"])
        self.assertIn("start-up window", result.findings[0].message)


class Splash(unittest.TestCase):
    def test_missing_branding_is_a_warning_not_a_failure(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(splash=(404, {"Content-Type": "text/html"}, b"")))
        self.assertTrue(result.ok)
        self.assertEqual([(f.code, f.level) for f in result.findings],
                         [("branding-missing", "warn")])

    def test_a_gateway_error_while_jellyfin_restarts_is_not_missing_branding(self):
        # The 2026-11-16 warning: a 502 on the splash during Jellyfin's start-up window
        # says nothing about whether the Gizmo splash is still installed.
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(splash=(502, {"Content-Type": "text/html"}, b"")))
        self.assertTrue(result.ok)
        self.assertEqual([(f.code, f.level) for f in result.findings],
                         [("branding-gateway", "warn")])

    def test_a_placeholder_image_is_under_the_floor(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
                             probe=probe_for(splash=(200, {"Content-Type": "image/png"}, b"x" * 900)))
        self.assertEqual(codes(result), ["branding-small"])

    def test_the_wrong_content_type_is_named(self):
        result = check.audit("https://media.innotel.us/", "https://iptv.innotel.us",
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
        # The label is the script's `CHECK` constant, which is `media_doors` — the family's
        # spelling, not the hyphenated inline literal this file used to pass. The textfile
        # keeps its `media-doors.prom` name: that is a path in the unit, not a label.
        self.assertIn('innotel_estate_check_last_status{check="media_doors"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="media_doors"} 1', body)
        self.assertNotIn("last_success_timestamp", body)


class Settle(unittest.TestCase):
    """A gateway answer gets the app's start-up window before it is a failure.

    2026-11-16: a watchtower restart of Jellyfin answered 502 for a couple of minutes, the
    check failed twice, and both runs were green on a hand re-run. The failure was real at
    the moment it was measured and useless as an alert, which is the distinction these tests
    pin: the same answer is a note when it clears and a failure when it does not.
    """

    def _settle(self, probe, attempts=2):
        return check.settle("https://media.innotel.us/", "https://iptv.innotel.us",
                            probe=probe, attempts=attempts, delay=0, sleep=lambda _: None)

    def test_a_gateway_answer_that_clears_is_a_note_not_a_failure(self):
        base = probe_for()
        seen = {"probes": 0}

        def probe(url, timeout=20.0):
            if url == "https://media.innotel.us/":
                seen["probes"] += 1
                if seen["probes"] == 1:
                    return 502, {}, b"<html>502</html>"
                return 200, {}, JELLYFIN_PAGE
            return base(url)

        result, recovered = self._settle(probe)
        self.assertTrue(result.ok)
        self.assertEqual(codes(result), [])
        self.assertEqual([(f.code, f.level) for f in recovered], [("front-recovered", "note")])
        self.assertIn("start-up window", recovered[0].message)

    def test_a_gateway_answer_that_stays_is_still_a_failure(self):
        result, recovered = self._settle(probe_for(media=(502, {}, b""), web=(502, {}, b"")))
        self.assertFalse(result.ok)
        self.assertEqual(recovered, [])
        self.assertEqual(sorted(codes(result)), ["front-gateway", "watch-gateway"])

    def test_a_wrong_page_is_not_re_examined(self):
        # 500 is the app answering, not the proxy failing to reach it; a second look cannot
        # change that, and treating it as a start-up window would hide a real fault.
        base = probe_for(media=(500, {}, b"<html>500</html>"))
        seen = {"probes": 0}

        def probe(url, timeout=20.0):
            seen["probes"] += 1
            return base(url)

        result, recovered = self._settle(probe, attempts=3)
        self.assertEqual(codes(result), ["front-status"])
        self.assertEqual(recovered, [])
        # One pass: media, watch, splash — nothing was asked a second time.
        self.assertEqual(seen["probes"], 3)

    def test_main_settles_a_start_up_window_to_zero(self):
        base = probe_for()
        seen = {"probes": 0}

        def probe(url, timeout=20.0):
            if url == "https://media.innotel.us/":
                seen["probes"] += 1
                if seen["probes"] == 1:
                    return 503, {}, b""
                return 200, {}, JELLYFIN_PAGE
            return base(url)

        original = check.fetch
        check.fetch = probe
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                code = check.main(["--settle-attempts", "2", "--settle-delay", "0"])
        finally:
            check.fetch = original
        self.assertEqual(code, 0)
        self.assertIn("NOTE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
