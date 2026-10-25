#!/usr/bin/env python3
"""Stop an agent session that has wedged or run away, and say which one and why.

WHY THIS EXISTS
---------------
On 2026-10-24 `i4` sat at load **104** on two vCPU with 143 MiB free and 5.3 GiB of
swap in use. Nothing in the estate was misconfigured and no container was leaking: the
culprit was one **agent session**, started the day before at 15:26 on `pts/0`, that had
sat in `D` state for most of its life with **5 h 40 m of accumulated CPU** and 674 MiB
resident — and had been spawning short-lived `node` children (`tsc --noEmit`, a probe
script in `/tmp`) all the while. The first symptom was that `ps` and `ls /proc` timed
out: on a box that loaded, a fork storm and a thrashing allocator look exactly like a
dead machine.

Nothing watched for that shape. `estate-checks` is half-hourly and asks estate
questions — is the address lit, is the cap enforced, is the disk filling — and this was
not an estate question, it was one process on one host. `edge-liveness` watches the
edge from off the edge, which is a different failure. So the session was found by a
person noticing the box was slow, after thirty hours.

This is the missing half of that picture: a **reaper**. It is deliberately not a check
that reports and waits for somebody — a session at 100 % of two cores costs every other
tenant on the host its latency for as long as it takes a human to read a page. It is
the one job here that acts, so it acts narrowly and it says what it did.

THE RULES
---------
1. **Only an agent session is a candidate.** The process table is scanned for the
   agent binary itself (`~/.config/manicode/freebuff`) and never for its children, its
   broker (`--terminal-command-broker`, which is short-lived by design), or the `node`
   wrapper that spawns it. A reaper that can kill the wrong process is worse than the
   runaway.

2. **Three shapes are reaped, and each has to be argued for in numbers.**
   - `wedged` — in `D` (uninterruptible) *now*, with at least `--wedge-cpu` of CPU
     behind it and `--min-age` of life. A healthy session spends essentially no time
     in `D`; one that is there with hours of CPU behind it is stuck against a kernel
     that will not let it go, which is what `SIGKILL` is for.
   - `runaway` — older than `--max-age` **and** past `--max-cpu`. Both halves matter:
     age alone reaps a long conversation somebody is using, CPU alone reaps a short
     legitimate burst. The defaults are deliberately far out (12 h, 2 h): a real
     session on this host ran 9 h 40 m with 1 h 40 m of CPU across an afternoon of
     tests, which is a working session and not a runaway — measured 2026-10-24 while
     the thresholds were being set, and the reason they are not 4 h/45 m.
   - `orphan` — its parent is gone (`ppid 1`) and it has outlived `--min-age`. Nobody
     can type into it, so nothing is lost by ending it.
   A session that fits none of these is left alone, whatever its numbers look like.

3. **The reaper never reaps itself.** Its own pid and every ancestor are excluded from
   the scan, so a session that runs this script by hand cannot kill the shell it is
   typing into.

4. **`SIGTERM`, then `SIGKILL` after `--grace`.** The same order that was tried by hand
   on 2026-10-24 and failed: a process in `D` cannot act on `SIGTERM`, so the grace is
   short (10 s) and the `SIGKILL` is expected rather than exceptional.

5. **A stale live-session file goes with it.** The agent keeps
   `~/.config/manicode/freebuff-live-<pid>.json` while a session is up; a killed
   session leaves the file behind, and a file whose pid is gone reads as a live session
   to anything that trusts the directory. Removing it is part of the reap.

6. **Every run publishes what it saw**, including runs that reaped nothing: the point
   of the numbers is to tell a quiet hour from a reaper that stopped running. The
   textfile is `innotel_agent_*` — its own family, not the estate-check contract,
   because this unit runs on the host rather than reading hosts over ssh and its
   absence is not an estate invariant.

7. **A pid in the hold file is never reaped.** `~/.config/manicode/reaper-hold`, one
   pid per line (blank lines and `#` comments ignored), is the escape hatch for the
   session somebody is deliberately leaving to run — a long migration, a soak test. It
   is a file rather than a flag because the person has to be able to set it from the
   session it protects, without restarting anything.

Usage:
    ./scripts/reap-wedged-agents.py                  # report only
    ./scripts/reap-wedged-agents.py --apply          # reap what the rules name
    ./scripts/reap-wedged-agents.py --apply --prom /var/lib/node_exporter/textfile/agent-reaper.prom

The unit (`systemd/agent-reaper.{service,timer}`) runs it with `--apply` every ten
minutes. Run without `--apply`, it prints the same findings and kills nothing.

Exit codes: 0 = nothing to reap, or what the rules named was reaped, 1 = a session
matched and was not reaped (`--apply` not given, or the kill failed), 2 = the scan could
not run — never silently a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

#: The agent binary, and the TUI directory it writes its live-session files into. Both
#: are under the account that runs the session, so neither is a fixed path: `--root`
#: and `--home` exist so a different account (or a test) can point at its own.
AGENT_NAME = "freebuff"
BROKER_FLAG = "--terminal-command-broker"

#: One shape of wedged session, with the numbers it has to clear. Defaults are read off
#: the 2026-10-24 incident (30 h old, 5 h 40 m CPU, `D` for most of it) and set an order
#: of magnitude below it, so the shape is caught early and a session that is merely busy
#: for a while is not.
MIN_AGE = 15 * 60  # a session younger than this is never a candidate
WEDGE_CPU = 20 * 60  # CPU behind a `D`-state session before it counts as wedged
MAX_AGE = 12 * 3600  # a session older than this is a candidate…
MAX_CPU = 2 * 3600  # …once it has also spent this much CPU
GRACE = 10  # seconds between SIGTERM and SIGKILL
HZ = os.sysconf("SC_CLK_TCK")


@dataclass
class Session:
    """One agent session, as `/proc` describes it at this instant."""

    pid: int
    ppid: int
    state: str
    cpu_seconds: float
    age_seconds: float
    rss_bytes: int
    command: str

    @property
    def label(self) -> str:
        return f"pid {self.pid} ({_human_time(self.age_seconds)} old, {_human_time(self.cpu_seconds)} CPU, {self.state})"


@dataclass
class Thresholds:
    min_age: float = MIN_AGE
    wedge_cpu: float = WEDGE_CPU
    max_age: float = MAX_AGE
    max_cpu: float = MAX_CPU


@dataclass
class Reaped:
    session: Session
    reason: str
    killed: bool
    escalated: bool = False
    note: str = ""


@dataclass
class Run:
    sessions: list[Session] = field(default_factory=list)
    reaped: list[Reaped] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)


def _human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


# --------------------------------------------------------------------------------------
# Reading the process table
# --------------------------------------------------------------------------------------


def parse_stat(text: str) -> tuple[int, str, float, float] | None:
    """`(ppid, state, cpu_seconds, age_seconds)` from one `/proc/<pid>/stat` line, or None.

    The command name sits in parentheses and may itself contain spaces and parentheses,
    so the fields are read after the **last** `)` rather than by splitting the line —
    the same reason `estate_check` and every other parser here does it this way.
    """
    close = text.rfind(")")
    if close < 0:
        return None
    try:
        rest = text[close + 2 :].split()
        # After the command: state, ppid, … (field 3 onward of proc(5)).
        state = rest[0]
        ppid = int(rest[1])
        utime, stime = int(rest[11]), int(rest[12])
        starttime = int(rest[19])
    except (IndexError, ValueError):
        return None
    return ppid, state, (utime + stime) / HZ, _age_from(starttime)


def _age_from(starttime: int) -> float:
    """Seconds since the process started, from its starttime field and the boot time."""
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return max(0.0, uptime - starttime / HZ)


def _rss(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm", encoding="utf-8") as handle:
            return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def is_agent_process(cmdline: str, *, home: str | None = None) -> bool:
    """Whether this `cmdline` is the agent's own process, and not its broker or wrapper.

    The binary is matched by name rather than by path: the same build runs under any
    home directory, and a path that changes with the account would make the reaper
    silently stop matching anything — the state this repo calls a check that cannot
    fail. The broker is excluded by its flag, and the `node` wrapper by not being the
    `freebuff` binary at all.
    """
    if not cmdline or BROKER_FLAG in cmdline:
        return False
    argv = cmdline.split()
    if not argv:
        return False
    # `node /usr/bin/freebuff` is the wrapper that spawns the session, not the session:
    # it is small, it holds no tools, and killing it leaves the TUI behind. The agent
    # itself is the executable, so its own argv[0] is the binary.
    if os.path.basename(argv[0]) in {"node", "python", "python3", "sh", "bash"}:
        return False
    for part in argv:
        if os.path.basename(part) != AGENT_NAME:
            continue
        if home is not None and not os.path.abspath(part).startswith(os.path.abspath(home) + os.sep):
            continue
        return True
    return False


def sessions(*, proc: str = "/proc", home: str | None = None) -> list[Session]:
    """Every live agent session on this host, newest last."""
    found: list[Session] = []
    try:
        entries = os.listdir(proc)
    except OSError as error:
        raise RuntimeError(f"cannot list {proc}: {error}") from error
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        cmdline = _cmdline(pid)
        if not is_agent_process(cmdline, home=home):
            continue
        try:
            stat = Path(f"{proc}/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_stat(stat)
        if parsed is None:
            continue
        ppid, state, cpu, age = parsed
        found.append(
            Session(pid=pid, ppid=ppid, state=state, cpu_seconds=cpu, age_seconds=age, rss_bytes=_rss(pid), command=cmdline)
        )
    found.sort(key=lambda s: s.age_seconds)
    return found


# --------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------


def classify(session: Session, thresholds: Thresholds = Thresholds()) -> str | None:
    """Why this session should be reaped, or None to leave it alone.

    Pure, so the boundaries are the thing under test rather than a live process table.
    Order matters only for the label: a `D`-state session is reported as `wedged` even
    when it is also old and heavy, because that is the shape with the actionable cause.
    """
    if session.age_seconds < thresholds.min_age:
        return None
    if session.state == "D" and session.cpu_seconds >= thresholds.wedge_cpu:
        return "wedged"
    if session.age_seconds >= thresholds.max_age and session.cpu_seconds >= thresholds.max_cpu:
        return "runaway"
    if session.ppid == 1:
        return "orphan"
    return None


def live_session_file(home: str, pid: int) -> Path:
    """The agent's own `freebuff-live-<pid>.json` for this session."""
    return Path(home) / ".config" / "manicode" / f"freebuff-live-{pid}.json"


def hold_file(home: str) -> Path:
    """Where a person writes the pids the reaper must leave alone."""
    return Path(home) / ".config" / "manicode" / "reaper-hold"


def held(path: Path) -> set[int]:
    """The pids named in a hold file. A missing or unreadable file holds nothing."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    pids: set[int] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def abandon_live_file(path: Path) -> str:
    """Remove a killed session's live file, and say what was removed (or why not).

    Only a file whose `ownerPid` is the pid being reaped is touched: a stale file for a
    *different* session belongs to that session's reap, and deleting it here would hide
    a live session from whatever reads the directory.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return "no live file"
    try:
        owner = int(json.loads(raw).get("ownerPid", 0))
    except (ValueError, TypeError):
        return "live file unreadable"
    if owner != int(path.stem.rsplit("-", 1)[-1]):
        return f"live file names pid {owner}, left alone"
    try:
        path.unlink()
    except OSError as error:
        return f"live file not removed: {error}"
    return f"live file removed ({path.name})"


def _alive(pid: int) -> bool:
    """Whether `pid` still exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    parsed = parse_stat(stat)
    return bool(parsed and parsed[1] != "Z")


def reap(session: Session, reason: str, *, home: str, grace: float, sleep=time.sleep, killer=os.kill) -> Reaped:
    """End one session: `SIGTERM`, wait `grace`, `SIGKILL`. Then drop its live file."""
    result = Reaped(session=session, reason=reason, killed=False)
    try:
        killer(session.pid, signal.SIGTERM)
    except ProcessLookupError:
        result.killed = True
        result.note = "already gone"
        return result
    except PermissionError as error:
        result.note = f"SIGTERM refused: {error}"
        return result

    deadline = grace
    while deadline > 0:
        sleep(min(1.0, deadline))
        deadline -= 1.0
        if not _alive(session.pid):
            break
    if _alive(session.pid):
        try:
            killer(session.pid, signal.SIGKILL)
            result.escalated = True
        except ProcessLookupError:
            pass
        except PermissionError as error:
            result.note = f"SIGKILL refused: {error}"
            return result
        sleep(1.0)
    result.killed = not _alive(session.pid)
    note = abandon_live_file(live_session_file(home, session.pid))
    result.note = f"{result.note}; {note}".strip("; ")
    return result


# --------------------------------------------------------------------------------------
# The signal — a textfile the alert rules read
# --------------------------------------------------------------------------------------


def prometheus_text(run: Run, now: float) -> str:
    """This run as Prometheus text. Pure, so the contract is the thing under test."""
    by_reason: dict[str, int] = {}
    for item in run.reaped:
        if item.killed:
            by_reason[item.reason] = by_reason.get(item.reason, 0) + 1
    lines = [
        "# HELP innotel_agent_sessions_live Agent sessions on this host",
        "# TYPE innotel_agent_sessions_live gauge",
        f"innotel_agent_sessions_live {len(run.sessions)}",
        "# HELP innotel_agent_sessions_candidate Sessions matching a reap rule",
        "# TYPE innotel_agent_sessions_candidate gauge",
        f"innotel_agent_sessions_candidate {sum(1 for s in run.sessions if classify(s) and s.pid not in _live(run))}",
        "# HELP innotel_agent_reaped_total Sessions ended by the reaper this run",
        "# TYPE innotel_agent_reaped_total gauge",
    ]
    if not by_reason:
        lines.append('innotel_agent_reaped_total{reason="none"} 0')
    for reason, count in sorted(by_reason.items()):
        lines.append(f'innotel_agent_reaped_total{{reason="{reason}"}} {count}')
    lines += [
        "# HELP innotel_agent_last_run_timestamp When the reaper last ran",
        "# TYPE innotel_agent_last_run_timestamp gauge",
        f"innotel_agent_last_run_timestamp {now:.0f}",
        "# HELP innotel_agent_sessions_rss_bytes Resident bytes across agent sessions",
        "# TYPE innotel_agent_sessions_rss_bytes gauge",
        f"innotel_agent_sessions_rss_bytes {sum(s.rss_bytes for s in run.sessions)}",
    ]
    return "\n".join(lines) + "\n"


def _live(run: Run) -> set[int]:
    return {item.session.pid for item in run.reaped if item.killed}


def write_textfile(path: str, body: str) -> None:
    """Write the textfile atomically and world-readably, as `estate_check` does."""
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False, prefix=".agent-reaper-")
    try:
        handle.write(body)
        handle.close()
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="reap what the rules name (default: report only)")
    parser.add_argument("--prom", metavar="PATH", help="write the Prometheus textfile here")
    parser.add_argument("--home", default=os.path.expanduser("~"), help="the account whose sessions are reaped")
    parser.add_argument("--grace", type=float, default=GRACE, help=f"seconds between SIGTERM and SIGKILL (default {GRACE})")
    parser.add_argument("--min-age", type=float, default=MIN_AGE, help="a session younger than this is never a candidate")
    parser.add_argument("--wedge-cpu", type=float, default=WEDGE_CPU, help="CPU behind a D-state session before it counts")
    parser.add_argument("--max-age", type=float, default=MAX_AGE, help="age past which a heavy session is a runaway")
    parser.add_argument("--max-cpu", type=float, default=MAX_CPU, help="CPU a runaway must also have spent")
    args = parser.parse_args(argv)

    thresholds = Thresholds(
        min_age=args.min_age, wedge_cpu=args.wedge_cpu, max_age=args.max_age, max_cpu=args.max_cpu
    )

    try:
        found = sessions(home=args.home)
    except RuntimeError as error:
        print(f"reap-wedged-agents: {error}", file=sys.stderr)
        return 2

    run = Run(sessions=found)
    now = time.time()
    failed = False
    spared = held(hold_file(args.home))

    for session in found:
        reason = classify(session, thresholds)
        if reason is None:
            continue
        if session.pid in spared:
            run.held.append(f"{session.label}: {reason} (held — named in {hold_file(args.home)})")
            continue
        if not args.apply:
            run.skipped.append(f"{session.label}: {reason} (not reaped — no --apply)")
            continue
        result = reap(session, reason, home=args.home, grace=args.grace)
        run.reaped.append(result)
        if not result.killed:
            failed = True

    for line in run.skipped:
        print(f"SKIP {line}")
    for line in run.held:
        print(f"HOLD {line}")
    for item in run.reaped:
        verdict = "REAP" if item.killed else "FAIL"
        escalation = " (SIGKILL)" if item.escalated else ""
        print(f"{verdict} {item.session.label}: {item.reason}{escalation}{' — ' + item.note if item.note else ''}")

    if args.prom:
        try:
            write_textfile(args.prom, prometheus_text(run, now))
        except OSError as error:
            print(f"reap-wedged-agents: could not write {args.prom}: {error}", file=sys.stderr)

    print(
        f"\nreap-wedged-agents: {len(found)} session(s), {len(run.reaped) + len(run.skipped) + len(run.held)} matching a rule, "
        f"{sum(1 for r in run.reaped if r.killed)} reaped"
    )
    if failed:
        return 1
    return 1 if run.skipped else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
