#!/usr/bin/env python3
"""Unit tests for check-mail-relay.py — the four things that make outbound mail leave.

The failure this check exists to catch has no symptom on the server: a route that was
never created queues messages against `Network is unreachable` and still looks like a
working server with a quiet queue. So what is worth locking here is that each of the four
parts (strategy, smarthost, DKIM, queue) **fails the run on its own**, that a server that
cannot be read is exit 2 rather than a quiet pass, and that the run writes the textfile
the alerts read.

Two things beyond the four rules are locked deliberately. The check ran on the estate for
days with the recovery admin password as a literal default in a mode-0755 file and was
not in this repo at all (found 2026-10-09) — so `test_the_credential_is_never_a_default_
in_this_file` guards the shape of that regression, not just its value. And the textfile
must carry `last_success_timestamp` like every other check, because `EstateCheckStale`
keys on it: without it a check that fails forever is silent (this check published its
four metrics but not that one until it moved onto `estate_check.py`).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-mail-relay.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_mail_relay", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()

#: A server with the whole chain wired: a smarthost every non-local recipient routes to,
#: one domain with an active signature, and an empty queue.
WIRED = {
    "x:MtaOutboundStrategy/get": {"list": [{"route": {"else": "smarthost"}}]},
    "x:MtaRoute/get": {"list": [{"name": "smarthost", "address": "relay.example.net", "port": 587}]},
    "x:Domain/get": {"list": [{"id": "b", "name": "innotel.us"}]},
    "x:DkimSignature/get": {"list": [{"domainId": "b", "stage": "active"}]},
    "x:QueuedMessage/get": {"list": []},
}


def _fake_jmap(answers: dict):
    def jmap(method, params=None):
        if method not in answers:
            raise AssertionError(f"the check asked for an unexpected method: {method}")
        return answers[method]

    return jmap


def _audit(answers: dict, **patches):
    with mock.patch.object(probe, "jmap", _fake_jmap(answers)), \
         mock.patch.object(probe, "port_open", lambda host, port, timeout=8: True):
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(probe, name, value))
            return probe.audit()


class RuleCase(unittest.TestCase):
    def test_a_wired_chain_is_clean(self):
        result = _audit(dict(WIRED))
        self.assertTrue(result.ok)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.warnings, [])

    def test_a_non_smarthost_strategy_is_a_failure(self):
        answers = dict(WIRED, **{"x:MtaOutboundStrategy/get": {"list": [{"route": {"else": "direct"}}]}})
        result = _audit(answers)
        self.assertFalse(result.ok)
        self.assertIn("do not route to a smarthost", result.failures[0].message)

    def test_a_missing_smarthost_route_is_a_failure(self):
        answers = dict(WIRED, **{"x:MtaRoute/get": {"list": []}})
        result = _audit(answers)
        self.assertFalse(result.ok)
        self.assertEqual(result.failures[0].code, "smarthost-missing")

    def test_an_unreachable_smarthost_is_a_failure(self):
        # The queue fills up later; the failure belongs to the host that stopped answering.
        result = _audit(dict(WIRED), port_open=lambda host, port, timeout=8: False)
        self.assertFalse(result.ok)
        self.assertEqual(result.failures[0].code, "smarthost-unreachable")

    def test_a_smarthost_without_an_address_is_a_failure(self):
        answers = dict(WIRED, **{"x:MtaRoute/get": {"list": [{"name": "smarthost", "address": "", "port": None}]}})
        result = _audit(answers)
        self.assertFalse(result.ok)
        self.assertEqual(result.failures[0].code, "smarthost-incomplete")

    def test_a_domain_with_no_active_signature_is_a_failure(self):
        # A rotation stuck in `pending` sends mail unsigned, which DMARC rejects at the
        # far end — the failure is not on this server at all.
        answers = dict(WIRED, **{
            "x:Domain/get": {"list": [{"id": "b", "name": "innotel.us"},
                                      {"id": "c", "name": "signara.innotel.us"}]},
            "x:DkimSignature/get": {"list": [{"domainId": "b", "stage": "active"},
                                             {"domainId": "c", "stage": "pending"}]},
        })
        result = _audit(answers)
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["dkim-inactive"])
        self.assertIn("signara.innotel.us", result.failures[0].message)

    def test_a_backlog_is_a_failure(self):
        # Stalwart returns a queue of messages, each with a recipient map; the finding is
        # about the depth once it is past the line the module names (`QUEUE_FAIL`).
        answers = dict(WIRED, **{"x:QueuedMessage/get": {"list": [
            {"recipients": {f"a{i}@x": {} for i in range(6)}},
            {"recipients": {f"b{i}@x": {} for i in range(5)}},
        ]}})
        result = _audit(answers)
        self.assertFalse(result.ok)
        self.assertEqual(result.failures[0].code, "queue-backlog")
        self.assertIn("11 queued recipient(s)", result.failures[0].message)

    def test_a_shallow_queue_is_only_a_warning(self):
        # A couple of queued recipients is a message in flight, not a broken chain.
        answers = dict(WIRED, **{"x:QueuedMessage/get": {"list": [{"recipients": {"a@x": {}, "b@x": {}}}]}})
        result = _audit(answers)
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.warnings], ["queue-some"])

    def test_an_empty_queue_is_a_note(self):
        result = _audit(dict(WIRED))
        self.assertEqual([f.code for f in result.findings], ["route-ok", "smarthost-present",
                                                             "smarthost-reachable", "dkim-active",
                                                             "queue-empty"])


class RunCase(unittest.TestCase):
    def _run(self, answers: dict, code_expected: int, failing_method: str | None = None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        prom = Path(directory.name) / "mail-relay.prom"

        def jmap(method, params=None):
            if failing_method and method == failing_method:
                raise OSError("connection refused")
            return answers[method]

        stdout = io.StringIO()
        with mock.patch.object(probe, "jmap", jmap), \
             mock.patch.object(probe, "port_open", lambda host, port, timeout=8: True), \
             contextlib.redirect_stdout(stdout):
            code = probe.main(["--prom", str(prom)])
        self.assertEqual(code, code_expected)
        return stdout.getvalue(), prom.read_text(encoding="utf-8")

    def test_a_clean_run_exits_zero_and_publishes_a_clean_run_time(self):
        out, body = self._run(dict(WIRED), 0)
        self.assertIn('innotel_estate_check_last_status{check="mail_relay"} 1', body)
        # EstateCheckStale keys on this; without it a check that never passes is silent.
        self.assertIn('innotel_estate_check_last_success_timestamp{check="mail_relay"} ', body)
        self.assertIn("0 failure(s), 0 warning(s)", out)

    def test_a_broken_chain_exits_one_and_publishes_the_failure(self):
        _, body = self._run(dict(WIRED, **{"x:MtaRoute/get": {"list": []}}), 1)
        self.assertIn('innotel_estate_check_last_status{check="mail_relay"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="mail_relay"} 1', body)

    def test_a_failure_is_printed_before_a_note(self):
        out, _ = self._run(dict(WIRED, **{"x:MtaRoute/get": {"list": []}}), 1)
        self.assertLess(out.index("FAIL"), out.index("NOTE"))

    def test_an_unreadable_server_is_exit_two_and_not_a_silent_pass(self):
        out, body = self._run(dict(WIRED), 2, failing_method="x:MtaOutboundStrategy/get")
        self.assertIn('innotel_estate_check_last_status{check="mail_relay"} 0', body)
        # Exit 2 publishes no `failures` series at all, which is what makes
        # EstateCheckCouldNotRun fire instead of EstateCheckFailing.
        self.assertNotIn("innotel_estate_check_failures", body)
        self.assertNotIn("could not be read", out)  # the message goes to stderr


class CredentialCase(unittest.TestCase):
    def test_the_credential_is_never_a_default_in_this_file(self):
        # The 2026-10-09 finding: this check ran on the edge with the recovery admin
        # password as a literal default. Locking the *shape* means the value cannot come
        # back with a different password in it.
        source = CHECK.read_text(encoding="utf-8")
        self.assertRegex(source, r'PASSWORD = os\.environ\.get\("STALWART_PASSWORD", ""\)')
        self.assertEqual(probe.PASSWORD, "")

    def test_the_server_is_read_with_the_credential_from_the_environment(self):
        seen = {}

        def jmap(method, params=None):
            seen["auth"] = probe.PASSWORD
            raise OSError("stop here")

        with mock.patch.object(probe, "jmap", jmap), mock.patch.object(probe, "PASSWORD", "from-env"):
            with contextlib.redirect_stderr(io.StringIO()):
                probe.main([])
        self.assertEqual(seen["auth"], "from-env")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
