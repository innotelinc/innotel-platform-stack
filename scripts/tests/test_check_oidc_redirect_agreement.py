#!/usr/bin/env python3
"""The comparison rules of check-oidc-redirect-agreement.py, without a vault or a provider.

Run with `python3 scripts/tests/test_check_oidc_redirect_agreement.py` — the estate has no
pytest, and the checks are stdlib only for that reason.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "check-oidc-redirect-agreement.py"

spec = importlib.util.spec_from_file_location("check_oidc_redirect_agreement", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

UI = "https://secrets.cerulean.innotel.us/ui/vault/auth/oidc/oidc/callback"
UI_HTTP = "http://secrets.cerulean.innotel.us/ui/vault/auth/oidc/oidc/callback"
CLI = "http://localhost:8250/oidc/callback"


def provider(*urls, mode="strict"):
    return [{"matching_mode": mode, "url": url} for url in urls]


class CompareRules(unittest.TestCase):
    def codes(self, audit):
        return [finding.code for finding in audit.findings]

    def test_identical_lists_agree(self):
        audit = module.compare([UI, CLI], provider(UI, CLI))
        self.assertEqual(self.codes(audit), ["agreed"])
        self.assertTrue(audit.ok)

    def test_role_uri_the_provider_lacks_fails(self):
        """Vault would build an auth_url the provider refuses at authorize time."""
        audit = module.compare([UI, UI_HTTP], provider(UI))
        self.assertEqual(self.codes(audit), ["missing_at_provider"])
        self.assertIn("Redirect URI Error", audit.findings[0].message)
        self.assertFalse(audit.ok)

    def test_provider_uri_the_role_lacks_fails(self):
        """The exact 2026-11-20 shape: Authentik returns, Vault refuses, UI says missing auth_url."""
        audit = module.compare([UI], provider(UI, UI_HTTP))
        self.assertEqual(self.codes(audit), ["missing_at_role"])
        self.assertIn("Missing auth_url", audit.findings[0].message)
        self.assertFalse(audit.ok)

    def test_order_does_not_matter(self):
        audit = module.compare([CLI, UI], provider(UI, CLI))
        self.assertEqual(self.codes(audit), ["agreed"])

    def test_both_directions_are_reported_together(self):
        audit = module.compare([UI], provider(UI_HTTP))
        self.assertEqual(sorted(self.codes(audit)),
                         ["missing_at_provider", "missing_at_role"])

    def test_repeated_role_uri_warns_but_does_not_fail(self):
        audit = module.compare([UI, UI], provider(UI))
        self.assertEqual(self.codes(audit), ["role_duplicate"])
        self.assertTrue(audit.ok)
        self.assertEqual(len(audit.warnings), 1)

    def test_empty_role_against_nothing_is_not_silent(self):
        """Two empty lists agree — but a role with no list is refused by the reader, and an
        empty comparison still reports itself rather than printing nothing."""
        audit = module.compare([], provider())
        self.assertEqual(self.codes(audit), ["agreed"])
        self.assertIn("0 redirect URI", audit.findings[0].message)

    def test_matching_mode_is_carried_through(self):
        audit = module.compare([UI], provider(UI, mode="regex"))
        self.assertEqual(self.codes(audit), ["agreed"])


class ReaderRules(unittest.TestCase):
    def test_role_without_a_list_is_unreadable_not_empty(self):
        """A mount default in force must never compare as 'both empty, all good'."""
        original = module._get
        module._get = lambda *a, **k: {"data": {"bound_audiences": ["vault"]}}
        try:
            with self.assertRaises(module.Unreadable) as caught:
                module.read_role_uris("http://127.0.0.1:8200", "tok", "auth/oidc/role/operator")
        finally:
            module._get = original
        self.assertIn("default_redirect_uri", str(caught.exception))

    def test_unknown_client_id_is_unreadable(self):
        original = module._get
        module._get = lambda *a, **k: {"results": [{"client_id": "other", "redirect_uris": []}]}
        try:
            with self.assertRaises(module.Unreadable) as caught:
                module.read_provider_uris("https://auth.example", "tok", "vault")
        finally:
            module._get = original
        self.assertIn("client_id vault", str(caught.exception))

    def test_two_providers_with_one_client_id_is_unreadable(self):
        row = {"client_id": "vault", "redirect_uris": []}
        original = module._get
        module._get = lambda *a, **k: {"results": [row, dict(row)]}
        try:
            with self.assertRaises(module.Unreadable):
                module.read_provider_uris("https://auth.example", "tok", "vault")
        finally:
            module._get = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
