#!/usr/bin/env python3
"""Unit tests for check-edge-liveness.py — the one check that runs off the edge.

The property that matters is not clever: it is that a probe which does **not** answer is
reported, and that a transient miss does not cry wolf (a watcher on a WiFi uplink that
pages on one dropped packet gets turned off, which is worse than not having it). Both are
locked here, along with the textfile a Prometheus off the edge would read.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-edge-liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_edge_liveness", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


watch = _load()


class _Sock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answering(addr, timeout=None):
    return _Sock()


def _refused(addr, timeout=None):
    raise OSError("connection refused")


class _Clock:
    """A monotonic clock that advances 50 ms per reading, so a probe reads as 50 ms."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        value = self.t
        self.t += 0.05
        return value


class ParseCase(unittest.TestCase):
    def test_a_host_port_parses(self):
        endpoint = watch.parse_endpoint("192.168.1.71:443")
        self.assertEqual((endpoint.host, endpoint.port), ("192.168.1.71", 443))
        self.assertEqual(endpoint.spec, "192.168.1.71:443")

    def test_a_bare_host_is_rejected(self):
        with self.assertRaises(ValueError):
            watch.parse_endpoint("192.168.1.71")

    def test_a_non_numeric_port_is_rejected(self):
        with self.assertRaises(ValueError):
            watch.parse_endpoint("192.168.1.71:https")


class ProbeCase(unittest.TestCase):
    def test_an_answering_endpoint_reports_a_latency(self):
        ms = watch.tcp_probe(watch.Endpoint("192.168.1.71", 443), connect=_answering, clock=_Clock())
        self.assertAlmostEqual(ms, 50.0, places=3)

    def test_a_refused_endpoint_reports_no_latency(self):
        self.assertIsNone(watch.tcp_probe(watch.Endpoint("192.168.1.71", 443), connect=_refused, clock=_Clock()))

    def test_one_transient_miss_is_not_a_failure(self):
        # The first attempt refused, the second answered: up. This is the flapping guard.
        calls = {"n": 0}

        def flaky(addr, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("dropped")
            return _Sock()

        results = watch.probe([watch.Endpoint("192.168.1.71", 443)], attempts=2, connect=flaky, clock=_Clock())
        self.assertIsNotNone(results[0].ms)
        self.assertTrue(watch.all_up(results))

    def test_every_attempt_refused_is_a_failure(self):
        results = watch.probe([watch.Endpoint("192.168.1.71", 443)], attempts=2, connect=_refused, clock=_Clock())
        self.assertIsNone(results[0].ms)
        self.assertFalse(watch.all_up(results))


class ReportCase(unittest.TestCase):
    def test_a_dark_endpoint_is_named(self):
        results = [
            watch.Result(endpoint=watch.Endpoint("192.168.1.71", 443), ms=None),
            watch.Result(endpoint=watch.Endpoint("192.168.1.71", 80), ms=4.0),
        ]
        text = watch.report(results)
        self.assertIn("192.168.1.71:443", text)
        self.assertIn("NO ANSWER", text)
        self.assertIn("dark on: 192.168.1.71:443", text)


class TextfileCase(unittest.TestCase):
    def test_a_healthy_probe_advances_last_success(self):
        body = watch.textfile([watch.Result(endpoint=watch.Endpoint("192.168.1.71", 443), ms=4.0)], now=1000.0)
        self.assertIn(f"{watch.METRIC}_reachable 1", body)
        self.assertIn(f"{watch.METRIC}_last_success_timestamp 1000", body)
        self.assertIn('endpoint_ms{endpoint="192.168.1.71:443"} 4.000', body)

    def test_a_dark_edge_does_not_advance_last_success(self):
        # The whole point: a Prometheus off the edge can alert on the *stale* success, even
        # though the edge's own Prometheus and thresholds are gone.
        body = watch.textfile([watch.Result(endpoint=watch.Endpoint("192.168.1.71", 443), ms=None)], now=1000.0)
        self.assertIn(f"{watch.METRIC}_reachable 0", body)
        self.assertIn(f"{watch.METRIC}_last_run_timestamp 1000", body)
        self.assertNotIn("last_success_timestamp", body)

    def test_the_textfile_is_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "edge-liveness.prom")
            watch.write_textfile(path, "x\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "x\n")


class MailCase(unittest.TestCase):
    def test_the_message_carries_from_to_subject_and_body(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                sent["addr"] = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def send_message(self, message):
                sent["to"] = message["To"]
                sent["from"] = message["From"]
                sent["subject"] = message["Subject"]
                sent["body"] = message.get_content()

        watch.send_mail(
            "192.168.1.15", 25, "edge-watch@innotel.us", "admin@innotel.us",
            "[critical] the edge did not answer", "the edge is dark\n", smtp=FakeSMTP,
        )
        self.assertEqual(sent["addr"], ("192.168.1.15", 25))
        self.assertEqual(sent["to"], "admin@innotel.us")
        self.assertEqual(sent["from"], "edge-watch@innotel.us")
        self.assertIn("[critical]", sent["subject"])
        self.assertIn("the edge is dark", sent["body"])

    def test_a_dark_edge_is_mailed_and_a_healthy_one_is_not(self):
        from unittest import mock

        dark = [watch.Result(endpoint=watch.Endpoint("192.168.1.71", 443), ms=None)]
        healthy = [watch.Result(endpoint=watch.Endpoint("192.168.1.71", 443), ms=4.0)]
        argv = ["--endpoints", "192.168.1.71:443", "--mail-to", "admin@innotel.us", "--smtp-host", "192.168.1.15"]

        with mock.patch.object(watch, "probe", return_value=dark), mock.patch.object(watch, "send_mail") as mailer:
            self.assertEqual(watch.main(argv), 1)
            mailer.assert_called_once()
            self.assertEqual(mailer.call_args.args[3], "admin@innotel.us")

        with mock.patch.object(watch, "probe", return_value=healthy), mock.patch.object(watch, "send_mail") as mailer:
            self.assertEqual(watch.main(argv), 0)
            mailer.assert_not_called()

    def test_an_empty_recipient_sends_nothing(self):
        from unittest import mock

        dark = [watch.Result(endpoint=watch.Endpoint("192.168.1.71", 443), ms=None)]
        with mock.patch.object(watch, "probe", return_value=dark), mock.patch.object(watch, "send_mail") as mailer:
            self.assertEqual(watch.main(["--endpoints", "192.168.1.71:443"]), 1)
            mailer.assert_not_called()


class NotifyCase(unittest.TestCase):
    def test_the_message_reaches_the_notify_command(self):
        seen = {}

        def runner(command, shell, input, text, timeout, check):
            seen["command"] = command
            seen["input"] = input
            return None

        watch.notify("sendmail -t", "the edge is dark\n", runner=runner)
        self.assertEqual(seen["command"], "sendmail -t")
        self.assertIn("the edge is dark", seen["input"])

    def test_a_failing_channel_does_not_hide_the_finding(self):
        def runner(*args, **kwargs):
            raise OSError("no such channel")

        watch.notify("nope", "the edge is dark\n", runner=runner)  # must not raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
