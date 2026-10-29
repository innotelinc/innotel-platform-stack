#!/usr/bin/env python3
"""Unit tests for check-edge-vhost-targets.py.

The two facts that cost a day: a vhost can point at a closed port and nginx will happily
proxy into it (502 for the browser, nothing in the config to catch the eye), and the
targets that are a contract — the SSO gateway's address — have to be held to a file
instead of to whoever last edited the container. Both are locked here, along with the
parked port, which must not be reported as a dead target: `ai.lms.innotel.us` is retired
on purpose and a check that cries about it gets switched off.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-edge-vhost-targets.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_edge_vhost_targets", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


targets = _load()

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


class Temp:
    """A throwaway `/data/nginx` with the vhosts a test asks for."""

    def __init__(self, vhosts: dict[str, tuple[str, int]]):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name) / "proxy_host"
        root.mkdir()
        for n, (domain, (host, port)) in enumerate(vhosts.values(), start=1):
            (root / f"{n}.conf").write_text(VHOST.format(domain=domain, host=host, port=port))
        self.config_dir = Path(self.dir.name)

    def decl(self, rows: list[tuple[str, str, int]]) -> Path:
        path = Path(self.dir.name) / "targets.tsv"
        body = "# domain\thost\tport\tnote\n"
        body += "".join(f"{d}\t{h}\t{p}\tnote\n" for d, h, p in rows)
        path.write_text(body)
        return path

    def close(self):
        self.dir.cleanup()


def alive_targets(*pairs: tuple[str, int]):
    """A probe that answers for exactly the given targets."""
    return lambda host, port, timeout=2.0: (host, port) in pairs


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.temp = Temp({
            "1.conf": ("proxy.innotel.us", ("172.31.42.10", 4180)),
            "2.conf": ("admin.monarch.innotel.us", ("127.0.0.1", 4180)),
        })

    def tearDown(self):
        self.temp.close()

    def test_a_loopback_target_that_moved_to_the_gateway_is_drift(self):
        expect = self.temp.decl([
            ("proxy.innotel.us", "172.31.42.10", 4180),
            ("admin.monarch.innotel.us", "172.31.42.10", 4180),
        ])
        code, findings = targets.check(
            self.temp.config_dir, expect, probe=alive_targets(("172.31.42.10", 4180)))
        self.assertEqual(code, 1)
        drifted = [f for f in findings if f["check"] == "drift"]
        self.assertEqual([f["domain"] for f in drifted], ["admin.monarch.innotel.us"])
        self.assertEqual(drifted[0]["got"], "127.0.0.1:4180")

    def test_apply_rewrites_the_target_and_keeps_a_backup(self):
        expect = self.temp.decl([("admin.monarch.innotel.us", "172.31.42.10", 4180)])
        code, findings = targets.check(
            self.temp.config_dir, expect, apply=True,
            probe=alive_targets(("172.31.42.10", 4180), ("127.0.0.1", 4180)))
        self.assertEqual(code, 0, findings)
        written = (self.temp.config_dir / "proxy_host" / "2.conf").read_text()
        self.assertIn('set $server         "172.31.42.10";', written)
        self.assertIn("set $port           4180;", written)
        self.assertTrue((self.temp.config_dir / "proxy_host" / "2.conf.bak").is_file())

    def test_a_missing_vhost_is_drift_not_a_silent_pass(self):
        expect = self.temp.decl([("new.innotel.us", "172.31.42.10", 4180)])
        code, findings = targets.check(
            self.temp.config_dir, expect, probe=alive_targets(("172.31.42.10", 4180)))
        self.assertEqual(code, 1)
        self.assertIsNone(findings[0]["got"])


class LivenessTest(unittest.TestCase):
    def test_a_closed_target_fails_and_names_its_domains(self):
        temp = Temp({"1.conf": ("admin.monarch.innotel.us", ("127.0.0.1", 4180))})
        try:
            code, findings = targets.check(temp.config_dir, None, probe=lambda *a: False)
        finally:
            temp.close()
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["state"], "dead")
        self.assertEqual(findings[0]["domains"], ["admin.monarch.innotel.us"])

    def test_the_parked_discard_port_is_not_reported_as_dead(self):
        temp = Temp({"1.conf": ("ai.lms.innotel.us", ("127.0.0.1", 9))})
        try:
            code, findings = targets.check(temp.config_dir, None, probe=lambda *a: False)
        finally:
            temp.close()
        self.assertEqual(code, 0)
        self.assertEqual(findings[0]["state"], "parked")

    def test_each_target_is_probed_once_across_vhosts(self):
        temp = Temp({
            "1.conf": ("a.innotel.us", ("192.168.1.56", 8080)),
            "2.conf": ("b.innotel.us", ("192.168.1.56", 8080)),
        })
        seen = []

        def probe(host, port, timeout=2.0):
            seen.append((host, port))
            return True

        try:
            code, _ = targets.check(temp.config_dir, None, probe=probe)
        finally:
            temp.close()
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("192.168.1.56", 8080)])

    def test_no_proxy_hosts_is_exit_two_not_success(self):
        with tempfile.TemporaryDirectory() as empty:
            code, findings = targets.check(Path(empty), None, probe=lambda *a: True)
        self.assertEqual(code, 2)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
