#!/usr/bin/env python3
"""Unit tests for check-mount-stacks.py — the stack rule, the stale rule, and the probe.

The failure this guards is one that produced no error anywhere: 32 769 stacked NFS mounts
under `/srv/onyx-pool` on 2026-10-29, each one mounted on top of an unreadable stale handle,
while every `df` on the host queued behind the probes and the control plane went dark. So
the two things that have to hold are that a second entry for one target is a finding on its
own (it is the early warning; nothing else in the estate reports it), and that the probe is
bounded — an unbounded one joins the queue it is meant to detect.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-mount-stacks.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_mount_stacks", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stacks = _load()


class ParseCase(unittest.TestCase):
    def test_a_row_reads_as_a_mount(self):
        mount = stacks.parse_walk("i3/distro|192.168.1.60:/mnt/onyx/main-pool|/srv/onyx-pool/main-pool|nfs|ok|3")[0]
        self.assertEqual(mount.scope, "i3/distro")
        self.assertEqual(mount.source, "192.168.1.60:/mnt/onyx/main-pool")
        self.assertEqual(mount.target, "/srv/onyx-pool/main-pool")
        self.assertEqual(mount.state, "ok")
        self.assertEqual(mount.elapsed_ms, 3)

    def test_a_stale_row_is_stale(self):
        mount = stacks.parse_walk("host|10.0.0.1:/x|/mnt/x|nfs4|stale|5001")[0]
        self.assertEqual(mount.state, "stale")
        self.assertEqual(mount.elapsed_ms, 5001)

    def test_a_malformed_row_is_skipped_not_guessed(self):
        self.assertEqual(stacks.parse_walk("nonsense\n\nhost|a|b\n"), [])

    def test_a_bad_elapsed_is_not_a_reason_to_drop_the_mount(self):
        mount = stacks.parse_walk("host|10.0.0.1:/x|/mnt/x|nfs|ok|soon")[0]
        self.assertEqual(mount.elapsed_ms, 0)


class AuditCase(unittest.TestCase):
    def _mount(self, target="/srv/onyx-pool/main-pool", scope="i3/distro", state="ok",
               elapsed=4, source="192.168.1.60:/mnt/onyx/main-pool"):
        return stacks.Mount(scope=scope, source=source, target=target, state=state, elapsed_ms=elapsed)

    def test_one_mount_that_answers_is_not_a_finding(self):
        result = stacks.audit([self._mount()])
        self.assertEqual(result.findings, [])
        self.assertTrue(result.ok)

    def test_the_same_target_twice_is_a_stack_and_a_failure(self):
        # The whole point: the second entry is the storm, even while both answer.
        result = stacks.audit([self._mount(), self._mount()])
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].code, "stacked")
        self.assertIn("mounted 2 times", result.failures[0].message)

    def test_a_deep_stack_is_reported_once_with_its_count(self):
        result = stacks.audit([self._mount() for _ in range(9)])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("mounted 9 times", result.failures[0].message)

    def test_two_different_targets_are_not_a_stack(self):
        result = stacks.audit([
            self._mount(target="/srv/onyx-pool/main-pool"),
            self._mount(target="/srv/onyx-pool/sdc", source="192.168.1.60:/mnt/onyx/main-pool"),
        ])
        self.assertTrue(result.ok, "sdc and main-pool are the same export on two targets, not a stack")

    def test_the_same_target_on_a_different_scope_is_not_a_stack(self):
        result = stacks.audit([self._mount(scope="i3/distro"), self._mount(scope="host")])
        self.assertTrue(result.ok)

    def test_a_stale_mount_is_a_failure(self):
        result = stacks.audit([self._mount(state="stale", elapsed=5001)])
        self.assertFalse(result.ok)
        self.assertEqual(result.failures[0].code, "stale")

    def test_a_slow_mount_warns_rather_than_fails(self):
        # Rule 3: hundreds of milliseconds is trouble ahead, not an outage now. The
        # estate's shares answer in single digits, so this is already abnormal.
        result = stacks.audit([self._mount(elapsed=stacks.SLOW_MS + 1)])
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.warnings], ["slow"])

    def test_the_slow_line_is_inclusive_like_every_other_line_here(self):
        # `host_disk` fails at 90 % exactly, not just above it; the same reading of "the
        # line" keeps this predictable at 2000 ms.
        self.assertEqual([f.code for f in stacks.audit([self._mount(elapsed=stacks.SLOW_MS)]).findings], ["slow"])
        self.assertEqual(stacks.audit([self._mount(elapsed=stacks.SLOW_MS - 1)]).findings, [])


class SeriesCase(unittest.TestCase):
    def test_the_entries_gauge_counts_every_scope(self):
        text = stacks.mount_series([
            stacks.Mount(scope="i3/distro", source="a", target="/mnt/a"),
            stacks.Mount(scope="i3/distro", source="a", target="/mnt/a"),
            stacks.Mount(scope="host", source="b", target="/mnt/b"),
        ])
        self.assertIn('innotel_estate_check_mount_entries{check="mount_stack",scope="i3/distro"} 2', text)
        self.assertIn('innotel_estate_check_mount_entries{check="mount_stack",scope="host"} 1', text)

    def test_the_probe_gauge_carries_the_measured_time(self):
        text = stacks.mount_series([stacks.Mount(scope="host", source="a", target="/mnt/a", elapsed_ms=7)])
        self.assertIn('innotel_estate_check_mount_probe_ms{check="mount_stack",scope="host",target="/mnt/a"} 7', text)


class WalkCase(unittest.TestCase):
    def test_the_probe_is_bounded(self):
        # `stat -f` blocks on a stale handle; without a timeout the check becomes the
        # thing it is looking for.
        self.assertIn("timeout -k", stacks.PROBE)
        self.assertIn("$PROBE_TIMEOUT", stacks.PROBE)

    def test_containers_are_probed_inside_the_container(self):
        # The bug this check shipped with for one run: a container's mountpoints are not
        # paths on the host, so a host-side `stat -f` read every one of them as stale.
        self.assertIn("incus exec", stacks.WALK)
        self.assertIn("sh -c __PROBE__", stacks.WALK)

    def test_the_walk_survives_being_quoted(self):
        # The probe travels as one `sh -c` argument; a single quote inside it would end
        # the argument early and the check would silently read nothing.
        self.assertNotIn("'", stacks.PROBE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
