#!/usr/bin/env python3
"""Fail when a container's declared CPU cap is not the one that is actually enforced.

WHY THIS EXISTS
---------------
A `limits.cpu` that is written down but not in force is worse than none: the
placement page reads as though a runaway is fenced, and the runaway is not. That is
not hypothetical — the 2026-10-01 pass set i1's caps and then read `cpu.max` to check
them, found `max`, and concluded the caps were waiting for a restart. They were not:
incus implements `limits.cpu` as a **cpuset pin**, not a CPU quota, so `cpu.max` stays
`max` on every container that has a cap, and the cap was live the whole time. The
wrong file made a correct state look broken, which is the same class of error as a
check that never fails.

THE RULES
---------
1. **The enforced cap is the cpuset.** A running container whose `limits.cpu` is N
   must have exactly N CPUs in `/sys/fs/cgroup/lxc.payload.<c>/cpuset.cpus.effective`
   (a container cannot use a CPU it is not assigned, which is why this bounds a
   runaway harder than a quota would). Differs → failure, and it names both numbers.
2. **A cap above the host is a warning, not a failure.** `limits.cpu=8` on a 4-CPU
   host says nothing an operator can act on; incus clamps it to the host, so the
   cpuset will be smaller than the number written and that is reported as the
   misconfiguration it is rather than as a broken cap.
3. **No declared cap is not a finding.** The estate caps containers it wants to
   fence, not all of them; an uncapped container is a decision (see
   `docs/container-placement.md`), not drift.
4. **A stopped container is a note.** It has no cgroup to be wrong.

Usage:
    ./scripts/check-container-limits.py
    ./scripts/check-container-limits.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-container-limits.py        # where the estate uses passwords

Exit codes: 0 = every declared cap is enforced, 1 = one is not, 2 = the check could
not run (a host unreachable — never silently a pass).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, count_cpus, ssh, write_prom  # noqa: E402

CHECK = "container_limits"

#: Where each estate host is reachable. `i4` is in scope from 2026-10-02: it took the
#: edge and three other containers off i1, each of which declares a `limits.cpu`, so a
#: cap there is a fence to check like any other. i4 is a 2-vCPU host and the moved
#: containers declare 1-2, so a cap above the host would be a real finding here.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}


@dataclass
class Container:
    """One container's declared cap and the cpuset actually in force."""

    name: str
    state: str
    declared: int | None = None
    effective: int | None = None


def audit(observed: dict[str, list[Container]], host_cpus: dict[str, int]) -> Audit:
    """Compare what each container declares against what the host enforces."""
    out = Audit()
    for host, containers in observed.items():
        cpus = host_cpus.get(host, 0)
        for inst in sorted(containers, key=lambda c: c.name):
            if inst.declared is None:
                continue  # rule 3: no declared cap is not a finding
            if inst.state != "RUNNING":
                out.findings.append(
                    Finding("stopped", "note", f"{host} {inst.name}: {inst.state} — no cgroup to check")
                )
                continue
            if inst.declared > cpus:
                out.findings.append(
                    Finding(
                        "cap_above_host",
                        "warn",
                        f"{host} {inst.name}: limits.cpu={inst.declared} on a {cpus}-CPU host — "
                        f"incus clamps it, so the written cap is not the fenced one",
                    )
                )
            if inst.effective is None:
                out.findings.append(
                    Finding(
                        "no_cpuset",
                        "fail",
                        f"{host} {inst.name}: running with limits.cpu={inst.declared} but no readable "
                        f"cpuset — the cap is not enforced",
                    )
                )
                continue
            if inst.effective != inst.declared and inst.declared <= cpus:
                out.findings.append(
                    Finding(
                        "cap_not_enforced",
                        "fail",
                        f"{host} {inst.name}: limits.cpu={inst.declared} but the cpuset holds "
                        f"{inst.effective} CPU(s) — the fence is not the one that was written",
                    )
                )
    return out


# --------------------------------------------------------------------------------------
# The estate, as it is
# --------------------------------------------------------------------------------------


def gather(hosts: dict[str, str], runner=ssh) -> tuple[dict[str, list[Container]], dict[str, int]]:
    """Read each host's declared caps, and the cpuset actually in force.

    Two reads per host rather than one per container: `incus list --format json` carries
    every container's `expanded_config` (profiles merged), and one shell loop reads every
    cpuset. The declared cap therefore comes from the same merged view incus uses.
    """
    observed: dict[str, list[Container]] = {}
    host_cpus: dict[str, int] = {}
    for host, target in hosts.items():
        host_cpus[host] = int(runner(target, "nproc").strip() or 0)
        listing = json.loads(runner(target, "incus list --format json") or "[]")
        cpuset = runner(
            target,
            "for c in $(incus list --format csv -c n); do "
            'printf "%s %s\\n" "$c" "$(cat /sys/fs/cgroup/lxc.payload.$c/cpuset.cpus.effective 2>/dev/null)"; '
            "done",
        )
        effective: dict[str, str] = {}
        for line in cpuset.splitlines():
            name, _, spec = line.partition(" ")
            effective[name.strip()] = spec.strip()

        containers: list[Container] = []
        for entry in listing:
            name = entry.get("name", "")
            declared_raw = (entry.get("expanded_config") or {}).get("limits.cpu")
            declared = int(declared_raw) if declared_raw not in (None, "", "0") else None
            spec = effective.get(name, "")
            containers.append(
                Container(
                    name=name,
                    # `incus list --format json` says "Running"; the csv says "RUNNING".
                    # Normalise once here so the audit has one spelling to reason about.
                    state=entry.get("status", "").upper(),
                    declared=declared,
                    effective=count_cpus(spec) if spec else None,
                )
            )
        observed[host] = containers
    return observed, host_cpus


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

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-container-limits: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-container-limits: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        observed, host_cpus = gather(hosts)
    except Exception as error:
        print(f"check-container-limits: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-container-limits: reads hosts with key-based `ssh`; where the estate uses "
                "passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(observed, host_cpus)
    emit(result, ran=True)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    declared = sum(1 for cs in observed.values() for c in cs if c.declared)
    print(f"\ncheck-container-limits: {len(result.failures)} failure(s), {len(result.warnings)} warning(s), {declared} declared cap(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
