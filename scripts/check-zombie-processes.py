#!/usr/bin/env python3
"""Count unreaped children on every estate host, and fail when they pile up.

WHY THIS EXISTS
---------------
Two containers leaked zombies for weeks and nothing reported it. `openmaic-mineru` on
i1 and `linkwarden` on i4 each ran a supervisor as PID 1 that never called `wait()`, so
every child it launched and lost stayed in the process table as `Z`. Nothing was broken
enough to page — the containers answered, the checks above passed — but the leak is
monotonic: each restart, each crashed child, one more uncollectable PID, until the
host's PID space or the container's `pids` limit is the thing that fails.

The fix was `init: true` in both compose files (2026-11-15), which puts a real reaper in
front of the supervisor. That fix is a *file* on two hosts; nothing in the estate would
notice it being reverted, and the next supervisor written the same way leaks the same
way. So this counts the symptom that is visible on every host regardless of whose
container is leaking.

WHAT IS A FINDING AND WHAT IS NOISE
-----------------------------------
A zombie whose parent is still alive is the leak: the parent will never reap it. A zombie
reparented to PID 1 (`ppid 1`) is a transient — init reaps it on its next pass — so the
count is what matters, not the presence of one. Every host has one or two at any moment.

`i2` carries one **deliberate** zombie: an `epmd` child of a k3s pod that cannot be
reaped because the estate has no `kubectl` on that host and the pod's spec is not ours to
change. It costs one pid slot and is left alone on purpose — do not "fix" it by
restarting k3s. The warning line is set above it so a normal host stays quiet.

THE RULES
---------
1. **3 zombies is a warning.** Above the estate's known baseline (0–1), below a leak.
2. **25 is a failure.** At that density the count is climbing on its own and a `pids`
   limit is the next thing to break.
3. **A host that cannot be read is not a pass** — same rule as every check here.

Usage:
    ./scripts/check-zombie-processes.py
    ./scripts/check-zombie-processes.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-zombie-processes.py

Exit codes: 0 = every host is under the warning line, 1 = one is not, 2 = a host could
not be read (never silently a pass).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, read_host, ssh, write_prom  # noqa: E402

CHECK = "host_zombies"

#: Where each estate host is reachable — the same table the other checks read. `i3`
#: belongs here from the start: it is a host in the estate again as of 2026-11-15. `i2` was
#: decommissioned on 2026-11-16 (`docs/estate-rebalance-2026-11-15.md` §19), and the
#: deliberate `epmd` that set the old baseline went with it.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: Above the known baseline (nothing on the estate reaps parents deliberately), below a real leak.
WARN_ZOMBIES = 3
FAIL_ZOMBIES = 25

#: `ps -eo stat=` pads its column, so the state is field-split before it is tested.
#: Two lines come back: the count, and a bounded sample of `comm(ppid)` for the finding.
_READ = (
    "ps -eo stat= | awk '{s=$1; if (substr(s,1,1)==\"Z\") n++} END{print \"COUNT\", n+0}'; "
    "ps -eo stat=,ppid=,comm= | awk '$1 ~ /^Z/ {printf \"%s(ppid %s) \", $3, $2} END{print \"\"}'"
)

_COUNT = re.compile(r"^COUNT\s+(\d+)\s*$")


@dataclass
class Reading:
    """One host's zombie count, and a sample of what they were."""

    host: str
    count: int | None = None
    sample: list[str] = field(default_factory=list)


def parse_ps(text: str) -> tuple[int, list[str]] | None:
    """`(count, sample)` from the two-line read, or None when it cannot be parsed.

    The count line is the authority; the sample is only what the finding quotes, and a
    host whose sample is empty but whose count is not is still counted, not discarded.
    """
    count = None
    sample: list[str] = []
    for line in text.splitlines():
        match = _COUNT.match(line.strip())
        if match:
            count = int(match.group(1))
        elif line.strip() and not line.startswith("COUNT"):
            sample = line.split()
    if count is None:
        return None
    return count, sample


def audit(readings: list[Reading]) -> Audit:
    """Read the counts as findings. Pure, so the thresholds are the thing under test."""
    out = Audit()
    for reading in sorted(readings, key=lambda r: r.host):
        if reading.count is None:
            out.findings.append(
                Finding(
                    "no_reading",
                    "fail",
                    f"{reading.host}: no readable `ps` output — the host could not be judged",
                )
            )
            continue
        quoted = ", ".join(reading.sample[:5]) or "no names read"
        if reading.count >= FAIL_ZOMBIES:
            out.findings.append(
                Finding(
                    "leaking",
                    "fail",
                    f"{reading.host}: {reading.count} zombies ({quoted}) — past the "
                    f"{FAIL_ZOMBIES} line; a `pids` limit is the next thing to break",
                )
            )
        elif reading.count >= WARN_ZOMBIES:
            out.findings.append(
                Finding(
                    "unreaped",
                    "warn",
                    f"{reading.host}: {reading.count} zombies ({quoted}) — past the "
                    f"{WARN_ZOMBIES} baseline; a PID 1 that does not reap is the usual cause "
                    f"(`init: true` is the fix)",
                )
            )
    return out


def zombie_series(readings: list[Reading]) -> str:
    """The per-host gauge, so the count is chartable and not only the verdict."""
    lines = [
        f"# HELP {METRIC}_host_zombie_processes Unreaped child processes, as counted by `ps`",
        f"# TYPE {METRIC}_host_zombie_processes gauge",
    ]
    for reading in sorted(readings, key=lambda r: r.host):
        if reading.count is None:
            continue
        lines.append(
            f'{METRIC}_host_zombie_processes{{check="{CHECK}",host="{reading.host}"}} {reading.count}'
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# The estate, as it is
# --------------------------------------------------------------------------------------


def gather(hosts: dict[str, str], runner=ssh) -> list[Reading]:
    """Read each host's zombie count over ssh, through `read_host`.

    An unreachable host raises (`HostUnreadable`) and `main` exits 2, the same as every
    other check: "the host is gone" is a different thing from "the host answered and is
    leaking". Output that arrives but cannot be parsed is a local `no_reading` finding.
    """
    readings: list[Reading] = []
    for host, target in hosts.items():
        text = read_host(host, target, _READ, runner)
        parsed = parse_ps(text)
        if parsed is None:
            readings.append(Reading(host=host))
        else:
            count, sample = parsed
            readings.append(Reading(host=host, count=count, sample=sample))
    return readings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool, readings: list[Reading] | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=zombie_series(readings) if readings else None)
            except OSError as error:
                print(f"check-zombie-processes: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-zombie-processes: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        readings = gather(hosts)
    except Exception as error:
        print(f"check-zombie-processes: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-zombie-processes: reads hosts with key-based `ssh`; where the estate uses "
                "passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(readings)
    emit(result, ran=True, readings=readings)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    counted = ", ".join(f"{r.host} {r.count}" if r.count is not None else f"{r.host} ?" for r in readings)
    print(f"\ncheck-zombie-processes: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    print(f"  zombies: {counted}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
