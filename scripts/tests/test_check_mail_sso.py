#!/usr/bin/env python3
"""Unit tests for check-mail-sso.py — the three legs and the signature that hid for days.

The 2026-11-06 report was "SSO login isn't working" while the sign-in itself completed: the
webmail exchanged the code for a token and then every request 500d because Stalwart had
cached a failed directory initialisation. These tests pin that exact signature (500 for an
impossible bearer) as a failure, pin 401 as healthy, and keep the quieter legs — a renamed
callback, a rejected redirect URI — from drifting into a pass.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-mail-sso.py"
CALLBACK = "https://mail.innotel.us/en/auth/callback"


def _load():
    spec = importlib.util.spec_from_file_location("check_mail_sso", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mail_sso = _load()


def healthy(**overrides) -> dict:
    facts = {
        "callback": CALLBACK,
        "sso_start": {"status": 200, "body": "{}", "authorize_url": "https://auth.example/authorize/?x=1"},
        "authorize_request": {"client_id": "stalwart-webui", "redirect_uri": CALLBACK},
        "authorize": {"status": 302, "location": "/if/flow/default-authentication-flow/?x=1", "body": ""},
        "idp_discovery": {"status": 200},
        "jmap_anonymous": {"status": 200, "body": "{}"},
        "jmap_bearer": {"status": 401, "body": "{}"},
    }
    facts.update(overrides)
    return facts


def codes(result) -> list[str]:
    return [finding.code for finding in result.findings]


class HealthyCase(unittest.TestCase):
    def test_a_working_chain_reports_nothing(self):
        result = mail_sso.audit(healthy())
        self.assertEqual(result.findings, [])
        self.assertTrue(result.ok)


class DirectoryCase(unittest.TestCase):
    def test_an_impossible_bearer_answered_500_is_the_cached_directory_failure(self):
        result = mail_sso.audit(healthy(**{"jmap_bearer": {"status": 500, "body": "Internal Server Error"}}))
        self.assertIn("directory_unavailable", codes(result))
        self.assertFalse(result.ok)
        finding = next(f for f in result.findings if f.code == "directory_unavailable")
        self.assertIn("Restart stalwart", finding.message)

    def test_401_is_the_healthy_refusal(self):
        self.assertNotIn("directory_unavailable", codes(mail_sso.audit(healthy())))

    def test_a_bearer_that_is_accepted_is_a_failure_too(self):
        result = mail_sso.audit(healthy(**{"jmap_bearer": {"status": 200, "body": "{}"}}))
        self.assertIn("auth_backend_unexpected", codes(result))

    def test_anonymous_503_is_an_unhealthy_server(self):
        result = mail_sso.audit(healthy(**{"jmap_anonymous": {"status": 503, "body": "down"}}))
        self.assertIn("stalwart_unhealthy", codes(result))


class StartCase(unittest.TestCase):
    def test_a_start_that_500s_fails(self):
        result = mail_sso.audit(healthy(**{"sso_start": {"status": 500, "body": "SESSION_SECRET is required for SSO"}}))
        self.assertIn("sso_start_failed", codes(result))
        self.assertFalse(result.ok)

    def test_a_renamed_callback_is_named(self):
        result = mail_sso.audit(healthy(**{
            "authorize_request": {"client_id": "stalwart-webui",
                                  "redirect_uri": "https://old.example/en/auth/callback"},
        }))
        self.assertIn("callback_mismatch", codes(result))

    def test_a_missing_client_id_fails(self):
        result = mail_sso.audit(healthy(**{
            "authorize_request": {"client_id": "", "redirect_uri": CALLBACK},
        }))
        self.assertIn("sso_start_incomplete", codes(result))


class AuthorizeCase(unittest.TestCase):
    def test_a_rejected_redirect_uri_is_named(self):
        result = mail_sso.audit(healthy(**{
            "authorize": {"status": 400, "location": "", "body": "Redirect URI Error"},
        }))
        self.assertIn("authorize_rejected", codes(result))

    def test_an_unreachable_idp_is_a_failure(self):
        result = mail_sso.audit(healthy(**{
            "authorize": {"status": 0, "location": "", "body": "timed out"},
            "idp_discovery": {"status": 0},
        }))
        self.assertIn("idp_unreachable", codes(result))
        self.assertIn("idp_discovery_failed", codes(result))

    def test_an_unexpected_authorize_answer_is_a_failure(self):
        result = mail_sso.audit(healthy(**{
            "authorize": {"status": 200, "location": "", "body": "hello"},
        }))
        self.assertIn("authorize_unexpected", codes(result))


if __name__ == "__main__":
    unittest.main()
