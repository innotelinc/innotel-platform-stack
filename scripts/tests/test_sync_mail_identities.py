#!/usr/bin/env python3
"""Unit tests for sync-mail-identities.py — the mirror's own exit codes.

The check in this repo judges the mirror by the code it exited with, and that contract is
only worth anything if the codes mean what the script's docstring says. On 2026-11-16 they
did not, in one direction: a `TimeoutError` out of a Stalwart JMAP body left `main()`
through an `except` that named `URLError`, so the process died with exit **1** — the code
reserved for "`--apply` was refused for some user" — while every mailbox already had its
identity. `check-mail-identities.py` could only read the number, so it asserted the refusal.

The fix is a retry of the *read* and an exit 2 when the retry is spent, and the tests here
pin three things about it: a timeout is tried again, two of them are "could not read"
(exit 2) rather than "refused" (exit 1), and a **write** is never repeated — a `POST` whose
reply timed out may already have created the user, and repeating it would turn one slow
answer into a duplicate-name rejection reported as a mailbox that failed to mirror.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
SYNC = SCRIPTS / "sync-mail-identities.py"

CREDENTIALS = {"STALWART_ADMIN": "admin:password", "AUTHENTIK_TOKEN": "token"}


def _load():
    spec = importlib.util.spec_from_file_location("sync_mail_identities", SYNC)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync = _load()


class Reads(unittest.TestCase):
    def test_a_read_that_times_out_is_tried_again(self):
        # The observed failure, in the shape it arrived: the response body times out, the
        # request was fine, so asking again is all that is needed.
        calls = []

        def first_timeout(url, auth, data, method):
            calls.append(method)
            if len(calls) == 1:
                raise TimeoutError("The read operation timed out")
            return {"ok": True}

        stderr = io.StringIO()
        with mock.patch.object(sync, "_read_json_once", first_timeout), \
                mock.patch.object(sync.time, "sleep", lambda _: None), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(sync.read_json("http://127.0.0.1:8080/jmap/", {}), {"ok": True})
        self.assertEqual(calls, [None, None])
        self.assertIn("retrying read", stderr.getvalue())

    def test_two_timeouts_are_an_api_that_could_not_be_read(self):
        with mock.patch.object(sync, "_read_json_once", mock.Mock(side_effect=TimeoutError("x"))), \
                mock.patch.object(sync.time, "sleep", lambda _: None), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(TimeoutError):
                sync.read_json("http://127.0.0.1:8080/jmap/", {})

    def test_a_write_is_never_repeated(self):
        # Idempotent reads only: a repeated POST could be a second user creation, and its
        # rejection would be published as a mailbox this script failed to mirror.
        once = mock.Mock(side_effect=TimeoutError("x"))
        with mock.patch.object(sync, "_read_json_once", once), \
                mock.patch.object(sync.time, "sleep", lambda _: None):
            with self.assertRaises(TimeoutError):
                sync.read_json("https://auth.example/api/v3/core/users/", {},
                               data={"username": "a@example.com"}, method="POST")
        self.assertEqual(once.call_count, 1)


class ExitCodes(unittest.TestCase):
    """What `check-mail-identities.py` reads, and therefore what has to stay true."""

    def test_a_timed_out_api_read_is_exit_two_not_one(self):
        with mock.patch.dict(os.environ, CREDENTIALS), \
                mock.patch.object(sync, "stalwart_accounts",
                                  side_effect=TimeoutError("The read operation timed out")), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(sync.main([]), 2)
        self.assertIn("could not read an estate API", stderr.getvalue())

    def test_a_refused_creation_is_exit_one_and_names_the_address(self):
        refusal = urllib.error.HTTPError(
            "https://auth.example/api/v3/core/users/", 400, "Bad Request", {},
            io.BytesIO(b'{"username": "already exists"}'))
        with mock.patch.dict(os.environ, CREDENTIALS), \
                mock.patch.object(sync, "stalwart_accounts",
                                  return_value=[("a@example.com", "A")]), \
                mock.patch.object(sync, "authentik_users", return_value={}), \
                mock.patch.object(sync, "create_user", side_effect=refusal), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(sync.main(["--apply"]), 1)
        self.assertIn("FAILED a@example.com", stderr.getvalue())

    def test_missing_credentials_are_exit_two(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sync.main([]), 2)

    def test_every_mailbox_mirrored_is_exit_zero(self):
        # The clean run the check reports as a note; the plan prints nothing to create.
        out = io.StringIO()
        with mock.patch.dict(os.environ, CREDENTIALS), \
                mock.patch.object(sync, "stalwart_accounts",
                                  return_value=[("a@example.com", "A")]), \
                mock.patch.object(sync, "authentik_users",
                                  return_value={"a@example.com": {"pk": "1"}}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(sync.main(["--apply"]), 0)
        self.assertIn("every mailbox already has an identity keyed by its address", out.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
