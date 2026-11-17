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


if __name__ == "__main__":
    unittest.main()
