#!/usr/bin/env python3
"""Unit tests for check-npm-config-drift.py.

The four shapes come from 2026-10-30, when NPM's own record of a host and the file it
wrote turned out not to be one fact: 25 hosts deleted with their files left serving, and
one host whose `forward_port` said 9 while its file still said 8099. Each is a separate
case here, because each fails differently and none of them makes nginx complain.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-npm-config-drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_npm_config_drift", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drift = _load()

VHOST = """server {{
  set $forward_scheme http;
  set $server         "{host}";
  set $port           {port};

  listen 80;
  server_name {names};
  location / {{
    include conf.d/include/proxy.conf;
  }}
}}
"""


class Estate:
    """A throwaway `nginx/` tree beside the hosts a test says NPM holds."""

    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name) / "nginx"
        (self.root / "proxy_host").mkdir(parents=True)
        (self.root / "redirection_host").mkdir(parents=True)

    def proxy(self, rid: int, names: str, host: str, port: int, enabled: bool = True):
        return drift.Row(kind="proxy_host", id=rid, domains=tuple(names.split()),
                         host=host, port=port, enabled=enabled)

    def redirect(self, rid: int, names: str, enabled: bool = True):
        return drift.Row(kind="redirection_host", id=rid, domains=tuple(names.split()),
                         enabled=enabled)

    def vhost(self, name: str, names: str, host: str, port: int,
              kind="proxy_host", suffix=".conf"):
        path = self.root / kind / f"{name}{suffix}"
        path.write_text(VHOST.format(host=host, port=port, names=names))
        return path

    def check(self, proxy=(), redirect=()):
        return drift.check(self.root, {"proxy_host": list(proxy),
                                       "redirection_host": list(redirect)})

    def close(self):
        self._dir.cleanup()


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.e = Estate()

    def tearDown(self):
        self.e.close()

    def test_a_file_no_host_owns_is_an_orphan_door(self):
        # The 25 deleted hosts: the API says gone, the file still proxies.
        self.e.vhost("1", "kept.innotel.us", "192.168.1.71", 3003)
        self.e.vhost("999", "pbx.innotel.us", "192.168.1.30", 3001)
        code, findings = self.e.check(proxy=[self.e.proxy(1, "kept.innotel.us", "192.168.1.71", 3003)])
        self.assertEqual(code, 1)
        orphan = [f for f in findings if f["check"] == "unmanaged_vhost"]
        self.assertEqual([f["file"] for f in orphan], ["999.conf"])
        self.assertEqual(orphan[0]["target"], "192.168.1.30:3001")

    def test_an_enabled_host_with_no_file_is_missing_not_quiet(self):
        code, findings = self.e.check(proxy=[self.e.proxy(1, "ghost.innotel.us", "192.168.1.30", 3010)])
        self.assertEqual(code, 1)
        self.assertEqual([f["check"] for f in findings], ["missing_vhost"])

    def test_a_host_and_file_naming_different_ports_is_drift(self):
        # resend.innotel.us: host port 9 (parked), file still 8099.
        self.e.vhost("220", "resend.innotel.us", "192.168.1.15", 8099)
        code, findings = self.e.check(
            proxy=[self.e.proxy(220, "resend.innotel.us", "192.168.1.15", 9)])
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["check"], "stale_target")
        self.assertEqual(findings[0]["row_target"], "192.168.1.15:9")
        self.assertEqual(findings[0]["file_target"], "192.168.1.15:8099")

    def test_a_disabled_host_that_still_serves_is_a_failure(self):
        self.e.vhost("1", "off.innotel.us", "192.168.1.30", 3001)
        code, findings = self.e.check(
            proxy=[self.e.proxy(1, "off.innotel.us", "192.168.1.30", 3001, enabled=False)])
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["check"], "disabled_but_served")

    def test_a_disabled_host_with_no_file_is_fine(self):
        code, findings = self.e.check(
            proxy=[self.e.proxy(1, "off.innotel.us", "192.168.1.30", 3001, enabled=False)])
        self.assertEqual((code, findings), (0, []))

    def test_redirections_are_checked_for_presence_only(self):
        (self.e.root / "redirection_host" / "5.conf").write_text(
            "server {\n  server_name old.innotel.us;\n  return 301 https://new.innotel.us;\n}\n")
        self.assertEqual(self.e.check(redirect=[self.e.redirect(5, "old.innotel.us")]), (0, []))
        code, findings = self.e.check()
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["check"], "unmanaged_vhost")

    def test_backups_beside_a_vhost_are_not_doors(self):
        self.e.vhost("1", "kept.innotel.us", "192.168.1.71", 3003)
        self.e.vhost("1", "kept.innotel.us", "192.168.1.71", 3003, suffix=".conf.bak")
        code, _ = self.e.check(proxy=[self.e.proxy(1, "kept.innotel.us", "192.168.1.71", 3003)])
        self.assertEqual(code, 0)

    def test_one_file_serving_several_names_is_one_door(self):
        self.e.vhost("204", "rizzaura.net www.rizzaura.net", "192.168.1.62", 3021)
        code, findings = self.e.check(
            proxy=[self.e.proxy(204, "rizzaura.net www.rizzaura.net", "192.168.1.62", 3021)])
        self.assertEqual((code, findings), (0, []))

    def test_a_missing_vhost_directory_is_exit_2_not_a_pass(self):
        empty = Path(self.e._dir.name) / "absent"
        code, findings = drift.check(empty, {"proxy_host": [], "redirection_host": []})
        self.assertEqual((code, findings), (2, []))


class CredentialTest(unittest.TestCase):
    def test_no_credential_is_an_unreadable_npm_not_an_empty_estate(self):
        with self.assertRaises(drift.Unreadable):
            drift.token_for("http://127.0.0.1:1", "", "")

    def test_an_unreachable_npm_is_unreadable_not_empty(self):
        with self.assertRaises(drift.Unreadable):
            drift.token_for("http://127.0.0.1:1", "a@b", "secret")


class ReportTest(unittest.TestCase):
    def test_the_summary_separates_orphans_from_mismatches(self):
        findings = [
            {"check": "unmanaged_vhost", "kind": "proxy_host", "file": "9.conf",
             "domains": ["a.innotel.us"], "target": "192.168.1.30:3001"},
            {"check": "stale_target", "kind": "proxy_host", "row": 220, "file": "220.conf",
             "domains": ["resend.innotel.us"], "row_target": "192.168.1.15:9",
             "file_target": "192.168.1.15:8099"},
        ]
        text = drift.report(findings)
        self.assertIn("1 orphan file(s)", text)
        self.assertIn("1 row/file mismatch(es)", text)
        self.assertIn("DRIFT", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
