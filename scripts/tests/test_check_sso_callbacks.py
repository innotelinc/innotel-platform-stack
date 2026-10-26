#!/usr/bin/env python3
"""Unit tests for check-sso-callbacks.py — the two rules, and the gate table.

The check exists because a person signed in to Onyx and got **502** at the callback:
oauth2-proxy's session cookie is larger than nginx's stock 4k header buffer, so the
proxy in front read half a header block. Everything here was green while that was
true — the gate's front page, its redirect, every other check — which is why the
rules are pinned: a 500 is the gate answering a bogus code correctly and must not
page, a 502 is the proxy failing *after* a successful sign-in and must, and a gate
whose proxy block cannot be read is a failure to run rather than a pass.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-sso-callbacks.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_sso_callbacks", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sso = _load()

BIG = """
server {
    listen 443 ssl;
    server_name onyx.innotel.us;
    proxy_buffer_size 32k;
    large_client_header_buffers 4 32k;
    proxy_pass http://127.0.0.1:8090;
}
"""

STOCK = """
server {
    listen 443 ssl;
    server_name re.genie.innotel.us;
    proxy_pass http://127.0.0.1:8091;
}
"""

SMALL = """
server {
    listen 443 ssl;
    server_name uc.genie.innotel.us;
    proxy_buffer_size 8k;
    large_client_header_buffers 4 8k;
    proxy_pass http://127.0.0.1:8092;
}
"""


def _gate(host="onyx.innotel.us", gate="oauth2-proxy", callback="/oauth2/callback"):
    return {"host": host, "callback": callback, "gate": gate}


class GateTableCase(unittest.TestCase):
    def test_the_check_label_is_the_one_the_alert_rule_names(self):
        # `EstateCheckAbsent` names this label; a rename here would leave the check
        # unwatched for going silent, which is the failure mode it was written for.
        self.assertEqual(sso.CHECK, "sso_callback")

    def test_the_table_is_pinned_so_a_new_gate_cannot_be_added_silently(self):
        # The gates are listed rather than discovered: a host that gains a sign-in gate
        # has to be added here, and adding one moves this test — the point is that the
        # reach of the check is a decision, not a side effect of a proxy host existing.
        self.assertEqual(
            [(g["host"], g["callback"], g["gate"]) for g in sso.GATES],
            [
                ("onyx.innotel.us", "/oauth2/callback", "oauth2-proxy"),
                ("re.genie.innotel.us", "/oauth2/callback", "oauth2-proxy"),
                ("uc.genie.innotel.us", "/oauth2/callback", "oauth2-proxy"),
                ("nas.innotel.us", "/oauth2/callback", "oauth2-proxy"),
                ("proxy.innotel.us", "/oauth2/callback", "oauth2-proxy"),
                ("tix.ontrak.technology", "/api/sso/callback", "app"),
            ],
        )

    def test_the_buffer_floor_is_above_nginxs_default(self):
        self.assertGreater(sso.MIN_BUFFER, 4 * 1024)


class SizeCase(unittest.TestCase):
    def test_sizes_are_bytes(self):
        self.assertEqual(sso._size("16384"), 16384)
        self.assertEqual(sso._size("32k"), 32 * 1024)
        self.assertEqual(sso._size("4K"), 4 * 1024)
        self.assertEqual(sso._size("1m"), 1024 ** 2)

    def test_a_non_size_is_no_reading(self):
        for value in ("", None, "off", "32kb", "k"):
            self.assertIsNone(sso._size(value), value)


class BufferCase(unittest.TestCase):
    def test_the_response_buffer_is_read(self):
        self.assertEqual(sso.buffer_of(BIG, "proxy_buffer_size"), 32 * 1024)

    def test_the_request_buffers_read_the_size_not_the_count(self):
        # `large_client_header_buffers 4 32k` is four buffers of 32k; reading the count
        # would call every gated host fine.
        self.assertEqual(sso.buffer_of(BIG, "large_client_header_buffers"), 32 * 1024)

    def test_an_unset_directive_is_no_reading(self):
        self.assertIsNone(sso.buffer_of(STOCK, "proxy_buffer_size"))


class ServerBlockCase(unittest.TestCase):
    def test_each_server_name_maps_to_its_block(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "1.conf").write_text(BIG, encoding="utf-8")
            Path(directory, "2.conf").write_text(STOCK, encoding="utf-8")
            blocks = sso.server_blocks(directory)
        self.assertIn("onyx.innotel.us", blocks)
        self.assertIn("re.genie.innotel.us", blocks)

    def test_a_missing_directory_is_no_blocks(self):
        # Rule 3: not being able to read the proxy configuration is exit 2, never a pass.
        self.assertEqual(sso.server_blocks("/nonexistent/npm"), {})


class AuditCase(unittest.TestCase):
    def test_a_gate_that_answers_and_fits_is_not_a_finding(self):
        result = sso.audit({"onyx.innotel.us": BIG}, {"onyx.innotel.us": 400}, [_gate()])
        self.assertEqual(result.findings, [])
        self.assertTrue(result.ok)

    def test_a_502_on_the_callback_is_the_2026_10_25_failure(self):
        result = sso.audit({"onyx.innotel.us": BIG}, {"onyx.innotel.us": 502}, [_gate()])
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["callback_gateway_error"])

    def test_every_gateway_error_fails(self):
        for status in (502, 503, 504):
            result = sso.audit({"onyx.innotel.us": BIG}, {"onyx.innotel.us": status}, [_gate()])
            self.assertFalse(result.ok, status)

    def test_a_500_is_the_gate_rejecting_a_bogus_code_not_a_failure(self):
        # The probe sends a fake code on purpose; 500 is oauth2-proxy's correct answer.
        result = sso.audit({"onyx.innotel.us": BIG}, {"onyx.innotel.us": 500}, [_gate()])
        self.assertTrue(result.ok)
        self.assertEqual(result.findings, [])

    def test_no_answer_at_all_is_a_failure(self):
        result = sso.audit({"onyx.innotel.us": BIG}, {}, [_gate()])
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["callback_unreachable"])

    def test_a_gated_host_with_no_proxy_block_fails(self):
        result = sso.audit({}, {"onyx.innotel.us": 302}, [_gate()])
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["no_proxy_block"])

    def test_a_stock_buffer_fails_before_anyone_signs_in(self):
        result = sso.audit({"re.genie.innotel.us": STOCK}, {"re.genie.innotel.us": 302},
                           [_gate(host="re.genie.innotel.us")])
        self.assertFalse(result.ok)
        self.assertEqual(sorted(f.code for f in result.failures), ["stock_buffer", "stock_buffer"])

    def test_a_buffer_below_the_floor_fails(self):
        result = sso.audit({"uc.genie.innotel.us": SMALL}, {"uc.genie.innotel.us": 302},
                           [_gate(host="uc.genie.innotel.us")])
        self.assertFalse(result.ok)
        self.assertEqual(sorted(f.code for f in result.failures), ["small_buffer", "small_buffer"])

    def test_an_app_gates_callback_is_probed_but_its_buffer_is_not_judged(self):
        # tix's session cookie is an id, not an ID token: rule 2 does not apply, so a host
        # with no proxy block here is not a finding — only its callback's answer is.
        result = sso.audit({}, {"tix.ontrak.technology": 302},
                           [_gate(host="tix.ontrak.technology", callback="/api/sso/callback",
                                  gate="app")])
        self.assertTrue(result.ok)

    def test_the_finding_names_the_host_so_the_fix_is_obvious(self):
        result = sso.audit({"onyx.innotel.us": BIG}, {"onyx.innotel.us": 502}, [_gate()])
        self.assertIn("onyx.innotel.us", result.failures[0].message)


class CannotRunCase(unittest.TestCase):
    def test_no_proxy_configuration_is_exit_two_and_a_published_failure(self):
        # Rule 3 again, at the entry point: silence is what 2026-10-25 looked like.
        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory, "sso.prom")
            code = sso.main(["--npm-conf", str(Path(directory, "absent")), "--prom", str(prom)])
            self.assertEqual(code, 2)
            body = prom.read_text(encoding="utf-8")
        self.assertIn('innotel_estate_check_last_status{check="sso_callback"} 0', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
