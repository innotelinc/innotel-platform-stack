#!/usr/bin/env python3
"""Unit tests for check-edge-vhost-pins.py.

The fact that cost the afternoon of 2026-11-09: the pin was compared to the file, and the
row was compared to the file, but never the pin to the row — so a row rewritten to the
SSO gateway and regenerated into the file read as two copies agreeing. The tests here hold
that comparison, and the one thing an apply must never do: drop a field it was not asked
to change (`advanced_config`, `certificate_id`, `ssl_forced` are load-bearing for a door
that serves video).
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-edge-vhost-pins.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_edge_vhost_pins", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pins = _load()

VHOST = """server {{
  set $forward_scheme http;
  set $server         "{host}";
  set $port           {port};

  listen 80;
  server_name {domain};
  location / {{
    include conf.d/include/proxy.conf;
  }}
}}
"""


def row(rid: int, domain: str, host: str, port: int, **extra) -> dict:
    """A proxy host in the shape NPM's API returns, with the fields tv actually has."""
    raw = {
        "id": rid,
        "created_on": "2026-09-15T01:17:32.000Z",
        "modified_on": "2026-11-09T18:26:27.000Z",
        "owner_user_id": 1,
        "domain_names": [domain],
        "forward_host": host,
        "forward_port": port,
        "access_list_id": 0,
        "certificate_id": 34,
        "ssl_forced": True,
        "caching_enabled": False,
        "block_exploits": True,
        "advanced_config": "client_max_body_size 0;",
        "meta": {"nginx_online": True, "nginx_err": None},
        "allow_websocket_upgrade": True,
        "http2_support": True,
        "forward_scheme": "http",
        "enabled": True,
        "locations": [],
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "trust_forwarded_proto": False,
    }
    raw.update(extra)
    return raw


class Estate:
    """A throwaway nginx tree beside the rows a test says NPM holds."""

    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        (self.root / "proxy_host").mkdir()
        self.pins = [pins.Pin("tv.innotel.us", "192.168.1.56", 14016)]

    def vhost(self, domain: str, host: str, port: int, name: str = "161.conf"):
        (self.root / "proxy_host" / name).write_text(
            VHOST.format(domain=domain, host=host, port=port))

    def check(self, rows: list[dict], pins_list=None):
        return pins.check(pins_list or self.pins, [pins.Host(raw=r) for r in rows],
                          pins.read_vhosts(self.root))

    def close(self):
        self._dir.cleanup()


class PinTest(unittest.TestCase):
    def setUp(self):
        self.e = Estate()

    def tearDown(self):
        self.e.close()

    def test_a_row_that_moved_while_the_file_did_not_is_row_drift(self):
        # The 15:30 repair: file correct, row still the gateway. This is the state that
        # no check read, and the next regeneration is what turns it into an outage.
        self.e.vhost("tv.innotel.us", "192.168.1.56", 14016)
        code, findings = self.e.check([row(161, "tv.innotel.us", "192.168.1.56", 14002)])
        self.assertEqual(code, 1)
        self.assertEqual([f["check"] for f in findings], ["row_drift"])
        self.assertEqual(findings[0]["row_target"], "192.168.1.56:14002")
        self.assertEqual(findings[0]["pin"], "192.168.1.56:14016")

    def test_a_row_that_holds_and_a_file_that_lags_is_file_lag(self):
        # The 17:53 state in reverse: the row is what regenerates the file, so the file
        # is the copy that follows — naming it separately says which side to look at.
        self.e.vhost("tv.innotel.us", "192.168.1.56", 14002)
        code, findings = self.e.check([row(161, "tv.innotel.us", "192.168.1.56", 14016)])
        self.assertEqual(code, 1)
        self.assertEqual([f["check"] for f in findings], ["file_lag"])
        self.assertEqual(findings[0]["file"], "161.conf")

    def test_a_pin_npm_has_no_host_for_is_not_a_pass(self):
        self.e.vhost("tv.innotel.us", "192.168.1.56", 14016)
        code, findings = self.e.check([])
        self.assertEqual(code, 1)
        self.assertEqual([f["check"] for f in findings], ["no_host"])

    def test_a_missing_file_is_lag_with_no_target(self):
        code, findings = self.e.check([row(161, "tv.innotel.us", "192.168.1.56", 14016)])
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["check"], "file_lag")
        self.assertIsNone(findings[0]["file_target"])

    def test_a_pin_that_holds_in_both_copies_is_clean(self):
        self.e.vhost("tv.innotel.us", "192.168.1.56", 14016)
        code, findings = self.e.check([row(161, "tv.innotel.us", "192.168.1.56", 14016)])
        self.assertEqual((code, findings), (0, []))

    def test_other_hosts_on_the_same_row_do_not_confuse_the_pin(self):
        # One file can serve several names; the pin is matched by its own domain.
        self.e.vhost("tv.innotel.us alt.innotel.us", "192.168.1.56", 14016)
        code, findings = self.e.check([
            row(161, "alt.innotel.us", "192.168.1.56", 14016),
            row(162, "tv.innotel.us", "192.168.1.56", 14016),
        ], pins_list=[pins.Pin("tv.innotel.us", "192.168.1.56", 14016)])
        self.assertEqual((code, findings), (0, []))


class PayloadTest(unittest.TestCase):
    def setUp(self):
        self.pin = pins.Pin("tv.innotel.us", "192.168.1.56", 14016)

    def test_the_payload_carries_every_field_the_estate_must_not_lose(self):
        body = pins.payload_for(row(161, "tv.innotel.us", "192.168.1.56", 14002), self.pin)
        self.assertEqual(body["advanced_config"], "client_max_body_size 0;")
        self.assertEqual(body["certificate_id"], 34)
        self.assertTrue(body["ssl_forced"])
        self.assertEqual(body["domain_names"], ["tv.innotel.us"])
        self.assertEqual(body["forward_host"], "192.168.1.56")
        self.assertEqual(body["forward_port"], 14016)

    def test_the_payload_leaves_the_panels_own_bookkeeping_alone(self):
        body = pins.payload_for(row(161, "tv.innotel.us", "192.168.1.56", 14002), self.pin)
        for key in ("id", "created_on", "modified_on", "owner_user_id", "meta"):
            self.assertNotIn(key, body)

    def test_a_clean_round_trip_reports_nothing(self):
        before = row(161, "tv.innotel.us", "192.168.1.56", 14002)
        after = dict(row(161, "tv.innotel.us", "192.168.1.56", 14016,
                         modified_on="2026-11-09T19:04:00.000Z"))
        self.assertEqual(pins.round_trip(before, after, self.pin), [])

    def test_a_dropped_field_is_named(self):
        before = row(161, "tv.innotel.us", "192.168.1.56", 14002)
        after = dict(row(161, "tv.innotel.us", "192.168.1.56", 14016), advanced_config="")
        self.assertEqual(pins.round_trip(before, after, self.pin), ["advanced_config"])

    def test_a_target_that_did_not_take_is_named(self):
        before = row(161, "tv.innotel.us", "192.168.1.56", 14002)
        after = row(161, "tv.innotel.us", "192.168.1.56", 14002)
        self.assertIn("forward_port", pins.round_trip(before, after, self.pin))


class ApplyTest(unittest.TestCase):
    """The write path, driven through an injected panel so no test touches a real door."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "proxy_host").mkdir()
        self.pin = pins.Pin("tv.innotel.us", "192.168.1.56", 14016)
        self.before = row(161, "tv.innotel.us", "192.168.1.56", 14002)
        self.calls = []
        self._fetch, self._put = pins.fetch_hosts, pins.put_host

    def tearDown(self):
        pins.fetch_hosts, pins.put_host = self._fetch, self._put
        self.tmp.cleanup()

    def _panel(self, reply: dict):
        pins.fetch_hosts = lambda api, token: [pins.Host(raw=self.before)]
        pins.put_host = lambda api, token, hid, body: self.calls.append(body) or reply

    def _apply(self):
        finding = {"check": "row_drift", "domain": "tv.innotel.us", "row": 161,
                   "pin": "192.168.1.56:14016", "row_target": "192.168.1.56:14002"}
        return pins.apply_pins("http://panel", "token", [self.pin], [finding],
                               config_dir=self.dir, sleep=lambda _: None, attempts=1)

    def test_apply_sends_the_pin_with_every_other_field_intact(self):
        self._panel(dict(row(161, "tv.innotel.us", "192.168.1.56", 14016),
                         modified_on="2026-11-09T19:10:00.000Z"))
        problems = self._apply()
        self.assertEqual(problems, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["forward_port"], 14016)
        self.assertEqual(self.calls[0]["advanced_config"], "client_max_body_size 0;")
        self.assertNotIn("id", self.calls[0])

    def test_apply_reports_a_field_the_panel_dropped(self):
        self._panel(dict(row(161, "tv.innotel.us", "192.168.1.56", 14016),
                         advanced_config=""))
        problems = self._apply()
        self.assertEqual([p["check"] for p in problems], ["apply_lost"])
        self.assertEqual(problems[0]["detail"], "advanced_config")

    def test_a_clean_estate_writes_nothing(self):
        self._panel({})
        self.assertEqual(pins.apply_pins("http://panel", "token", [self.pin], [],
                                        config_dir=self.dir, sleep=lambda _: None), [])
        self.assertEqual(self.calls, [])


class DeclarationTest(unittest.TestCase):
    def test_the_estates_own_declaration_parses(self):
        loaded = pins.load_pins(SCRIPTS / "edge-vhost-targets.tsv")
        domains = [pin.domain for pin in loaded]
        self.assertIn("admin.monarch.innotel.us", domains)
        self.assertIn("tv.innotel.us", domains)
        tv = next(pin for pin in loaded if pin.domain == "tv.innotel.us")
        self.assertEqual(tv.target, "192.168.1.56:14016")
        self.assertTrue(tv.note, "a pin carries the reason it is a pin")

    def test_a_short_line_is_an_error_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "pins.tsv"
            bad.write_text("tv.innotel.us\t192.168.1.56\n")
            with self.assertRaises(ValueError):
                pins.load_pins(bad)


class CredentialTest(unittest.TestCase):
    def test_no_credential_is_unreadable_not_an_empty_estate(self):
        with self.assertRaises(pins.Unreadable):
            pins.token_for("http://127.0.0.1:1", "", "")

    def test_an_unreachable_panel_is_unreadable_not_empty(self):
        with self.assertRaises(pins.Unreadable):
            pins.token_for("http://127.0.0.1:1", "a@b", "secret")


class ReportTest(unittest.TestCase):
    def test_the_summary_separates_the_three_failures(self):
        text = pins.report([
            {"check": "row_drift", "domain": "tv.innotel.us", "row": 161,
             "pin": "192.168.1.56:14016", "row_target": "192.168.1.56:14002"},
            {"check": "file_lag", "domain": "b.innotel.us", "row": 2,
             "pin": "1.2.3.4:80", "file_target": "1.2.3.4:81", "file": "2.conf"},
            {"check": "no_host", "domain": "c.innotel.us", "pin": "1.2.3.4:80"},
        ])
        self.assertIn("1 row drift", text)
        self.assertIn("1 file lag", text)
        self.assertIn("1 pinned name NPM does not have", text)
        self.assertIn("ROW-DRIFT", text)

    def test_the_audit_names_the_regeneration_risk(self):
        audit = pins.to_audit([
            {"check": "row_drift", "domain": "tv.innotel.us", "row": 161,
             "pin": "192.168.1.56:14016", "row_target": "192.168.1.56:14002"}])
        self.assertEqual(len(audit.failures), 1)
        self.assertEqual(audit.failures[0].code, "row_drift")
        self.assertIn("next regeneration", audit.failures[0].message)


if __name__ == "__main__":
    unittest.main()
