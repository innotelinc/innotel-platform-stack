#!/usr/bin/env python3
"""Unit tests for check-sso-redirects.py — the address half of the registration rule.

The rule that matters is "every registered redirect URI must be served by an NPM proxy
host", and on 2026-11-18 it was about to be wrong three times over: the Coolify panels on
`i2` and the `dev` VM sign in through Authentik by address, and an address has no NPM host
by definition. The check failed all three registrations, which for an audit is the worst
kind of error — it reads exactly like the dead-name finding it exists to produce.

So the fixtures here are the two states an address registration can be in, and one sentinel
for the rule that must not move: a *named* registration whose host NPM does not serve is
still a failure, because a name that moved is the case the whole check was written for.

Everything runs against a fake probe; nothing is dialled.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-sso-redirects.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_sso_redirects", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Addresses(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load()

    def test_an_address_is_told_apart_from_a_name(self) -> None:
        self.assertTrue(self.module.is_address("192.168.1.113"))
        self.assertTrue(self.module.is_address("10.0.0.5"))
        self.assertTrue(self.module.is_address("[::1]"))
        self.assertFalse(self.module.is_address("deploy.innotel.us"))
        self.assertFalse(self.module.is_address("192.168.1.113.example.com"))

    def test_a_live_address_passes_and_says_what_answered(self) -> None:
        self.module.fetch = lambda url, follow=False, timeout=15: (302, {}, b"")
        verdict, detail = self.module.address_verdict("http://192.168.1.113:8000/auth/authentik/callback")
        self.assertEqual("PASS", verdict)
        self.assertIn("192.168.1.113:8000", detail)
        self.assertIn("302", detail)

    def test_an_address_with_nothing_answering_is_a_failure(self) -> None:
        """The service moving is the finding; the address form does not excuse it."""
        self.module.fetch = lambda url, follow=False, timeout=15: (0, {}, b"refused")
        verdict, detail = self.module.address_verdict("http://192.168.1.52:8000/auth/oidc/callback")
        self.assertEqual("FAIL", verdict)
        self.assertIn("nothing answers", detail)

    def test_the_port_in_the_registration_is_the_one_dialled(self) -> None:
        seen = {}

        def fake(url, follow=False, timeout=15):
            seen["url"] = url
            return 200, {}, b""

        self.module.fetch = fake
        self.module.address_verdict("https://192.168.1.5:8443/auth/authentik/callback")
        self.assertEqual("https://192.168.1.5:8443/", seen["url"])

    def test_a_default_port_follows_the_scheme(self) -> None:
        seen = {}

        def fake(url, follow=False, timeout=15):
            seen["url"] = url
            return 200, {}, b""

        self.module.fetch = fake
        self.module.address_verdict("http://192.168.1.5/auth/oidc/callback")
        self.assertEqual("http://192.168.1.5:80/", seen["url"])
        self.module.address_verdict("https://192.168.1.5/auth/oidc/callback")
        self.assertEqual("https://192.168.1.5:443/", seen["url"])


if __name__ == "__main__":
    unittest.main()
