#!/usr/bin/env python3
"""Fail when a host carries a process that has been stuck for over an hour.

WHY THIS EXISTS
---------------
On 2026-10-27 `i4` sat at load 13 with 797 MiB free and 3.5 GiB of swap gone. Nothing
was misconfigured and no container was over its cap: one `docker exec` in
`metrics-prometheus` had been running `grep -rn TEXTFILE_DIR /` for **18 h 20 m**, and
`cerulean-npm` carried an nginx worker in `worker process is shutting down` for
**7 h 58 m** plus a cache-manager process orphaned when its old master died (393 and
390 MiB resident). Killing three processes took the `proxy` cgroup from 4200 to
3333 MiB and the load average from 13.3 to 3.8.

None of that was an estate question, which is why nothing caught it: `estate-checks`
asks whether a name is lit, a cap enforced, a disk filling; `agent-reaper` watches the
agent binary only. A command somebody typed and walked away from belongs to neither.

THE RULES
---------
1. **An hour is the line.** A reload that takes a minute, a `tail -f` behind a
   migration, a `docker exec` mid-probe — all ordinary. A process still there an hour
   later is holding memory and, often, a connection that nothing will ever close.

2. **A worker stuck shutting down is a failure — but only with nothing left to
   drain.** nginx sends its workers a shutdown signal on reload and reaps them when
   their last connection closes, so a worker still alive an hour later is either
   proxying one long-lived stream (legitimate: an SSE or websocket client can hold one
   open for hours) or waiting for nothing at all. The two cases are told apart by
   `ss`: a worker still holding an established socket is doing its job and is not
   reported; one holding none is stuck, keeps the whole old worker resident (386 MiB
   each, measured on `cerulean-npm`) and will never be reaped, because the master does
   not escalate on its own.

3. **A command-line probe is a failure, not an alert.** The patterns are the ones that
   only ever make sense interactively — `docker exec`/`run`, a recursive `grep`, a log
   follow, `journalctl -f`, `watch`, `timeout N …`, `tcpdump`, `strace`, `perf record`,
   `dd if=`. Matched on the command line, so a service that *is* one of these shapes by
   design has to be named.

4. **Long-lived by design is named, not guessed.** The allowlist carries the reason for
   each entry, and an allowlisted process is not a finding at all: the Auvik agent's own
   `tail -F` runs for the life of the container on `i1`, and the Vault bootstrap
   wrapper's `sleep 43200` is a 12-hour rotation timer. Both are correct states, and a
   check that reported them half-hourly would be pages nobody reads.

5. **A host that cannot be read is exit 2.** Never silently a pass: the whole point is
   that this shape was found late because nothing watched for it.

Usage:
    ./scripts/check-stuck-processes.py
    ./scripts/check-stuck-processes.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-stuck-processes.py       # where the estate uses passwords

Exit codes: 0 = nothing has been stuck for an hour, 1 = something has, 2 = the check
could not run (a host unreachable).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, read_host, ssh, write_prom  # noqa: E402

CHECK = "stuck_processes"

#: Where each host is reachable — the same table every host check reads.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: Seconds. An hour: long enough that every ordinary reload, probe and log follow has
#: finished, short enough that the next scheduled run sees the leak rather than a day of
#: it.
AGE_LIMIT = 3600

#: nginx's own words for a worker it has signalled and is waiting to exit.
SHUTDOWN_MARKER = "worker process is shutting down"

#: Commands that only make sense interactively, with the shape each one has.
PROBE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bdocker (exec|run)\b", "a docker exec/run"),
    (r"\bgrep\b[^\n]* -[A-Za-z]*[rR]", "a recursive grep"),
    (r"\b(tail|journalctl|dmesg)\b[^\n]* -[A-Za-z]*[fF]\b", "a log follow"),
    (r"(^|/|\s)watch\s", "a watch loop"),
    (r"\btimeout\s+\d", "a timeout wrapper"),
    (r"\b(tcpdump|strace|ltrace|perf record)\b", "a tracer"),
    (r"\bdd\s+if=", "a raw copy"),
)

#: Long-lived by design, each with the reason it is allowed to be old. Checked before
#: the probe patterns: an allowlisted process is a correct state, not a finding.
ALLOWLIST: tuple[tuple[str, str], ...] = (
    (r"tail -F /usr/share/auvik/agent/logs/AuvikAgent\.log",
     "the Auvik agent's own log tail, started by its entrypoint"),
    (r"/vault/bootstrap/entrypoint\.sh", "the Vault bootstrap wrapper"),
    (r"^sleep\s+43200$", "the Vault 12-hour rotation sleep"),
)

#: What separates the two tables in one host's output.
SOCKETS_MARKER = "==established-sockets=="

#: `ps` format: `pid ppid stat etimes rss args…`. `etimes` is seconds, so no `etime`
#: parsing is needed; `args` is last and keeps its spaces. The socket table follows it,
#: after a marker line, so a host costs one round trip: which pids still hold an
#: established connection decides whether a draining worker is working or stuck.
PS_COMMAND = f"ps -eo pid=,ppid=,stat=,etimes=,rss=,args=; echo {SOCKETS_MARKER}; ss -tnHp state established"


@dataclass
class Process:
    """One process as `ps` described it on its host."""

    host: str
    pid: int
    ppid: int
    state: str
    age: float
    rss_kib: int
    command: str
    #: Whether the process still holds an established socket. A worker draining for an
    #: hour with one is proxying a long-lived stream; with none it has nothing left to
    #: wait for and is stuck.
    established: bool = False

    @property
    def label(self) -> str:
        return f"{self.host} pid {self.pid} ({_human_age(self.age)}, {self.rss_kib // 1024} MiB, {self.state})"


def _human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def split_tables(text: str) -> tuple[str, str]:
    """`(ps table, socket table)` from one host's output."""
    ps_text, _, sockets = text.partition(SOCKETS_MARKER)
    return ps_text, sockets


def established_pids(sockets: str) -> set[int]:
    """Every pid `ss -tnHp state established` names as holding a socket.

    Lines look like `ESTAB 0 0 172.18.0.3:443 192.168.1.20:51422
    users:(("nginx",pid=4096286,fd=31))`; a line with no `users:` is a socket whose
    owner this process cannot see, and names nobody.
    """
    return {int(match) for match in re.findall(r"pid=(\d+)", sockets)}


def parse_ps(text: str, host: str = "") -> list[Process]:
    """Every process in one `ps` table. Pure, so the parser is the thing under test.

    Split five ways and let the rest be the command: a command line carries spaces, and
    `ps` truncates nothing here, so a positional split would lose the arguments that
    decide whether the process is a probe.
    """
    out: list[Process] = []
    for line in text.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) < 6 or not fields[0].isdigit():
            continue
        pid, ppid, state, etimes, rss, command = fields
        try:
            out.append(
                Process(
                    host=host,
                    pid=int(pid),
                    ppid=int(ppid),
                    state=state,
                    age=float(etimes),
                    rss_kib=int(rss),
                    command=command.strip(),
                )
            )
        except ValueError:
            continue
    return out


def by_design(command: str) -> str | None:
    """The reason this command is allowed to be old, or None."""
    for pattern, reason in ALLOWLIST:
        if re.search(pattern, command):
            return reason
    return None


def stuck_kind(process: Process, age_limit: float = AGE_LIMIT) -> str | None:
    """`shutdown_pending`, `stray_command`, or None to leave it alone.

    Pure, so the boundaries are what the tests assert rather than a live process table.
    A shutdown-pending worker is named first: it is the shape with an actionable cause,
    and it can also match a probe pattern when its command line carries one.
    """
    if process.age < age_limit:
        return None
    if by_design(process.command):
        return None
    if SHUTDOWN_MARKER in process.command and not process.established:
        return "shutdown_pending"
    if SHUTDOWN_MARKER in process.command:
        return None  # a draining worker with a live socket is doing its job
    if re.search(r"^\[", process.command):
        return None  # a kernel thread: `[kworker/0:1]`, `[rcu_gp]` — not a probe
    if SHUTDOWN_MARKER in process.command and process.established:
        return None  # still proxying a stream: draining does take that long, sometimes
    for pattern, _shape in PROBE_PATTERNS:
        if re.search(pattern, process.command):
            return "stray_command"
    return None


def audit(observed: dict[str, list[Process]], age_limit: float = AGE_LIMIT) -> Audit:
    """Every stuck process on every host, as findings."""
    out = Audit()
    for host, processes in observed.items():
        for process in processes:
            kind = stuck_kind(process, age_limit)
            if kind is None:
                continue
            if kind == "shutdown_pending":
                message = (
                    f"{process.label}: a worker has been shutting down for over "
                    f"{_human_age(age_limit)} — its connections never drained and the "
                    f"master will not escalate; kill it and let the reload finish"
                )
            else:
                message = (
                    f"{process.label}: {_shape(process.command)} has been running for over "
                    f"{_human_age(age_limit)} — {process.command[:110]}"
                )
            out.findings.append(Finding(kind, "fail", message))
    return out


def _shape(command: str) -> str:
    """The probe pattern's own words for this command, for the finding."""
    for pattern, shape in PROBE_PATTERNS:
        if re.search(pattern, command):
            return shape
    return "a command"


def gather(hosts: dict[str, str], runner=ssh) -> dict[str, list[Process]]:
    """Every host's process table, one `ps` per host."""
    observed: dict[str, list[Process]] = {}
    for host, target in hosts.items():
        ps_text, sockets = split_tables(read_host(host, target, PS_COMMAND, runner))
        held = established_pids(sockets)
        processes = parse_ps(ps_text, host)
        for process in processes:
            process.established = process.pid in held
        observed[host] = processes
    return observed


def counts(observed: dict[str, list[Process]], age_limit: float = AGE_LIMIT) -> dict[tuple[str, str], int]:
    """`(host, kind) -> count`, for the series this check publishes."""
    out: dict[tuple[str, str], int] = {}
    for host, processes in observed.items():
        for process in processes:
            kind = stuck_kind(process, age_limit)
            if kind is not None:
                out[(host, kind)] = out.get((host, kind), 0) + 1
    return out


def prometheus_extra(observed: dict[str, list[Process]], age_limit: float = AGE_LIMIT) -> str:
    """This check's own series, on top of the shared `innotel_estate_check` contract."""
    lines = [
        "# HELP innotel_estate_stuck_processes Processes stuck for over an hour, by host and kind",
        "# TYPE innotel_estate_stuck_processes gauge",
    ]
    rows = counts(observed, age_limit)
    if not rows:
        lines.append('innotel_estate_stuck_processes{host="",kind="none"} 0')
    for (host, kind), count in sorted(rows.items()):
        lines.append(f'innotel_estate_stuck_processes{{host="{host}",kind="{kind}"}} {count}')
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--age", type=float, default=AGE_LIMIT, help=f"seconds before a process counts as stuck (default {AGE_LIMIT})")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool, extra: str | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=extra)
            except OSError as error:
                print(f"check-stuck-processes: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-stuck-processes: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        observed = gather(hosts)
    except Exception as error:  # noqa: BLE001 — any ssh failure is the same finding here
        print(f"check-stuck-processes: could not read a host: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    result = audit(observed, args.age)
    print(f"check-stuck-processes: {sum(len(p) for p in observed.values())} process(es) on "
          f"{len(observed)} host(s), {len(result.failures)} stuck for over {_human_age(args.age)}")
    for finding in result.findings:
        print(f"FAIL {finding.message}")

    emit(result, ran=True, extra=prometheus_extra(observed, args.age))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
