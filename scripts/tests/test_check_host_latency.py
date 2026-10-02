#!/usr/bin/env python3
"""Unit tests for check-host-latency.py — the number, and what counts as slow.

The check exists because the `i4` WiFi power-save regression was *felt* rather than
measured (`docs/container-placement.md` §The i4 host): nothing in the estate could say
"everything just got 40× slower". So the two things worth locking here are the reading
(one ping per sample, after an idle gap — because consecutive pings train the radio and
would hide the regression) and the verdict (a healthy ~7 ms passes, hundreds of ms warns,
a dead address fails).
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-host-latency.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_host_latency", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lat = _load()

PING_7MS = "PING 192.168.1.51 (192.168.1.51) 56(84) bytes of data.\n" "64 bytes from 192.168.1.51: icmp_seq=1 ttl=64 time=7.24 ms\n"
PING_400MS = "64 bytes from 192.168.1.52: icmp_seq=1 ttl=63 time=412 ms\n"
PING_TIMEOUT = "PING 192.168.1.53 (192.168.1.53) 56(84) bytes of data.\n\n--- 192.168.1.53 ping statistics ---\n1 packets transmitted, 0 received, 100% packet loss, time 0ms\n"


class ParseCase(unittest.TestCase):
    def test_a_reply_is_read_as_a_number(self):
        self.assertEqual(lat.parse_rtts(PING_7MS), [7.24])

    def test_every_reply_is_read(self):
        self.assertEqual(lat.parse_rtts(PING_400MS + PING_400MS), [412.0, 412.0])

    def test_a_timeout_reads_as_no_sample_not_as_zero(self):
        # Zero would read as the fastest host on the estate; no sample is the honest answer
        # and is what the audit turns into a failure.
        self.assertEqual(lat.parse_rtts(PING_TIMEOUT), [])


class ProbeCase(unittest.TestCase):
    def test_the_probe_is_one_ping_per_sample_after_an_idle_gap(self):
        command = lat.probe_command("192.168.1.51", samples=3, idle=0.6)
        self.assertIn("192.168.1.51", command)
        self.assertIn("sleep 0.6", command)
        self.assertEqual(command.count("ping -c 1"), 1, "the ping is inside the loop, one per sample")

    def test_measure_pings_each_host_at_its_address(self):
        seen = []

        def runner(command: str) -> str:
            seen.append(command)
            return PING_7MS

        samples = lat.measure({"i1": "root@192.168.1.51", "i4": "root@192.168.1.54"}, runner=runner)
        self.assertEqual(set(samples), {"i1", "i4"})
        self.assertEqual(samples["i1"], [7.24])
        self.assertEqual(len(seen), 2)
        self.assertIn("192.168.1.51", seen[0])
        self.assertIn("192.168.1.54", seen[1])


class AuditCase(unittest.TestCase):
    def test_a_healthy_host_is_not_a_finding(self):
        result = lat.audit({"i1": [7.0, 7.2, 7.4]})
        self.assertEqual(result.findings, [])
        self.assertTrue(result.ok)

    def test_scores_of_milliseconds_is_a_warning_naming_the_power_save(self):
        result = lat.audit({"i1": [400.0, 380.0, 420.0]})
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].code, "slow")
        self.assertEqual(result.findings[0].level, "warn")
        self.assertIn("wifi-powersave-off", result.findings[0].message)

    def test_a_host_at_the_slow_threshold_is_a_warning(self):
        # The boundary is inclusive, so a host that reads exactly SLOW_MS is reported
        # rather than sitting one millisecond under the rule.
        result = lat.audit({"i1": [lat.SLOW_MS, lat.SLOW_MS, lat.SLOW_MS]})
        self.assertEqual([f.code for f in result.findings], ["slow"])

    def test_an_unusable_host_is_a_failure(self):
        result = lat.audit({"i1": [1200.0, 1100.0, 1300.0]})
        self.assertEqual([f.code for f in result.findings], ["unusable"])
        self.assertFalse(result.ok)

    def test_no_reply_is_a_failure_not_a_zero(self):
        result = lat.audit({"i1": []})
        self.assertEqual([f.code for f in result.findings], ["no_reply"])
        self.assertFalse(result.ok)

    def test_one_bad_host_does_not_hide_behind_a_good_one(self):
        result = lat.audit({"i1": [7.0], "i2": [400.0], "i3": [7.0], "i4": []})
        self.assertEqual(sorted(f.code for f in result.findings), ["no_reply", "slow"])


class SeriesCase(unittest.TestCase):
    def test_the_reading_is_published_per_host(self):
        body = lat.latency_series({"i1": [7.0, 8.0], "i2": []})
        self.assertIn('innotel_estate_check_host_latency_ms{check="host_latency",host="i1"} 7.500', body)
        # A host with no reply has no number to publish; it becomes a failure finding, not
        # a zero on the chart.
        self.assertNotIn('host="i2"', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
