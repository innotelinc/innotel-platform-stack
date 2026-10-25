#!/usr/bin/env python3
"""Unit tests for reap-wedged-agents.py — what a session has to look like to be ended.

The reaper is the one job in this repo that kills something, so what it kills is the
thing worth testing. The 2026-10-24 incident set the shape: a session 30 h old with
5 h 40 m of CPU, wedged in `D`. The tests below hold the line either side of each
threshold, hold the process matcher to the agent's own binary (never its broker, never
the `node` wrapper that spawns it), and hold the reap to the two signals it promises.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

REAPER = SCRIPTS / "reap-wedged-agents.py"


def _load():
    spec = importlib.util.spec_from_file_location("reap_wedged_agents", REAPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reaper = _load()


def stat_line(pid: int = 100, comm: str = "freebuff", state: str = "S", ppid: int = 1, utime: int = 0, stime: int = 0) -> str:
    """One `/proc/<pid>/stat` line with the fields this parser reads, and filler elsewhere."""
    after_comm = [state, str(ppid)]
    # ppid is the only field this parser reads before utime; pad fields 5..13 (pgrp … cmajflt).
    after_comm += [str(value) for value in range(1, 10)]
    after_comm += [str(utime), str(stime)]
    after_comm += [str(value) for value in range(1, 8)]  # cutime … itrealvalue
    after_comm += ["0"]  # starttime
    return f"{pid} ({comm}) " + " ".join(after_comm) + " 0 0 0\n"


def session(*, pid: int = 100, ppid: int = 5000, state: str = "S", cpu: float = 0.0, age: float = 0.0) -> object:
    return reaper.Session(
        pid=pid,
        ppid=ppid,
        state=state,
        cpu_seconds=cpu,
        age_seconds=age,
        rss_bytes=1024,
        command="/root/.config/manicode/freebuff",
    )


class ParseStatCase(unittest.TestCase):
    def test_cpu_is_utime_plus_stime_in_seconds(self):
        parsed = reaper.parse_stat(stat_line(utime=600, stime=300))
        self.assertIsNotNone(parsed)
        ppid, state, cpu, _ = parsed
        self.assertEqual(ppid, 1)
        self.assertEqual(state, "S")
        self.assertAlmostEqual(cpu, 900 / reaper.HZ)

    def test_a_command_name_with_spaces_and_parens_does_not_shift_the_fields(self):
        # `(freebuff (v1) runner)` — the naive split would read the state from "v1)".
        parsed = reaper.parse_stat(stat_line(comm="freebuff (v1) runner", state="D", ppid=77))
        self.assertIsNotNone(parsed)
        ppid, state, _, _ = parsed
        self.assertEqual(state, "D")
        self.assertEqual(ppid, 77)

    def test_a_truncated_line_is_not_a_reading(self):
        self.assertIsNone(reaper.parse_stat("100 (freebuff) S 1 2\n"))


class MatcherCase(unittest.TestCase):
    def test_the_agent_binary_is_a_session(self):
        self.assertTrue(reaper.is_agent_process("/root/.config/manicode/freebuff", home="/root"))

    def test_the_terminal_command_broker_is_not_the_session(self):
        self.assertFalse(
            reaper.is_agent_process(
                "/root/.config/manicode/freebuff --terminal-command-broker", home="/root"
            )
        )

    def test_the_node_wrapper_is_not_the_session(self):
        # `node /usr/bin/freebuff` is the launcher; its own name is `freebuff` too.
        self.assertFalse(reaper.is_agent_process("node /usr/bin/freebuff", home="/root"))

    def test_another_accounts_session_is_not_ours(self):
        self.assertFalse(reaper.is_agent_process("/home/other/.config/manicode/freebuff", home="/root"))

    def test_an_unrelated_process_is_not_a_session(self):
        self.assertFalse(reaper.is_agent_process("/usr/bin/postgres -D /var/lib/postgresql", home="/root"))


class ClassifyCase(unittest.TestCase):
    thresholds = reaper.Thresholds(min_age=900, wedge_cpu=1200, max_age=14400, max_cpu=2700)

    def test_a_young_session_is_left_alone_whatever_it_is_doing(self):
        young = session(state="D", cpu=100000, age=300)
        self.assertIsNone(reaper.classify(young, self.thresholds))

    def test_a_wedged_session_is_named_wedged(self):
        wedged = session(state="D", cpu=1500, age=3600)
        self.assertEqual(reaper.classify(wedged, self.thresholds), "wedged")

    def test_d_state_without_the_cpu_behind_it_is_not_wedged(self):
        # A short `D` is ordinary: a page-in, a fsync, a container exec.
        self.assertIsNone(reaper.classify(session(state="D", cpu=60, age=3600), self.thresholds))

    def test_old_and_heavy_is_a_runaway(self):
        self.assertEqual(reaper.classify(session(cpu=3000, age=15000), self.thresholds), "runaway")

    def test_old_but_light_is_not_a_runaway(self):
        # A conversation somebody has had open all afternoon: 30 m CPU over 5 h.
        self.assertIsNone(reaper.classify(session(cpu=1800, age=18000), self.thresholds))

    def test_heavy_but_young_is_not_a_runaway(self):
        self.assertIsNone(reaper.classify(session(cpu=3000, age=1800), self.thresholds))

    def test_an_orphan_past_the_minimum_age_is_reaped(self):
        self.assertEqual(reaper.classify(session(ppid=1, cpu=1, age=1000), self.thresholds), "orphan")

    def test_the_incident_session_is_reaped(self):
        # 2026-10-24: 30 h old, 5 h 40 m CPU, `D` for most of it.
        incident = session(state="D", cpu=5 * 3600 + 40 * 60, age=30 * 3600)
        self.assertEqual(reaper.classify(incident), "wedged")

    def test_the_defaults_do_not_reap_a_long_working_session(self):
        # The session this file was written in: 9 h 40 m old, 1 h 40 m CPU, healthy.
        working = session(state="S", cpu=100 * 60, age=int(9.7 * 3600))
        self.assertIsNone(reaper.classify(working))

    def test_the_defaults_still_reap_a_runaway(self):
        runaway = session(state="S", cpu=3 * 3600, age=20 * 3600, ppid=5000)
        self.assertEqual(reaper.classify(runaway), "runaway")


class LiveFileCase(unittest.TestCase):
    def test_the_killed_sessions_live_file_is_removed(self):
        with tempfile.TemporaryDirectory() as home:
            path = reaper.live_session_file(home, 4321)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ownerPid": 4321}), encoding="utf-8")
            note = reaper.abandon_live_file(path)
            self.assertIn("removed", note)
            self.assertFalse(path.exists())

    def test_another_pids_live_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as home:
            path = reaper.live_session_file(home, 4321)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ownerPid": 9999}), encoding="utf-8")
            note = reaper.abandon_live_file(path)
            self.assertIn("left alone", note)
            self.assertTrue(path.exists())

    def test_no_live_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIn("no live file", reaper.abandon_live_file(reaper.live_session_file(home, 1)))


class HoldCase(unittest.TestCase):
    def test_a_hold_file_names_pids_and_ignores_comment_and_noise(self):
        with tempfile.TemporaryDirectory() as home:
            path = reaper.hold_file(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# a migration I am watching\n4321\n\n 99  # second\nnot-a-pid\n", encoding="utf-8")
            self.assertEqual(reaper.held(path), {4321, 99})

    def test_no_hold_file_holds_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(reaper.held(reaper.hold_file(home)), set())


class ReapCase(unittest.TestCase):
    def test_signals_arrive_in_order_and_a_stubborn_session_is_escalated(self):
        sent: list[int] = []
        alive = {"yes": True}

        def killer(pid: int, sig: int) -> None:
            sent.append(sig)
            if sig == signal.SIGKILL:
                alive["yes"] = False

        original = reaper._alive
        reaper._alive = lambda pid: alive["yes"]
        try:
            with tempfile.TemporaryDirectory() as home:
                result = reaper.reap(session(pid=4321), "wedged", home=home, grace=2, sleep=lambda _: None, killer=killer)
        finally:
            reaper._alive = original

        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(result.escalated)
        self.assertTrue(result.killed)

    def test_a_session_that_honours_sigterm_is_not_escalated(self):
        sent: list[int] = []
        alive = {"yes": True}

        def killer(pid: int, sig: int) -> None:
            sent.append(sig)
            alive["yes"] = False

        original = reaper._alive
        reaper._alive = lambda pid: alive["yes"]
        try:
            with tempfile.TemporaryDirectory() as home:
                result = reaper.reap(session(pid=4321), "runaway", home=home, grace=2, sleep=lambda _: None, killer=killer)
        finally:
            reaper._alive = original

        self.assertEqual(sent, [signal.SIGTERM])
        self.assertFalse(result.escalated)
        self.assertTrue(result.killed)

    def test_a_session_that_is_already_gone_is_not_a_failure(self):
        def killer(pid: int, sig: int) -> None:
            raise ProcessLookupError

        with tempfile.TemporaryDirectory() as home:
            result = reaper.reap(session(pid=4321), "orphan", home=home, grace=1, sleep=lambda _: None, killer=killer)
        self.assertTrue(result.killed)


class TextfileCase(unittest.TestCase):
    def test_the_run_is_readable_even_when_nothing_was_reaped(self):
        body = reaper.prometheus_text(reaper.Run(sessions=[session()]), now=1700000000)
        self.assertIn("innotel_agent_sessions_live 1", body)
        self.assertIn('innotel_agent_reaped_total{reason="none"} 0', body)
        self.assertIn("innotel_agent_last_run_timestamp 1700000000", body)

    def test_a_reap_is_counted_by_reason(self):
        item = reaper.Reaped(session=session(pid=7), reason="wedged", killed=True)
        body = reaper.prometheus_text(reaper.Run(sessions=[session(pid=7)], reaped=[item]), now=1)
        self.assertIn('innotel_agent_reaped_total{reason="wedged"} 1', body)
        self.assertNotIn('reason="none"', body)

    def test_the_candidate_count_drops_once_a_session_is_reaped(self):
        wedged = session(pid=8, state="D", cpu=5000, age=50000)
        body = reaper.prometheus_text(
            reaper.Run(
                sessions=[wedged],
                reaped=[reaper.Reaped(session=wedged, reason="wedged", killed=True)],
            ),
            now=1,
        )
        self.assertIn("innotel_agent_sessions_candidate 0", body)


class ScanCase(unittest.TestCase):
    def test_a_missing_proc_is_a_hard_error_not_an_empty_scan(self):
        with self.assertRaises(RuntimeError):
            reaper.sessions(proc="/proc/definitely-not-here")


if __name__ == "__main__":
    unittest.main()
