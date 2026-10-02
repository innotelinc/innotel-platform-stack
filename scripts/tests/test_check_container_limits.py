#!/usr/bin/env python3
"""Unit tests for check-container-limits.py — the CPU-cap invariant.

The cases are the mistake this check exists to prevent: a `limits.cpu` that is written
down but not in force, and a verification that read the wrong file and called a correct
state broken. Each case builds the estate as data and asserts what the check said.

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

    def test_a_running_container_with_no_readable_cpuset_is_a_failure(self):
        result = chk.audit(observed([("proxy", 2, None, "RUNNING")]), HOST_CPUS)
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["no_cpuset"])


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
