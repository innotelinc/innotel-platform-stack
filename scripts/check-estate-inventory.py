#!/usr/bin/env python3
"""Fail when the estate has an incus host or storage pool the docs do not know about.

WHY THIS EXISTS
---------------
The placement page said "three Incus hosts" and listed none of i2's storage beyond
`tank`. Both were wrong: a fourth host, `i4` (`.54`), was reachable and trusted by the
other three, and i2 had grown a second, 465 GiB pool (`main-pool`) that its `default`
and `docker` profiles point at. Neither was hidden — `incus remote list` and
`incus storage list` name them on any host — but nothing compared them against what
the estate believes it has, so a host or a pool could appear and the docs would keep
describing the estate that used to exist.

This is that comparison. The inventory below is the estate as it should be; anything
on a host that is not in it is reported, and so is a known pool that has gone missing.

THE RULES
---------
1. **A host the estate trusts is a host the estate should know.** Every `incus`
   remote configured on any host, resolved to its address, must be in the inventory.
   An address with no entry is a failure — that is what an unaccounted fourth host
   looks like from the inside.
2. **A storage pool is estate structure, not a detail.** Any pool on a known host
   that the inventory does not name is a failure; a pool it names that is gone is a
   warning (a pool can be retired on purpose, and the page then says so).
3. **The bench is read but not required.** `i4` is a test bench with no estate
   container; if it is powered off the check reports that as a warning and carries
   on, because a bench being down is not an estate problem. Its absence does not
   excuse an unknown host or pool anywhere else.
4. *(not a rule, a note)* `i4`'s container and pools are still checked when it is up:
   a bench that grew a new pool is exactly how a stray pool reaches production.

Usage:
    ./scripts/check-estate-inventory.py
    ./scripts/check-estate-inventory.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-estate-inventory.py

Exit codes: 0 = the estate is the one the inventory describes, 1 = something is not
in it (or has gone), 2 = a required host could not be read (never silently a pass).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, ssh, write_prom  # noqa: E402

CHECK = "estate_inventory"

#: The estate as it should be: name → address, and the storage pools each host may have.
#: Cross-checked against docs/container-placement.md — a change here is a change there.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: address → name, derived from HOSTS, so the two cannot drift.
ADDRESS_OF: dict[str, str] = {name: target.split("@", 1)[-1] for name, target in HOSTS.items()}
NAME_OF_ADDRESS: dict[str, str] = {address: name for name, address in ADDRESS_OF.items()}

#: name → the storage pools that host is allowed to have. A pool not listed is a failure.
POOLS: dict[str, set[str]] = {
    "i1": {"incus"},
    "i2": {"tank", "main-pool"},
    "i3": {"tank"},
    "i4": {"default", "tank"},
}

#: Hosts whose absence is a warning rather than an exit 2 — the test bench and nothing else.
OPTIONAL: set[str] = {"i4"}

#: What each host is for. Reported, so a finding says what the host is.
ROLE: dict[str, str] = {
    "i1": "estate (edge — every address is dialled here)",
    "i2": "estate (apps)",
    "i3": "estate (light)",
    "i4": "bench (routed-networking tests; no estate container)",
}


@dataclass
class HostView:
    """One host as it is: the addresses it trusts, the pools it has, and whether it answered."""

    name: str
    remotes: list[str] = field(default_factory=list)  # addresses of protocol=incus remotes
    pools: list[str] = field(default_factory=list)
    reachable: bool = True


def _remote_address(url: str) -> str | None:
    """The address an incus remote URL names, or None for a non-incus-API endpoint."""
    parsed = urlparse(url)
    return parsed.hostname or None


def audit(views: list[HostView]) -> Audit:
    """Compare what the hosts report against HOSTS / POOLS."""
    out = Audit()
    known_addresses = set(NAME_OF_ADDRESS)
    for view in views:
        if not view.reachable:
            if view.name in OPTIONAL:
                out.findings.append(
                    Finding("bench_unreachable", "warn", f"{view.name}: bench not reachable — {ROLE.get(view.name, '')}")
                )
            continue

        for address in sorted(set(view.remotes)):
            if address not in known_addresses:
                out.findings.append(
                    Finding(
                        "unknown_host",
                        "fail",
                        f"{view.name} trusts an incus host at {address} that the inventory does not name — "
                        f"a host or pool the estate has but the docs do not describe",
                    )
                )

        known = POOLS.get(view.name, set())
        for pool in sorted(set(view.pools) - known):
            out.findings.append(
                Finding(
                    "unknown_pool",
                    "fail",
                    f"{view.name} has storage pool '{pool}' the inventory does not name "
                    f"(known: {', '.join(sorted(known)) or 'none'}) — record it or retire it",
                )
            )
        for pool in sorted(known - set(view.pools)):
            out.findings.append(
                Finding(
                    "missing_pool",
                    "warn",
                    f"{view.name} no longer has the pool '{pool}' the inventory names",
                )
            )
    return out


# --------------------------------------------------------------------------------------
# The estate, as it is
# --------------------------------------------------------------------------------------


def gather(hosts: dict[str, str], optional: set[str], runner=ssh) -> list[HostView]:
    """Read each host's trusted incus remotes and its storage pools."""
    views: list[HostView] = []
    for name, target in hosts.items():
        try:
            remotes_csv = runner(target, "incus remote list --format csv")
            pools_csv = runner(target, "incus storage list --format csv -c n")
        except Exception:
            if name in optional:
                views.append(HostView(name=name, reachable=False))
                continue
            raise
        remotes = []
        for line in remotes_csv.splitlines():
            columns = line.split(",")
            if len(columns) < 3 or columns[2].strip() != "incus":
                continue
            address = _remote_address(columns[1].strip())
            if address:
                remotes.append(address)
        pools = [line.split(",")[0].strip() for line in pools_csv.splitlines() if line.strip()]
        views.append(HostView(name=name, remotes=remotes, pools=pools))
    return views


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    optional = set(OPTIONAL)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()
        optional = set()

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-estate-inventory: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-estate-inventory: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        views = gather(hosts, optional)
    except Exception as error:
        print(f"check-estate-inventory: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-estate-inventory: reads hosts with key-based `ssh`; where the estate uses "
                "passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(views)
    emit(result, ran=True)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    described = ", ".join(f"{v.name} ({ROLE.get(v.name, '?')})" for v in views if v.reachable)
    print(f"\ncheck-estate-inventory: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    print(f"  read: {described}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
