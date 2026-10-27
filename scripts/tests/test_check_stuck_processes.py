#!/usr/bin/env python3
"""Unit tests for check-stuck-processes.py — the hour-old-straggler invariant.

The cases are the three processes that were actually sitting on `i4` on 2026-10-27 at
load 13, and the states that must *not* be reported: a reload that is one minute old, the
Auvik agent's own log tail, and the Vault wrapper's rotation sleep.

The module under test has hyphens in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
CHECK = SCRIPTS / "check-stuck-processes.py"

sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("check_stuck_processes", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


chk = _load()

#: `ps -eo pid=,ppid=,stat=,etimes=,rss=,args=` — the format the check itself runs.
RUNNING_GREP = (
    "4074960 2797 Sl 66000 9656 docker exec metrics-prometheus sh -c grep -rn TEXTFILE_DIR / 2>/dev/null | head -5\n"
)
SHUTTING_WORKER = "1834366 2987441 S 28713 393080 nginx: worker process is shutting down\n"
FRESH_RELOAD = "4096286 2987441 S 120 395972 nginx: worker process is shutting down\n"
AUVIK_TAIL = "14798 14171 S 172506 760 /usr/bin/tail -F /usr/share/auvik/agent/logs/AuvikAgent.log\n"
VAULT_SLEEP = "3844179 2672058 S 9486 48 sleep 43200\n"


def processes(*lines: str, host: str = "i4"):
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return chk.parse_ps(text, host)


class ParseCase(unittest.TestCase):
    def test_command_keeps_its_spaces(self):
        [proc] = processes(RUNNING_GREP)
        self.assertIn("grep -rn TEXTFILE_DIR /", proc.command)
        self.assertEqual(proc.pid, 4074960)
        self.assertEqual(proc.ppid, 2797)
        self.assertEqual(proc.age, 66000)
        self.assertEqual(proc.rss_kib, 9656)

    def test_a_header_or_blank_is_not_a_process(self):
        self.assertEqual(chk.parse_ps("  PID  PPID STAT\n\n", "i1"), [])


class RuleCase(unittest.TestCase):
    def test_the_eighteen_hour_grep_is_a_stray_command(self):
        [proc] = processes(RUNNING_GREP)
        self.assertEqual(chk.stuck_kind(proc), "stray_command")

    def test_the_stuck_worker_is_shutdown_pending(self):
        [proc] = processes(SHUTTING_WORKER)
        self.assertEqual(chk.stuck_kind(proc), "shutdown_pending")

    def test_a_draining_worker_with_a_live_socket_is_doing_its_job(self):
        [proc] = processes(SHUTTING_WORKER)
        proc.established = True
        self.assertIsNone(chk.stuck_kind(proc))

    def test_a_reload_younger_than_the_limit_is_left_alone(self):
        [proc] = processes(FRESH_RELOAD)
        self.assertIsNone(chk.stuck_kind(proc))

    def test_the_probe_boundary_is_the_hour(self):
        [proc] = processes(RUNNING_GREP)
        proc.age = chk.AGE_LIMIT - 1
        self.assertIsNone(chk.stuck_kind(proc))
        proc.age = chk.AGE_LIMIT
        self.assertEqual(chk.stuck_kind(proc), "stray_command")

    def test_the_auvik_log_tail_is_by_design(self):
        [proc] = processes(AUVIK_TAIL, host="i1")
        self.assertEqual(chk.by_design(proc.command), "the Auvik agent's own log tail, started by its entrypoint")
        self.assertIsNone(chk.stuck_kind(proc))

    def test_the_vault_rotation_sleep_is_by_design(self):
        [proc] = processes(VAULT_SLEEP)
        self.assertIsNone(chk.stuck_kind(proc))

    def test_a_kernel_thread_is_not_a_probe(self):
        [proc] = processes("1882292 2 I< 111074 0 [kworker/R-cifsiod]\n")
        self.assertIsNone(chk.stuck_kind(proc))

    def test_an_ordinary_service_is_not_a_probe(self):
        [proc] = processes("7803 7573 Ssl 172000 189900 /usr/bin/dotnet /opt/technitium/dns/DnsServerApp.dll /etc/dns\n")
        self.assertIsNone(chk.stuck_kind(proc))


class AuditCase(unittest.TestCase):
    def test_each_stuck_process_is_a_failure_naming_the_host_and_age(self):
        result = chk.audit({"i4": processes(RUNNING_GREP, SHUTTING_WORKER, FRESH_RELOAD)})
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failures), 2)
        codes = sorted(f.code for f in result.findings)
        self.assertEqual(codes, ["shutdown_pending", "stray_command"])
        joined = " ".join(f.message for f in result.findings)
        self.assertIn("i4 pid 4074960", joined)
        self.assertIn("18h20m", joined)

    def test_a_clean_host_passes(self):
        result = chk.audit({"i1": processes(AUVIK_TAIL, host="i1"), "i4": processes(VAULT_SLEEP)})
        self.assertTrue(result.ok)
        self.assertEqual(result.findings, [])

    def test_the_series_carries_host_and_kind(self):
        body = chk.prometheus_extra({"i4": processes(RUNNING_GREP, SHUTTING_WORKER), "i1": processes(AUVIK_TAIL, host="i1")})
        self.assertIn('innotel_estate_stuck_processes{host="i4",kind="shutdown_pending"} 1', body)
        self.assertIn('innotel_estate_stuck_processes{host="i4",kind="stray_command"} 1', body)
        self.assertNotIn('kind="none"', body)

    def test_nothing_stuck_still_publishes_a_zero(self):
        body = chk.prometheus_extra({"i1": processes(AUVIK_TAIL, host="i1")})
        self.assertIn('innotel_estate_stuck_processes{host="",kind="none"} 0', body)


class SocketCase(unittest.TestCase):
    def test_a_pid_named_in_the_socket_table_is_marked(self):
        table = (
            'ESTAB 0 0 172.18.0.3:443 192.168.1.20:51422 users:(("nginx",pid=4096286,fd=31))\n'
            'ESTAB 0 0 172.18.0.3:443 192.168.1.21:51423 users:(("nginx",pid=4096287,fd=32))\n'
        )
        self.assertEqual(chk.established_pids(table), {4096286, 4096287})

    def test_a_socket_with_no_visible_owner_names_nobody(self):
        self.assertEqual(chk.established_pids("ESTAB 0 0 172.18.0.3:443 192.168.1.20:51422\n"), set())


class GatherCase(unittest.TestCase):
    def test_one_ps_per_host_through_read_host(self):
        seen = []

        def runner(target, command):
            seen.append((target, command))
            return RUNNING_GREP if target.endswith(".54") else ""

        observed = chk.gather({"i1": "root@192.168.1.51", "i4": "root@192.168.1.54"}, runner)
        self.assertEqual([t for t, _ in seen], ["root@192.168.1.51", "root@192.168.1.54"])
        self.assertEqual(seen[0][1], chk.PS_COMMAND)
        self.assertEqual(len(observed["i4"]), 1)
        self.assertEqual(observed["i4"][0].host, "i4")

    def test_a_worker_holding_a_socket_is_not_a_finding(self):
        sockets = f"\n{chk.SOCKETS_MARKER}\nESTAB 0 0 172.18.0.3:443 192.168.1.20:51422 users:((\"nginx\",pid=1834366,fd=31))\n"

        def runner(target, command):
            return SHUTTING_WORKER + sockets if target.endswith(".54") else ""

        observed = chk.gather({"i4": "root@192.168.1.54"}, runner)
        self.assertTrue(observed["i4"][0].established)
        self.assertTrue(chk.audit(observed).ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
