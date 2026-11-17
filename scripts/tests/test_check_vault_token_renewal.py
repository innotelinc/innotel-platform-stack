#!/usr/bin/env python3
"""Unit tests for check-vault-token-renewal.py — the lease, and what a refusal means.

The facts worth pinning are the ones that decide whether this is silent: a refusal must be
a **failure** (the next thing that happens is `credential_age` exiting 2), a non-renewable
token must be a failure for a different reason an operator can act on, and a short lease is
a warning rather than a pass. The transport is injected, so none of this needs a vault.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-vault-token-renewal.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_vault_token_renewal", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

ADDR = "http://192.168.1.71:8200"
TOKEN = "s.fake"


def transport(answer):
    """A transport that records the call and answers with `answer` (a dict or an exception)."""
    calls = []

    def send(url, token, payload, timeout):
        calls.append((url, token, payload))
        if isinstance(answer, Exception):
            raise answer
        return answer

    send.calls = calls  # type: ignore[attr-defined]
    return send


def codes(result) -> list[tuple[str, str]]:
    return [(f.code, f.level) for f in result.findings]


class Renewal(unittest.TestCase):
    def test_a_renewal_is_a_note_with_the_lease_it_got(self):
        result = check.audit(ADDR, TOKEN,
                             renewer=lambda a, t, timeout: {"lease_duration": 720 * 3600,
                                                            "renewable": True})
        self.assertEqual(codes(result), [("renewed", "note")])
        self.assertTrue(result.ok)

    def test_the_endpoint_is_renew_self_and_nothing_else(self):
        send = transport({"auth": {"lease_duration": 3600, "renewable": True}})
        check.renew(ADDR, TOKEN, transport=send)
        self.assertEqual(send.calls[0][0], "http://192.168.1.71:8200/v1/auth/token/renew-self")
        self.assertEqual(send.calls[0][1], TOKEN)

    def test_a_refusal_is_a_failure_not_silence(self):
        # The whole point: if the vault will not renew, credential_age dies 30 days later
        # with no other symptom, so this has to be red now.
        def refused(a, t, timeout):
            raise check.VaultRefused("HTTP 403 from renew-self")

        result = check.audit(ADDR, TOKEN, renewer=refused)
        self.assertEqual(codes(result), [("renew-refused", "fail")])
        self.assertFalse(result.ok)
        self.assertIn("credential_age", result.failures[0].message)

    def test_a_non_renewable_token_is_a_failure_with_the_fix_in_it(self):
        result = check.audit(ADDR, TOKEN,
                             renewer=lambda a, t, timeout: {"lease_duration": 3600,
                                                            "renewable": False})
        self.assertEqual(codes(result), [("not-renewable", "fail")])
        self.assertIn("VAULT_TOKEN", result.failures[0].message)

    def test_a_short_lease_warns_and_still_passes(self):
        result = check.audit(ADDR, TOKEN, min_days=7,
                             renewer=lambda a, t, timeout: {"lease_duration": 2 * 86_400,
                                                            "renewable": True})
        self.assertEqual(codes(result), [("short-lease", "warn")])
        self.assertTrue(result.ok)

    def test_no_credential_is_exit_two(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = check.main(["--addr", "", "--token", ""])
        self.assertEqual(code, 2)
        self.assertIn("token to renew", err.getvalue())

    def test_a_clean_renewal_is_exit_zero(self):
        original = check.audit
        check.audit = lambda *a, **k: _ok_audit()
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = check.main(["--addr", ADDR, "--token", TOKEN])
        finally:
            check.audit = original
        self.assertEqual(code, 0)


def _ok_audit():
    result = check.Audit()
    result.findings.append(check.Finding("renewed", "note", "renewed for 30.0 days"))
    return result


if __name__ == "__main__":
    unittest.main()
