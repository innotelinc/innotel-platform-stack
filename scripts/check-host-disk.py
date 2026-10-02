#!/usr/bin/env python3
"""Warn when a host's root filesystem is filling up, and fail when it is nearly full.

WHY THIS EXISTS
---------------
The estate's own page has said for days that **i2's root filesystem is the one to watch**
(48 % at the 2026-10-02 refresh, with the standalone-project migration filling `dev`), and
that `i4`'s climbed to 39 % when four rootfs copies landed on it. Both were read by hand,
at survey time, and nothing watched them in between: a disk fills by arithmetic, overnight,
and the first symptom is a service that cannot write — a container that dies on a full
volume, a database that stops checkpointing. `docs/container-placement.md` §Open items
keeps this as a standing concern; this makes it a number instead of a note.

Only the root filesystem is checked. That is where the estate's own concern lives (`dir`
storage pools, container rootfs copies, docker images all sit under `/` on every host), and
a second mountpoint is a second policy question rather than a second number. When one
appears, it belongs in `MOUNTS` beside the thresholds.

THE RULES
---------
1. **80 % is a warning.** Enough runway to act, not so little that the first sign is an
   outage. "Enough runway" is the point: at 90 % a host is one log rotation from trouble.
2. **90 % is a failure.** A root filesystem that full is an incident waiting for a write.
3. **A host that cannot be read is not a pass.** No `df` output is a failure naming the
   host, never silence — the same rule every check here follows.

Usage:
    ./scripts/check-host-disk.py
    ./scripts/check-host-disk.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-host-disk.py

Exit codes: 0 = every root filesystem has room, 1 = one does not, 2 = a host could not be
read (never silently a pass).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, read_host, ssh, write_prom  # noqa: E402

CHECK = "host_disk"

#: Where each estate host is reachable — the same table the other checks read.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: The mountpoints to watch, per host. One entry today: the estate's disk concern is `/`.
MOUNTS: dict[str, list[str]] = {name: ["/"] for name in HOSTS}

#: Percent at/above which a mountpoint is reported. 80 leaves room to act; 90 is an incident.
WARN_PERCENT = 80.0
FAIL_PERCENT = 90.0

#: `df -P` columns: Filesystem, 1024-blocks, Used, Available, Capacity, Mounted on. `-P`
#: forces one line per filesystem, so a wrapped long device name cannot look like a mount.
_DF = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(.+)$")


@dataclass
class Usage:
    """One host's mountpoint, as `df` reports it."""

    host: str
    mount: str
    percent: float | None = None
    avail_bytes: int | None = None
    total_bytes: int | None = None


def parse_df(text: str) -> tuple[float, int, int] | None:
    """`(percent, available_bytes, total_bytes)` from `df -P` output, or None.

    The last matching line wins: `df -P <mount>` prints one line, and a run whose device
    name is long enough to be wrapped is what `-P` exists to prevent.
    """
    found = None
    for line in text.splitlines():
        match = _DF.match(line.strip())
        if match:
            total_kb, avail_kb, percent = int(match.group(2)), int(match.group(4)), float(match.group(5))
            found = (percent, avail_kb * 1024, total_kb * 1024)
    return found


def audit(usages: list[Usage]) -> Audit:
    """Read the usages as findings. Pure, so what counts as tight is the thing under test."""
    out = Audit()
    for usage in sorted(usages, key=lambda u: (u.host, u.mount)):
        where = f"{usage.host} {usage.mount}"
        if usage.percent is None:
            out.findings.append(
                Finding("no_reading", "fail", f"{where}: no readable `df` output — the host could not be judged")
            )
            continue
        if usage.percent >= FAIL_PERCENT:
            out.findings.append(
                Finding(
                    "full",
                    "fail",
                    f"{where}: {usage.percent:.0f} % used ({_human(usage.avail_bytes)} free) — "
                    f"past the {FAIL_PERCENT:.0f} % line, one write from an outage",
                )
            )
        elif usage.percent >= WARN_PERCENT:
            out.findings.append(
                Finding(
                    "tight",
                    "warn",
                    f"{where}: {usage.percent:.0f} % used ({_human(usage.avail_bytes)} free) — "
                    f"the {WARN_PERCENT:.0f} % line; reclaim before it is an outage",
                )
            )
    return out


def _human(size: int | None) -> str:
    if size is None:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024.0
    return f"{size:.1f} TiB"


def disk_series(usages: list[Usage]) -> str:
    """The per-host gauge, so the numbers are chartable and not only the verdict."""
    lines = [
        f"# HELP {METRIC}_host_disk_percent Root filesystem used, as a percentage",
        f"# TYPE {METRIC}_host_disk_percent gauge",
        f"# HELP {METRIC}_host_disk_avail_bytes Root filesystem available bytes",
        f"# TYPE {METRIC}_host_disk_avail_bytes gauge",
    ]
    for usage in sorted(usages, key=lambda u: (u.host, u.mount)):
        if usage.percent is None:
            continue
        selector = f'check="{CHECK}",host="{usage.host}",mount="{usage.mount}"'
        lines.append(f"{METRIC}_host_disk_percent{{{selector}}} {usage.percent:.3f}")
        if usage.avail_bytes is not None:
            lines.append(f"{METRIC}_host_disk_avail_bytes{{{selector}}} {usage.avail_bytes}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# The estate, as it is
# --------------------------------------------------------------------------------------


def gather(hosts: dict[str, str], mounts: dict[str, list[str]], runner=ssh) -> list[Usage]:
    """Read each host's watched mountpoints over ssh, through `read_host`.

    An unreachable host raises (`HostUnreadable`) and `main` exits 2, the same as every
    other check: "the host is gone" is a different thing from "the host answered and its
    disk is tight", and the alert that names it is `EstateCheckCouldNotRun`. Output that
    arrives but cannot be parsed is a local `no_reading` finding instead — the host was
    reached, so this is about `df`, not about the estate.
    """
    usages: list[Usage] = []
    for host, target in hosts.items():
        for mount in mounts.get(host, ["/"]):
            text = read_host(host, target, f"df -P {mount} | tail -n 1", runner)
            parsed = parse_df(text)
            if parsed is None:
                usages.append(Usage(host=host, mount=mount))
            else:
                percent, avail, total = parsed
                usages.append(Usage(host=host, mount=mount, percent=percent, avail_bytes=avail, total_bytes=total))
    return usages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    mounts = {name: list(m) for name, m in MOUNTS.items()}
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()
        mounts = {name: ["/"] for name in hosts}

    def emit(result: Audit | None, ran: bool, usages: list[Usage] | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=disk_series(usages) if usages else None)
            except OSError as error:
                print(f"check-host-disk: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-host-disk: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        usages = gather(hosts, mounts)
    except Exception as error:
        print(f"check-host-disk: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-host-disk: reads hosts with key-based `ssh`; where the estate uses "
                "passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(usages)
    emit(result, ran=True, usages=usages)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    readings = ", ".join(
        f"{u.host} {u.percent:.0f}%" if u.percent is not None else f"{u.host} ?" for u in usages
    )
    print(f"\ncheck-host-disk: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    print(f"  root used: {readings}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
