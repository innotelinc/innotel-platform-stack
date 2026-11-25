#!/usr/bin/env python3
"""The deploy door's certificate runway, and the line a healthy renewal never crosses."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / "check-deploy-door.py"
NAME = "deploy.innotel.us"


def _load():
    spec = importlib.util.spec_from_file_location("check_deploy_door", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CertificateRunway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chk = _load()

    def _findings(self, days: float) -> list[str]:
        door = self.chk.Door(
            NAME, status=200, ms=100.0, body_has_marker=True,
            cert_days=days, cert_sans=[NAME],
        )
        return [repr(finding) for finding in self.chk.audit([door], []).findings]

    def test_a_certificate_inside_three_weeks_warns(self):
        findings = self._findings(20.0)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("cert_soon", findings[0])

    def test_a_certificate_that_renewed_on_schedule_is_silent(self):
        # Let's Encrypt renews with about thirty days left, so anything at or above that
        # is healthy — warning there would fire on every certificate on every pass.
        for days in (30.0, 45.0, 89.0):
            self.assertEqual(self._findings(days), [], days)

    def test_the_lines_below_the_warning_still_escalate(self):
        critical = self._findings(2.0)
        self.assertEqual(len(critical), 1, critical)
        self.assertIn("cert_critical", critical[0])
        expired = self._findings(-1.0)
        self.assertEqual(len(expired), 1, expired)
        self.assertIn("cert_expired", expired[0])

    def test_the_warning_line_sits_below_the_renewal_point(self):
        self.assertLess(self.chk.WARN_DAYS, 30)
        self.assertLess(self.chk.FAIL_DAYS, self.chk.WARN_DAYS)


class ForwardAuthGate(unittest.TestCase):
    """The estate's own gate is an open door; a stranger's login page is not.

    `deploy.innotel.us` was put behind the `auth.innotel.us` forward-auth gate on
    2026-11-25, so the page a browser reaches is provider 35's login rather than the
    panel. The marker cannot appear until somebody signs in, and treating the gate as a
    wrong page would make the check red for the door it was built to watch.
    """

    @classmethod
    def setUpClass(cls):
        cls.chk = _load()

    def _door(self, final_url: str, marker: bool = False) -> "object":
        return self.chk.Door(
            NAME, status=200, ms=100.0, body_has_marker=marker,
            final_url=final_url, cert_days=60.0, cert_sans=[NAME],
        )

    def test_the_estate_login_page_is_the_gate_and_not_a_failure(self):
        door = self._door("https://auth.innotel.us/if/flow/default-authentication-flow/")
        result = self.chk.audit([door], [])
        self.assertTrue(result.ok, result.findings)
        self.assertEqual([finding.code for finding in result.notes], ["gated"])

    def test_a_login_page_from_another_host_is_still_a_wrong_page(self):
        door = self._door("https://auth.example.test/login")
        result = self.chk.audit([door], [])
        self.assertFalse(result.ok)
        self.assertEqual([finding.code for finding in result.failures], ["wrong_page"])

    def test_the_panel_itself_passes_without_the_gate(self):
        door = self._door("https://deploy.innotel.us/login", marker=True)
        self.assertEqual(self.chk.audit([door], []).findings, [])


if __name__ == "__main__":
    unittest.main()
