#!/usr/bin/env python3
"""Unit tests for env-credential-audit.py — the URL-safe-credential audit.

The audit exists because a `#` in a credential ends a URL at a fragment, and the
symptom appears nowhere near the cause (see the module docstring). These cases
pin the two sides of that: a raw secret carrying a `#` is still reported, and a
`vault://` *reference* — whose `#` is the reference's own key separator and whose
value is fetched at boot — is not. Reading a reference as a credential was a
false positive on every product whose secrets moved to Vault, which is exactly
the posture the estate is supposed to be in, so the audit was failing the thing
it was built to encourage.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[1] / "env-credential-audit.py"


def _load():
    spec = importlib.util.spec_from_file_location("env_credential_audit", AUDIT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load()


class References(unittest.TestCase):
    """A reference is not the value it names, and its `#` is not a fragment."""

    def test_a_vault_reference_is_not_a_credential(self):
        env = {"STRIPE_SECRET_KEY": "vault://cerulean/magnate/stripe#STRIPE_SECRET_KEY"}
        self.assertEqual(audit.problems(env), [])

    def test_a_quoted_reference_is_read_the_way_dotenv_would(self):
        # `parse()` strips the quotes first, so `problems()` sees the bare
        # reference — and the quoted form must reduce to the same thing.
        self.assertEqual(
            audit.unquote("'vault://cerulean/zeus#SESSION_SECRET'"),
            "vault://cerulean/zeus#SESSION_SECRET",
        )
        env = {"SESSION_SECRET": "vault://cerulean/zeus#SESSION_SECRET"}
        self.assertEqual(audit.problems(env), [])

    def test_an_infisical_reference_is_left_to_the_other_check(self):
        # check-vault-refs.py reports a retired store; this audit must not be a
        # second thing shouting about the same line.
        self.assertEqual(audit.problems({"TOKEN": "infisical://prod/x/TOKEN"}), [])


class RawValues(unittest.TestCase):
    """The rule still fires on the values it was written for."""

    def test_a_plain_secret_with_a_hash_is_still_flagged(self):
        found = audit.problems({"SESSION_SECRET": "abc#def"})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "WARN")
        self.assertEqual(found[0][1], "SESSION_SECRET")

    def test_a_url_holding_a_fragment_is_still_flagged(self):
        found = audit.problems({"CALLBACK_URL": "https://h/path?x=1#frag"})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "ERROR")

    def test_a_plain_unquoted_value_ends_at_a_trailing_comment(self):
        self.assertEqual(audit.unquote("a-token # rotate me"), "a-token")


if __name__ == "__main__":
    unittest.main()
