#!/usr/bin/env python3
"""Unit tests for check-address-latency.py — the addresses probed, and what counts as dark.

Two failures this check exists to tell apart, and both are locked here:

  * an address that is **dialled but does not answer** (`no_reply`) — the outage one layer
    below a renumber, and the reason the probe set is the *address table*, not just the
    hosts; and
  * an address that answers **slowly** (`slow`, a warning) — the `i4` WiFi power-save
    regression, which was felt rather than measured and needs a number to be visible.

The probe set is read from `check-container-addresses.py` so it cannot drift, so a test
here also guards that reading (a container in the table must be probed).
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-address-latency.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_address_latency", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lat = _load()

PING_7MS = "64 bytes from 192.168.1.51: icmp_seq=1 ttl=64 time=7.24 ms\n"
PING_400MS = "64 bytes from 192.168.1.30: icmp_seq=1 ttl=63 time=412 ms\n"
PING_TIMEOUT = "PING 192.168.1.30: 1 packets transmitted, 0 received, 100% packet loss, time 0ms\n"


def _reading(name, address, kind, rtts, where="", role="", attempted=None):
    return lat.Reading(
        target=lat.Target(name=name, address=address, kind=kind, where=where, role=role),
        rtts=rtts,
        attempted=len(rtts) if attempted is None else attempted,
    )


class ParseCase(unittest.TestCase):
    def test_a_reply_is_read_as_a_number(self):
        self.assertEqual(lat.parse_rtts(PING_7MS), [7.24])

    def test_a_timeout_reads_as_no_sample_not_as_zero(self):
        # Zero would read as the fastest address on the estate; no sample is the honest
        # answer and is what the audit turns into a failure.
        self.assertEqual(lat.parse_rtts(PING_TIMEOUT), [])


class TargetCase(unittest.TestCase):
    def test_every_host_and_every_table_address_is_probed(self):
        expected, _ = lat._address_table()
        probed = {t.address for t in lat.targets()}
        for target in lat.HOSTS.values():
            self.assertIn(target.split("@", 1)[-1], probed, "every estate host must be probed")
        for host, containers in expected.items():
            for name, address in containers.items():
                self.assertIn(address, probed, f"{name} on {host} is dialled and must be probed")

    def test_target_keys_are_unique(self):
        keys = [t.key for t in lat.targets()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_service_is_labelled_with_the_host_it_lives_on(self):
        table = {"i2": {"atlas": "192.168.1.90"}}
        targets = lat.targets(hosts={}, expected=table, roles={})
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].label, "atlas on i2")
        self.assertEqual(targets[0].address, "192.168.1.90")


class ProbeCase(unittest.TestCase):
    def test_the_probe_is_one_ping_per_sample_after_an_idle_gap(self):
        command = lat.probe_command("192.168.1.51", samples=3, idle=0.5)
        self.assertIn("192.168.1.51", command)
        self.assertIn("sleep 0.5", command)
        self.assertEqual(command.count("ping -c 1"), 1, "the ping is inside the loop, one per sample")


class AuditCase(unittest.TestCase):
    def test_a_healthy_estate_is_not_a_finding(self):
        readings = [_reading("i1", "192.168.1.51", "host", [7.0, 7.2, 7.4]),
                    _reading("atlas", "192.168.1.90", "service", [7.0], where="i2")]
        result = lat.audit(readings)
        self.assertEqual(result.findings, [])
        self.assertTrue(result.ok)

    def test_a_dark_service_address_is_a_failure(self):
        result = lat.audit([_reading("capstone", "192.168.1.30", "service", [], where="i2", role="capstone / Zeus telephony")])
        self.assertEqual([f.code for f in result.findings], ["no_reply"])
        self.assertFalse(result.ok)
        # It must name the address and what is dialled there, or the failure says nothing.
        self.assertIn("192.168.1.30", result.findings[0].message)
        self.assertIn("Zeus telephony", result.findings[0].message)

    def test_scores_of_milliseconds_is_a_warning_naming_the_power_save(self):
        result = lat.audit([_reading("i1", "192.168.1.51", "host", [400.0, 380.0, 420.0])])
        self.assertEqual(result.findings[0].code, "slow")
        self.assertEqual(result.findings[0].level, "warn")
        self.assertIn("wifi-powersave-off", result.findings[0].message)

    def test_an_unusable_address_is_a_failure(self):
        result = lat.audit([_reading("i3", "192.168.1.53", "host", [1200.0, 1100.0, 1300.0])])
        self.assertEqual([f.code for f in result.findings], ["unusable"])
        self.assertFalse(result.ok)

    def test_partial_loss_is_a_note_not_a_warning(self):
        # A lost first burst on a WiFi hop is normal; warning here would page every run.
        result = lat.audit([_reading("i3", "192.168.1.53", "host", [5.0, 6.0], attempted=3)])
        self.assertTrue(result.ok)
        self.assertEqual([f.level for f in result.findings], ["note"])
        self.assertEqual([f.code for f in result.findings], ["lossy"])

    def test_one_bad_address_does_not_hide_behind_a_good_one(self):
        readings = [
            _reading("i1", "192.168.1.51", "host", [7.0]),
            _reading("atlas", "192.168.1.90", "service", [400.0], where="i2"),
            _reading("capstone", "192.168.1.30", "service", [], where="i2"),
        ]
        result = lat.audit(readings)
        self.assertEqual(sorted(f.code for f in result.findings), ["no_reply", "slow"])


class ConfirmCase(unittest.TestCase):
    def test_a_silent_first_round_is_re_probed_before_it_is_called_dark(self):
        # The flapping guard: round one answers nothing, round two answers. Not dark.
        rounds = {"n": 0}

        def runner(command: str) -> str:
            rounds["n"] += 1
            return PING_TIMEOUT if rounds["n"] == 1 else PING_7MS

        readings = lat.measure([lat.Target(name="i1", address="192.168.1.51", kind="host")], runner=runner, samples=1, idle=0)
        self.assertEqual(readings[0].rtts, [7.24])
        self.assertEqual(readings[0].attempted, 2, "both rounds were attempted")

    def test_silent_across_both_rounds_is_dark(self):
        readings = lat.measure(
            [lat.Target(name="i1", address="192.168.1.51", kind="host")],
            runner=lambda command: PING_TIMEOUT,
            samples=1,
            idle=0,
        )
        self.assertEqual(readings[0].rtts, [])
        self.assertEqual(readings[0].attempted, 2)


class SeriesCase(unittest.TestCase):
    def test_the_reading_is_published_per_address(self):
        body = lat.latency_series([
            _reading("i1", "192.168.1.51", "host", [7.0, 8.0]),
            _reading("atlas", "192.168.1.90", "service", [6.0], where="i2"),
            _reading("vpn", "192.168.1.43", "service", [], where="i4"),
        ])
        self.assertIn('innotel_estate_check_address_latency_ms{check="address_latency",kind="host",name="i1",address="192.168.1.51"} 7.500', body)
        self.assertIn('kind="service",name="atlas",address="192.168.1.90"', body)
        # A dark address has no number to publish; it becomes a failure finding, not a zero.
        self.assertNotIn('name="vpn"', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
