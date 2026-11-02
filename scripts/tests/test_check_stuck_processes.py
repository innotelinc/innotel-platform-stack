#!/usr/bin/env python3
"""Unit tests for check-stuck-processes.py — what counts as stuck, and what does not.

The check exists because a process somebody walked away from is not an estate question:
`estate-checks` asked whether a name was lit, a cap enforced, a disk filling, and
`agent-reaper` watched one binary, so an 18-hour `docker exec` on i4 took the host to load
13 without any check speaking. The two boundaries that keep it from crying wolf are
locked here:

  * **an hour**, so every ordinary reload, probe and log follow has finished; and
  * **a draining nginx worker with a live socket is working, not stuck** — it is proxying
    a long-lived stream, and killing it drops a client's connection.

Both are pure functions, so the tests assert the boundary rather than a live process table.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-stuck-processes.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_stuck_processes", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stuck = _load()


def _process(age, command, established=False, state="S", rss_kib=4096, pid=42, host="i4"):
    return stuck.Process(
        host=host,
        pid=pid,
        ppid=1,
        state=state,
        age=age,
        rss_kib=rss_kib,
        command=command,
        established=established,
    )


class ParseCase(unittest.TestCase):
    def test_a_table_line_becomes_a_process_with_its_arguments_intact(self):
        # `args` is last and keeps its spaces: a positional split would lose the arguments
        # that decide whether this is a probe in the first place.
        text = "  12     1 S  7200   512 docker exec metrics-prometheus grep -rn TEXTFILE_DIR /\n"
        [process] = stuck.parse_ps(text, "i4")
        self.assertEqual((process.pid, process.ppid), (12, 1))
        self.assertEqual(process.age, 7200)
        self.assertEqual(process.rss_kib, 512)
        self.assertEqual(process.command, "docker exec metrics-prometheus grep -rn TEXTFILE_DIR /")

    def test_a_header_or_short_line_is_ignored(self):
        text = "PID PPID STAT ELAPSED RSS ARGS\n  3 1 S 10\n"
        self.assertEqual(stuck.parse_ps(text, "i4"), [])

    def test_the_socket_table_names_the_pids_that_hold_a_connection(self):
        sockets = (
            'ESTAB 0 0 172.18.0.3:443 192.168.1.20:51422 users:(("nginx",pid=4096286,fd=31))\n'
            "ESTAB 0 0 172.18.0.3:443 192.168.1.21:51500\n"
        )
        self.assertEqual(stuck.established_pids(sockets), {4096286})

    def test_the_two_tables_are_split_on_the_marker(self):
        ps_text, sockets = stuck.split_tables(f"ps here\n{stuck.SOCKETS_MARKER}\nss here\n")
        self.assertEqual(ps_text.strip(), "ps here")
        self.assertEqual(sockets.strip(), "ss here")


class YoungProcessCase(unittest.TestCase):
    def test_a_probe_under_an_hour_is_left_alone(self):
        # The line that makes this check usable: a `docker exec` mid-probe is ordinary.
        self.assertIsNone(stuck.stuck_kind(_process(stuck.AGE_LIMIT - 1, "docker exec x grep -rn foo /")))

    def test_a_probe_past_an_hour_is_stuck(self):
        self.assertEqual(
            stuck.stuck_kind(_process(stuck.AGE_LIMIT + 1, "docker exec x grep -rn foo /")),
            "stray_command",
        )


class ShapeCase(unittest.TestCase):
    def test_each_interactive_shape_is_named(self):
        for command, shape in [
            ("docker exec -it app sh", "docker exec/run"),
            ("grep -rn TEXTFILE_DIR /", "recursive grep"),
            ("tail -f /var/log/syslog", "log follow"),
            ("tcpdump -i any port 53", "tracer"),
            ("dd if=/dev/sda of=/tmp/x", "raw copy"),
        ]:
            self.assertEqual(stuck.stuck_kind(_process(9999, command)), "stray_command", command)
            self.assertIn(shape, stuck._shape(command))

    def test_a_kernel_thread_is_not_a_probe(self):
        self.assertIsNone(stuck.stuck_kind(_process(99999, "[kworker/0:1]")))

    def test_a_long_lived_service_is_left_alone_when_its_shape_is_not_a_probe(self):
        self.assertIsNone(stuck.stuck_kind(_process(999999, "/usr/sbin/sshd -D")))


class AllowlistCase(unittest.TestCase):
    def test_a_command_that_is_long_lived_by_design_is_not_a_finding(self):
        command = "tail -F /usr/share/auvik/agent/logs/AuvikAgent.log"
        self.assertEqual(stuck.by_design(command), "the Auvik agent's own log tail, started by its entrypoint")
        self.assertIsNone(stuck.stuck_kind(_process(99999, command)))


class ShutdownCase(unittest.TestCase):
    def test_a_draining_worker_holding_a_socket_is_working(self):
        # The exception that keeps a legitimate SSE/websocket proxy from being killed.
        process = _process(99999, "nginx: worker process is shutting down", established=True)
        self.assertIsNone(stuck.stuck_kind(process))

    def test_a_draining_worker_with_nothing_to_drain_is_stuck(self):
        process = _process(99999, "nginx: worker process is shutting down", established=False)
        self.assertEqual(stuck.stuck_kind(process), "shutdown_pending")


class AuditCase(unittest.TestCase):
    def test_a_clean_host_is_not_a_finding(self):
        result = stuck.audit({"i4": [_process(10, "sshd: root@pts/0"), _process(20, "/usr/bin/node app.js")]})
        self.assertEqual(result.findings, [])
        self.assertTrue(result.ok)

    def test_a_finding_names_the_host_pid_age_and_memory(self):
        result = stuck.audit({"i4": [_process(66000, "docker exec x grep -rn foo /", rss_kib=393 * 1024)]})
        self.assertEqual([f.code for f in result.findings], ["stray_command"])
        message = result.findings[0].message
        self.assertIn("i4 pid 42", message)
        self.assertIn("18h20m", message)
        self.assertIn("393 MiB", message)
        self.assertFalse(result.ok)

    def test_a_shutdown_worker_says_what_to_do(self):
        result = stuck.audit({"i2": [_process(28800, "nginx: worker process is shutting down")]})
        self.assertEqual(result.findings[0].code, "shutdown_pending")
        self.assertIn("kill it", result.findings[0].message)


class SeriesCase(unittest.TestCase):
    def test_the_count_is_published_per_host_and_kind(self):
        body = stuck.prometheus_extra(
            {"i4": [_process(66000, "docker exec x grep -rn foo /"), _process(5, "sshd: root@pts/0")]}
        )
        self.assertIn("innotel_estate_stuck_processes", body)
        self.assertIn('innotel_estate_stuck_processes{host="i4",kind="stray_command"} 1', body)

    def test_a_clean_estate_publishes_a_zero_rather_than_nothing(self):
        # A series that is absent when clean would make the alert silent, which is the
        # silence this check is a response to.
        body = stuck.prometheus_extra({"i4": [_process(5, "sshd: root@pts/0")]})
        self.assertIn('innotel_estate_stuck_processes{host="",kind="none"} 0', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
