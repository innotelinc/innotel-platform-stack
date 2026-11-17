#!/usr/bin/env python3
"""Unit tests for check-credential-age.py — the age of a vault secret, never its value.

The facts under test are the ones a hand check gets wrong: `updated_time` is when the
current version was written (a rotated secret is *new*), `created_time` is when the path
first existed (a rotated secret would read old forever), and a missing token is exit 2
rather than a clean run. The secret's data is never fetched, which one test pins by
recording every path the reader is asked for.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-credential-age.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_credential_age", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

NOW = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)


def metadata(days_ago: float, *, version: int = 3) -> dict:
    written = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"updated_time": written, "current_version": version,
            "versions": {str(version): {"created_time": written}}}


def reader_for(ages: dict[str, float]):
    """A reader that answers each path with metadata `ages[path]` days old."""
    asked: list[tuple[str, str, str]] = []

    def reader(addr, token, path):
        asked.append((addr, token, path))
        return metadata(ages[path])

    reader.asked = asked  # type: ignore[attr-defined]
    return reader


def codes(result) -> list[tuple[str, str]]:
    return [(finding.code, finding.level) for finding in result.findings]


class Age(unittest.TestCase):
    def test_the_updated_time_is_what_is_measured(self):
        self.assertAlmostEqual(check.age_days(check.last_written(metadata(3)), NOW), 3, places=3)

    def test_created_time_is_not_used_when_updated_time_exists(self):
        # A secret rotated yesterday has old versions behind it; using `created_time` would
        # call it old forever and the check would be noise within one rotation cycle.
        document = {"updated_time": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "created_time": (NOW - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "current_version": 4, "versions": {"4": {"created_time": "2020-01-01T00:00:00Z"}}}
        self.assertAlmostEqual(check.age_days(check.last_written(document), NOW), 1, places=3)

    def test_the_version_table_is_the_fallback(self):
        document = {"current_version": 2,
                    "versions": {"2": {"created_time": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}}}
        self.assertAlmostEqual(check.age_days(check.last_written(document), NOW), 5, places=3)

    def test_vault_nanosecond_timestamps_are_read_not_refused(self):
        # Vault writes NINE fractional digits. `fromisoformat` takes at most six, so this
        # raised ValueError and the check reported a vault it could not read — the first
        # live run (2026-11-17) exited 2 on a perfectly valid `updated_time`.
        document = {"updated_time": "2026-11-16T21:00:00.779670597+00:00",
                    "current_version": 1, "versions": {}}
        self.assertAlmostEqual(check.age_days(check.last_written(document), NOW), 0.25, places=2)

    def test_a_current_secret_is_a_note(self):
        result = check.audit(["cerulean/mailjet"], 90, addr="http://v", token="t",
                             reader=reader_for({"cerulean/mailjet": 2.0}), now=NOW)
        self.assertTrue(result.ok)
        self.assertEqual(codes(result), [("current", "note")])
        self.assertIn("inside the 90d window", result.findings[0].message)

    def test_an_overdue_secret_is_a_warning_naming_its_runbook(self):
        result = check.audit(["cerulean/mailjet"], 90, addr="http://v", token="t",
                             reader=reader_for({"cerulean/mailjet": 120.0}), now=NOW)
        self.assertTrue(result.ok)  # a warning does not fail the estate
        self.assertEqual(codes(result), [("overdue", "warn")])
        self.assertIn("docs/mailjet-key-rotation.md", result.findings[0].message)
        # The reason it is a warning and not a failure is the message's job to say.
        self.assertIn("still works", result.findings[0].message)

    def test_a_missing_secret_is_a_failure(self):
        def reader(addr, token, path):
            raise check.NoSuchSecret(path)

        result = check.audit(["cerulean/gone"], 90, addr="http://v", token="t",
                             reader=reader, now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), [("missing-secret", "fail")])

    def test_the_window_is_sixty_days_and_the_unit_says_so(self):
        # Two numbers decide when an operator is told: the default here and the flag the
        # timer passes. A test that only pinned one would let them part company, and the
        # check would report one window while the estate ran another.
        self.assertEqual(check.DEFAULT_MAX_AGE_DAYS, 60.0)
        unit = (SCRIPTS.parent / "systemd" / "estate-checks.service").read_text()
        line = next(l for l in unit.splitlines()
                    if l.startswith("ExecStart=") and "check-credential-age.py" in l)
        self.assertIn(f"--max-age-days {check.DEFAULT_MAX_AGE_DAYS:.0f}", line)

    def test_a_secret_past_sixty_days_is_already_warning(self):
        result = check.audit(["cerulean/mailjet"], check.DEFAULT_MAX_AGE_DAYS,
                             addr="http://v", token="t",
                             reader=reader_for({"cerulean/mailjet": 61.0}), now=NOW)
        self.assertEqual(codes(result), [("overdue", "warn")])

    def test_only_metadata_is_read(self):
        reader = reader_for({"cerulean/mailjet": 1.0})
        check.audit(["cerulean/mailjet"], 90, addr="http://v:8200", token="tok",
                    reader=reader, now=NOW)
        self.assertEqual(reader.asked, [("http://v:8200", "tok", "cerulean/mailjet")])

    def test_the_metadata_url_is_the_kv_v2_one(self):
        self.assertEqual(check.metadata_url("http://v:8200/", "cerulean/mailjet"),
                         "http://v:8200/v1/cerulean/metadata/mailjet")

    def test_a_listing_is_narrowed_to_secrets(self):
        # A real listing mixes directories (`acme/`) with secrets (`mailjet`); a directory has
        # no age of its own, and counting it would report a wolf that is not there.
        self.assertEqual(
            check.top_level(["acme/", "mailjet", "pki/", "technitium"], "cerulean"),
            ["cerulean/mailjet", "cerulean/technitium"])


class Main(unittest.TestCase):
    def _run(self, *args, reader=None):
        original = check.read_metadata
        if reader is not None:
            check.read_metadata = reader
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(list(args))
            return code, out.getvalue(), err.getvalue()
        finally:
            check.read_metadata = original

    def test_no_vault_credential_is_exit_two(self):
        code, _, err = self._run("--addr", "", "--token", "")
        self.assertEqual(code, 2)
        self.assertIn("VAULT_ADDR/VAULT_TOKEN are not set", err)

    def test_an_unreadable_vault_is_exit_two(self):
        def reader(addr, token, path, timeout=10.0):
            raise check.VaultUnreadable("HTTP 403")

        code, _, err = self._run("--addr", "http://v", "--token", "t", reader=reader)
        self.assertEqual(code, 2)
        self.assertIn("could not be read", err)

    def test_all_ages_every_listed_secret(self):
        original_list, original_read = check.list_secrets, check.read_metadata
        check.list_secrets = lambda addr, token, mount="cerulean", timeout=10.0: [
            "cerulean/alpha", "cerulean/beta"]
        check.read_metadata = lambda addr, token, path, timeout=10.0: metadata(1)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                code = check.main(["--all", "--addr", "http://v", "--token", "t"])
        finally:
            check.list_secrets, check.read_metadata = original_list, original_read
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("cerulean/alpha", text)
        self.assertIn("cerulean/beta", text)

    def test_an_unreadable_listing_is_exit_two(self):
        def listing(*args, **kwargs):
            raise check.VaultUnreadable("HTTP 403")

        original = check.list_secrets
        check.list_secrets = listing
        try:
            _, _, err = self._run("--all", "--addr", "http://v", "--token", "t")
        finally:
            check.list_secrets = original
        self.assertIn("listing could not be read", err)

    def test_a_warning_still_exits_zero(self):
        code, out, _ = self._run(
            "--addr", "http://v", "--token", "t",
            reader=lambda addr, token, path, timeout=10.0: metadata(200))
        self.assertEqual(code, 0)
        self.assertIn("WARN", out)

    def test_the_textfile_carries_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential-age.prom"
            code, _, _ = self._run(
                "--addr", "http://v", "--token", "t", "--prom", str(path),
                reader=lambda addr, token, path_, timeout=10.0: metadata(1))
            self.assertEqual(code, 0)
            body = path.read_text()
        self.assertIn('innotel_estate_check_last_status{check="credential_age"} 1', body)
        self.assertIn('innotel_estate_check_warnings{check="credential_age"} 0', body)
        # A note is published as its own series, because a count alone cannot be acted on.
        self.assertIn('code="current"', body)
        self.assertIn('detail="', body)

    def test_an_unrunnable_check_publishes_zero_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential-age.prom"
            code, _, _ = self._run("--addr", "", "--token", "", "--prom", str(path))
            self.assertEqual(code, 2)
            body = path.read_text()
        self.assertIn('innotel_estate_check_last_status{check="credential_age"} 0', body)


if __name__ == "__main__":
    unittest.main()
