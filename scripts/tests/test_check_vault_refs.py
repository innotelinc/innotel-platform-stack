#!/usr/bin/env python3
"""Unit tests for check-vault-refs.py — the resolvable-reference scanner.

These cover the rules, not the estate: each case builds a throwaway tree, a fake
Vault, and asserts what the scanner said. The cases are the failures this check
was written after, so a regression in any of them is a regression in the check's
reason to exist (see the module docstring): a reference naming a sibling
product's path, a path that holds some keys but not the referenced one, a
producer that never moved the key out of `.env`, and a leftover `infisical://`
reference in a store that is retired, not a fallback.

Vault is faked at the module boundary, so the suite runs anywhere — including CI,
where there is no Vault and no estate above the checkout. The module under test
has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

CHECK = Path(__file__).resolve().parents[1] / "check-vault-refs.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_vault_refs", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before executing: the module uses dataclasses, and `@dataclass`
    # resolves its own module through sys.modules while the class body is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


chk = _load()


class FakeVault:
    """The estate's Vault, faked: `reads[(mount, path)]` → (status, data)."""

    def __init__(self, reads=None, addr="http://vault:8200"):
        self.reads = reads or {}
        self.reachable = True
        self.addr = addr
        self.asked = []

    def read(self, mount, path, token):
        self.asked.append((mount, path, token))
        status, data = self.reads.get((mount, path), ("forbidden", {}))
        if status == "unreachable":
            self.reachable = False
        return status, data


class ScanCase(unittest.TestCase):
    """Build a tree, scan it, hand back the findings."""

    def build(self, files: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def scan(self, files: dict[str, str], vault=None):
        root = self.build(files)
        return chk.check_root(root, vault or FakeVault()), root

    def violations(self, files, vault=None):
        return [f for f in self.scan(files, vault)[0] if f.is_violation]


class ReferenceGrammar(ScanCase):
    def test_documented_grammar_parses(self):
        self.assertEqual(
            chk.parse_ref("vault://cerulean/zeus#SESSION_SECRET"),
            ("cerulean", "zeus", "SESSION_SECRET"),
        )

    def test_nested_path_keeps_the_product_as_the_first_segment(self):
        self.assertEqual(
            chk.parse_ref("vault://cerulean/magnate/stripe#STRIPE_SECRET_KEY"),
            ("cerulean", "magnate/stripe", "STRIPE_SECRET_KEY"),
        )

    def test_a_reference_without_a_key_is_not_parsed(self):
        # The `#key` fragment is required: a consumer that needs one value cannot
        # guess which of a secret's keys it wanted.
        self.assertIsNone(chk.parse_ref("vault://cerulean/zeus"))

    def test_a_reference_without_a_key_fails_the_line(self):
        findings = self.scan({"1-primary/x/.env": "TOKEN=vault://cerulean/x\n"})[0]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, "missing")
        self.assertIn("#key", findings[0].detail)

    def test_comments_and_plain_values_are_not_references(self):
        files = {
            "1-primary/x/.env": (
                "# TOKEN=vault://cerulean/x#TOKEN\n"
                "\n"
                "OTHER=plain\n"
                "NOTE=see vault://cerulean/x#TOKEN in the docs\n"
            )
        }
        self.assertEqual(self.scan(files)[0], [])


class ProductTokens(ScanCase):
    """Rule 2 — the product's own token has to be on the host that holds the file."""

    def test_missing_token_fails_before_any_call_to_vault(self):
        vault = FakeVault({("cerulean", "x"): ("ok", {"TOKEN": "value"})})
        findings = self.scan({"1-primary/x/.env": "TOKEN=vault://cerulean/x#TOKEN\n"}, vault)[0]
        self.assertEqual([f.status for f in findings], ["no-token"])
        self.assertEqual(vault.asked, [])

    def test_the_token_is_found_under_the_repo_it_belongs_to(self):
        files = {"1-primary/x/data/vault/token/x.token": "s.token\n"}
        tokens = chk.token_files(self.build(files))
        self.assertEqual(sorted(tokens), ["x"])

    def test_an_unreadable_token_is_reported_not_crashed(self):
        root = self.build({"1-primary/x/.env": "TOKEN=vault://cerulean/x#TOKEN\n"})
        (root / "1-primary/x/data/vault/token").mkdir(parents=True)
        token = root / "1-primary/x/data/vault/token/x.token"
        token.write_text("s.token")
        token.chmod(0o000)
        try:
            token.read_text()
            self.skipTest("running as a user that can read mode-000 files")
        except OSError:
            pass
        findings = chk.check_root(root, FakeVault())
        self.assertEqual([f.status for f in findings], ["no-token"])


class Resolution(ScanCase):
    """Rules 3 and 4 — the token reads the path, and the key is there and non-empty."""

    def files(self):
        return {
            "1-primary/x/.env": "TOKEN=vault://cerulean/x#TOKEN\n",
            "1-primary/x/data/vault/token/x.token": "s.x-token\n",
        }

    def test_a_resolved_reference_is_ok_and_reports_a_length(self):
        vault = FakeVault({("cerulean", "x"): ("ok", {"TOKEN": "a-value"})})
        findings = self.scan(self.files(), vault)[0]
        self.assertEqual([f.status for f in findings], ["ok"])
        self.assertEqual(findings[0].resolved_chars, len("a-value"))
        self.assertEqual(vault.asked[0][2], "s.x-token")

    def test_a_403_is_the_scoping_working_not_a_vault_outage(self):
        # The token is for another product's path: Vault answers 403, and that is
        # the failure this rule names rather than losing to "unreachable".
        vault = FakeVault({("cerulean", "x"): ("forbidden", {})})
        findings = self.scan(self.files(), vault)[0]
        self.assertEqual([f.status for f in findings], ["forbidden"])
        self.assertIn("HTTP 403", findings[0].render())

    def test_a_path_that_holds_other_keys_is_missing_not_ok(self):
        vault = FakeVault({("cerulean", "x"): ("ok", {"SOMETHING_ELSE": "v"})})
        findings = self.scan(self.files(), vault)[0]
        self.assertEqual([f.status for f in findings], ["missing"])
        self.assertIn("SOMETHING_ELSE", findings[0].detail)

    def test_an_empty_value_is_missing(self):
        vault = FakeVault({("cerulean", "x"): ("ok", {"TOKEN": "   "})})
        self.assertEqual([f.status for f in self.scan(self.files(), vault)[0]], ["missing"])

    def test_the_path_asked_for_is_the_reference_path_not_the_mount(self):
        vault = FakeVault({("cerulean", "x"): ("ok", {"TOKEN": "v"})})
        self.scan(self.files(), vault)
        self.assertEqual(vault.asked[0][:2], ("cerulean", "x"))


class LegacyStore(ScanCase):
    """Rule 5 — Infisical is retired, so a leftover reference fails."""

    def test_an_infisical_reference_fails(self):
        findings = self.scan({"1-primary/x/.env": "TOKEN=infisical://prod/x/TOKEN\n"})[0]
        self.assertEqual([f.status for f in findings], ["denied"])
        self.assertIn("retired", findings[0].detail)


class UnreachableVault(ScanCase):
    """An unreachable store is reported and skipped — unless `--strict`."""

    def files(self):
        return {
            "1-primary/x/.env": "TOKEN=vault://cerulean/x#TOKEN\n",
            "1-primary/x/data/vault/token/x.token": "s.x-token\n",
        }

    def run_main(self, files, strict):
        root = self.build(files)
        vault = FakeVault({("cerulean", "x"): ("unreachable", {})})
        argv = ["--root", str(root)] + (["--strict"] if strict else [])
        out = io.StringIO()
        with mock.patch.object(chk, "vault_from_env", return_value=vault):
            with redirect_stdout(out):
                code = chk.main(argv)
        return code, out.getvalue()

    def test_skipped_by_default(self):
        code, out = self.run_main(self.files(), strict=False)
        self.assertEqual(code, 0)
        self.assertIn("could not be checked", out)

    def test_fails_under_strict(self):
        code, _ = self.run_main(self.files(), strict=True)
        self.assertEqual(code, 1)


class Cli(ScanCase):
    def test_json_reports_the_violations(self):
        root = self.build({"1-primary/x/.env": "TOKEN=vault://cerulean/x#TOKEN\n"})
        out = io.StringIO()
        with redirect_stdout(out):
            code = chk.main(["--root", str(root), "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["violations"], 1)
        self.assertEqual(payload["findings"][0]["status"], "no-token")

    def test_a_clean_tree_exits_zero(self):
        root = self.build({"1-primary/x/.env": "TOKEN=plain\n"})
        out = io.StringIO()
        with redirect_stdout(out):
            code = chk.main(["--root", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("no vault:// references", out.getvalue())

    def test_a_standalone_checkout_skips_cleanly(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = chk.main(["--root", "/nonexistent-estate"])
        self.assertEqual(code, 0)
        self.assertIn("stands alone", out.getvalue())

    def test_values_are_never_printed(self):
        root = self.build(
            {
                "1-primary/x/.env": "TOKEN=vault://cerulean/x#TOKEN\n",
                "1-primary/x/data/vault/token/x.token": "s.x-token\n",
            }
        )
        vault = FakeVault({("cerulean", "x"): ("ok", {"TOKEN": "super-secret-value"})})
        out = io.StringIO()
        with mock.patch.object(chk, "vault_from_env", return_value=vault):
            with redirect_stdout(out):
                chk.main(["--root", str(root)])
        self.assertNotIn("super-secret-value", out.getvalue())
        self.assertIn(f"({len('super-secret-value')} chars)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
