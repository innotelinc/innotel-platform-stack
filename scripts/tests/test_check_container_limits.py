#!/usr/bin/env python3
"""Unit tests for check-container-limits.py — the CPU-cap and file-descriptor invariants.

The cases are the mistakes this check exists to prevent: a `limits.cpu` that is written
down but not in force, a verification that read the wrong file and called a correct
state broken, and a soft `nofile` that is in force and already spent (`cerulean-npm`
held 572 of 1024 and could not reload its config). Each case builds the estate as data
and asserts what the check said.

The module under test has hyphens in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
CHECK = SCRIPTS / "check-container-limits.py"

sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("check_container_limits", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


chk = _load()

HOST_CPUS = {"i1": 4}


def observed(rows):
    """`rows` is [(name, declared, effective, state)] on the one host, i1."""
    return {"i1": [chk.Container(name=n, state=s, declared=d, effective=e) for n, d, e, s in rows]}


class CapCase(unittest.TestCase):
    def test_the_estate_as_it_should_be_has_no_failures(self):
        result = chk.audit(observed([("proxy", 2, 2, "RUNNING"), ("mail", 1, 1, "RUNNING")]), HOST_CPUS)
        self.assertTrue(result.ok, [f.message for f in result.failures])

    def test_a_cap_that_is_not_enforced_is_a_failure_that_names_both_numbers(self):
        result = chk.audit(observed([("proxy", 2, 1, "RUNNING")]), HOST_CPUS)
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["cap_not_enforced"])
        self.assertIn("limits.cpu=2", result.failures[0].message)
        self.assertIn("1 CPU", result.failures[0].message)

    def test_a_cap_above_the_host_is_a_warning_not_a_failure(self):
        # limits.cpu=8 on a 4-CPU host is meaningless; incus clamps it, and the written
        # number is the misconfiguration rather than the fence being broken.
        result = chk.audit(observed([("proxy", 8, 4, "RUNNING")]), HOST_CPUS)
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual([f.code for f in result.warnings], ["cap_above_host"])

    def test_a_container_with_no_declared_cap_is_not_a_finding(self):
        result = chk.audit(observed([("capstone", None, 4, "RUNNING")]), HOST_CPUS)
        self.assertEqual(result.findings, [])

    def test_a_stopped_container_is_a_note(self):
        result = chk.audit(observed([("acme", 1, None, "STOPPED")]), HOST_CPUS)
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.findings], ["stopped"])

    def test_a_virtual_machine_is_not_held_to_the_container_rule(self):
        # The estate's first VM (a Windows packaging build, 2026-10-26) failed this check on
        # every run: a VM's limits.cpu is the vCPU count qemu boots with, so there is no
        # `lxc.payload.<name>` cgroup to read and its absence proves nothing.
        result = chk.audit(
            {"i1": [chk.Container(name="ontrak-winpack-build", state="RUNNING", declared=2,
                                  effective=None, kind="virtual-machine")]},
            {"i1": 2},
        )
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual([f.code for f in result.findings], ["vm_cap_at_boot"])

    def test_a_virtual_machine_asking_for_more_cpus_than_the_host_has_still_warns(self):
        # The exemption is the cpuset rule, not arithmetic: a cap incus has to clamp is
        # still worth saying out loud.
        result = chk.audit(
            {"i1": [chk.Container(name="vm", state="RUNNING", declared=8, effective=None,
                                  kind="virtual-machine")]},
            {"i1": 4},
        )
        self.assertEqual([f.code for f in result.warnings], ["cap_above_host"])

    def test_a_running_container_with_no_readable_cpuset_is_a_failure(self):
        result = chk.audit(observed([("proxy", 2, None, "RUNNING")]), HOST_CPUS)
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["no_cpuset"])


def measured(rows, host="i2"):
    """`rows` is [(scope, container, process, soft, open)] on one host."""
    return {
        host: [
            chk.FdPressure(host=host, scope=scope, container=name, process=proc, soft=soft, open=held)
            for scope, name, proc, soft, held in rows
        ]
    }


class FdHeadroomCase(unittest.TestCase):
    """Rules 5-7: a file limit that is in force and nearly out."""

    def test_the_estate_as_it_is_has_no_failure(self):
        # Measured 2026-10-27: the estate's worst is Monarch's Whisparr at 431 of 1024
        # (42 %), and the edge after the fix holds 575 of 65536 (0.9 %).
        result = chk.audit(
            observed([("proxy", 2, 2, "RUNNING")]),
            HOST_CPUS,
            measured([("monarch", "whisparr", "Whisparr", 1024, 431),
                      ("proxy", "cerulean-npm", "nginx", 65536, 575)]),
        )
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual(result.findings, [])

    def test_the_edge_as_it_was_is_a_warning_that_names_both_numbers(self):
        # 2026-10-27, before the ulimit: nginx's master held 572 of 1024 — 56 % — and the
        # reload it could not do needed ~1050 of them. Nothing failed until the reload.
        result = chk.audit(
            observed([("proxy", 2, 2, "RUNNING")]),
            HOST_CPUS,
            measured([("proxy", "cerulean-npm", "nginx", 1024, 572)]),
        )
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual([f.code for f in result.warnings], ["fd_near_limit"])
        self.assertIn("572", result.warnings[0].message)
        self.assertIn("1024", result.warnings[0].message)
        self.assertIn("nginx", result.warnings[0].message)

    def test_half_the_budget_is_the_line_and_it_is_not_past_it(self):
        # Rule 6 is "more than half", so exactly half is quiet and one descriptor past it
        # is not. Locked here so a later edit cannot quietly move the line.
        self.assertEqual(chk.fd_findings(measured([("proxy", "a", "nginx", 1024, 512)])), [])
        self.assertEqual(
            [f.code for f in chk.fd_findings(measured([("proxy", "a", "nginx", 1024, 513)]))],
            ["fd_near_limit"],
        )

    def test_a_process_at_its_limit_is_a_failure(self):
        result = chk.audit(
            observed([("proxy", 2, 2, "RUNNING")]),
            HOST_CPUS,
            measured([("proxy", "a", "nginx", 1024, 1024)]),
        )
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["fd_exhausted"])
        self.assertIn("of 1024 open files", result.failures[0].message)

    def test_the_worst_container_on_a_host_is_reported_first(self):
        # The journal is read top-down, and the point of the rule is the container that
        # is closest to failing.
        codes = chk.fd_findings(measured([("proxy", "quiet", "nginx", 1024, 520),
                                          ("proxy", "loud", "nginx", 1024, 900)]))
        self.assertEqual([f.code for f in codes], ["fd_near_limit", "fd_near_limit"])
        self.assertIn("loud", codes[0].message)

    def test_a_host_with_no_docker_container_is_a_note(self):
        # Four hosts have docker somewhere, so a host reporting nothing is the probe
        # having stopped working — not a host that happens to run nothing.
        result = chk.audit(observed([("proxy", 2, 2, "RUNNING")]), HOST_CPUS, {"i1": []})
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.findings], ["fd_unread"])

    def test_a_cap_finding_and_an_fd_finding_are_reported_together(self):
        # One check publishes one failures count, so both families have to land in it.
        result = chk.audit(
            observed([("proxy", 2, 1, "RUNNING")]),
            HOST_CPUS,
            measured([("proxy", "a", "nginx", 1024, 1024)]),
        )
        self.assertEqual(sorted(f.code for f in result.findings),
                         ["cap_not_enforced", "fd_exhausted"])


class ProbeCase(unittest.TestCase):
    """The probe's lines as data — the half that can silently read nothing."""

    def test_a_line_becomes_the_scope_container_limit_and_count(self):
        parsed = chk.parse_fd("i2", "proxy|cerulean-npm|65536|575|nginx")
        self.assertEqual(len(parsed), 1)
        self.assertEqual((parsed[0].scope, parsed[0].container, parsed[0].process), ("proxy", "cerulean-npm", "nginx"))
        self.assertEqual((parsed[0].soft, parsed[0].open), (65536, 575))
        self.assertEqual(parsed[0].share, 575 / 65536)

    def test_the_process_name_is_last_so_a_separator_in_it_cannot_break_the_line(self):
        # `/proc/<pid>/comm` is 16 bytes of whatever the program called itself, and it is
        # written last so the fields before it stay at fixed positions.
        parsed = chk.parse_fd("i4", "host|app|1024|9|weird|name")
        self.assertEqual(parsed[0].process, "weird|name")
        self.assertEqual((parsed[0].soft, parsed[0].open), (1024, 9))

    def test_an_unlimited_limit_has_no_number_to_compare_against(self):
        # The kernel reports RLIM_INFINITY as `unlimited`; nothing can exhaust it, and
        # inventing a number for it would invent a finding.
        self.assertEqual(chk.parse_fd("i1", "monarch|app|unlimited|900|nginx"), [])

    def test_a_truncated_or_foreign_line_is_dropped_rather_than_guessed(self):
        self.assertEqual(chk.parse_fd("i1", "docker ps: command not found\nproxy|app\n"), [])
        self.assertEqual(chk.parse_fd("i1", ""), [])

    def test_a_container_reading_no_process_is_not_invented(self):
        # The probe only prints a container once it found a process to measure, so an
        # empty read is an empty list rather than a container at 0 of 0.
        self.assertEqual(chk.parse_fd("i3", "\n\n"), [])


class CpusetCase(unittest.TestCase):
    """The count that the whole check turns on — a range is N CPUs, not one string."""

    def test_a_range_and_a_bare_cpu_count_correctly(self):
        from estate_check import count_cpus

        self.assertEqual(count_cpus("0-1"), 2)
        self.assertEqual(count_cpus("3"), 1)
        self.assertEqual(count_cpus("0,2-3"), 3)
        self.assertEqual(count_cpus(""), 0)
        self.assertEqual(count_cpus(" 0-1 "), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
