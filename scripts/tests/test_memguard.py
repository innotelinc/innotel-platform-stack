"""Tests for the ord-atheniq memory guard.

The guard only earns its place if it fires before swap is gone and never picks
a database as the thing to cancel, so those two properties are what is pinned
here.
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "memguard.py")


def load():
    spec = importlib.util.spec_from_file_location("memguard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Levels(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def s(self, mem, swap, psi=None):
        return {"mem_avail_pct": mem, "swap_free_pct": swap, "psi_full60": psi}

    def test_healthy_is_ok(self):
        self.assertEqual(self.m.level_of(self.s(40, 60)), "OK")

    def test_low_memory_alone_warns(self):
        # MemAvailable in single digits with swap still healthy is a warning,
        # not an action: a build can be reclaiming caches and still finish.
        self.assertEqual(self.m.level_of(self.s(11.9, 60)), "WARN")

    def test_low_swap_alone_warns(self):
        self.assertEqual(self.m.level_of(self.s(40, 14.9)), "WARN")

    def test_a_build_using_ram_is_not_an_emergency(self):
        # Page cache is reclaimable: a webpack stage legitimately takes RAM. If
        # this counted as critical the guard would cancel every build it saw.
        self.assertEqual(self.m.level_of(self.s(6.9, 60)), "WARN")

    def test_swap_exhaustion_is_critical_on_its_own(self):
        # 2026-10-27: swap hit zero and the host stopped answering. That is the
        # shape to cancel a build for, whatever RAM says.
        self.assertEqual(self.m.level_of(self.s(40, 5.9)), "CRIT")

    def test_low_ram_with_low_swap_is_critical(self):
        self.assertEqual(self.m.level_of(self.s(3.9, 10)), "CRIT")

    def test_never_acts_on_a_single_spike(self):
        self.assertGreaterEqual(self.m.CRIT_SAMPLES_TO_ACT, 2)

    def test_warn_thresholds_are_above_critical(self):
        self.assertGreater(self.m.WARN_MEM_PCT, self.m.CRIT_MEM_PCT)
        self.assertGreater(self.m.WARN_SWAP_PCT, self.m.CRIT_SWAP_PCT)


class Protection(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_databases_are_never_candidates(self):
        for name in ("mysqld", "mariadbd", "redis-server", "mongod",
                     "meilisearch", "dockerd", "containerd"):
            self.assertTrue(self.m.PROTECTED.match(name), name)

    def test_the_guard_itself_is_protected(self):
        self.assertTrue(self.m.PROTECTED.match("memguard"))

    def test_only_a_build_is_ever_matched(self):
        patterns = self.m.BUILD_PATTERNS
        joined = " ".join(patterns)
        self.assertIn("openedx-mfe", joined)
        # A generic `docker build` of something else must not match.
        self.assertFalse(any("docker build" == p for p in patterns))


if __name__ == "__main__":
    unittest.main()
