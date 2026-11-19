#!/usr/bin/env python3
"""Fail when a retired incus instance is back on a host that must not carry it.

WHY THIS EXISTS
---------------
`monarch` was moved off `i1` on 2026-11-16 because one spindle could not carry it:
37 containers scanning drove `/proc/pressure/io` to 94-97 % and starved `mail`,
`ontrak` and `atheniq` on the same disk. The source instance was renamed
`monarch-retired`, stopped, and given `boot.autostart=false`; the dataset stayed as
a rollback. That rename is the control that actually holds: every stale
`incus start|exec|copy monarch` on `i1` now fails with *Instance not found*
instead of quietly reloading the spindle.

Two things can undo it, and neither is visible in a config diff:

  * the instance is **started** again — by hand, by a timer, or by a script that
    learned the old name and recreated it;
  * a **new instance with the live name** appears on the host, which looks like a
    fresh deployment and behaves like the outage returning.

`incus-guard` cannot cover this: it is a cooperative lease, so a session that does
not ask for it is unaffected, and it says nothing about the *declaration* being
right. This check reads the hosts instead, which is the only place the answer is.

THE RULES
---------
1. **A retired instance must exist, be `STOPPED`, and have `boot.autostart=false`.**
   A running retired instance is the workload back on the host that could not carry
   it; a missing one means the rollback that justified the move is gone. Autostart
   is separate from state because a reboot is where a stopped instance comes back.
   The table is empty as of 2026-11-18 (the reclaim below), so today this rule has
   no rows to assert and rule 2 carries the move on its own.
2. **A forbidden name must not exist on that host at all.** `monarch` on `i1` is the
   concrete case: the rename is what makes stale automation fail closed, and a
   recreated instance with that name removes the protection even while stopped.
3. **A host that cannot be read is exit 2, never a pass.** A powered-off host is
   exactly the state in which somebody starts the instance again.

Usage:
    ./scripts/check-retired-instances.py
    ./scripts/check-retired-instances.py --prom /path/to/textfile
    SSHPASS='...' ./scripts/check-retired-instances.py

Exit codes: 0 = every declaration holds, 1 = one does not, 2 = a host could not be
read (an unreadable host is not evidence of anything).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, HostUnreadable, read_host, ssh, write_prom  # noqa: E402

CHECK = "retired_instances"

HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: host -> instances that must stay stopped, with the reason the check is asserting it.
#: Empty since 2026-11-18: `monarch-retired` was reclaimed on `i1` following
#: `docs/incus-retired-reclaim.md` (the live copy had run on `i2` for two days, then the
#: instance and its 25.4 GB dataset were deleted — `incus list` and `zfs list` on `i1`
#: no longer name it). That runbook says the row goes in the same change as the reclaim,
#: because a check asserting a rollback nobody kept reads as noise, and noise is how a
#: real regression goes unnoticed. Recomputation is not an option here: there is nothing
#: left to assert, so the enforceable half of the move lives in FORBIDDEN below.
RETIRED: dict[str, dict[str, str]] = {}

#: host -> names that must not exist on that host at all, stopped or not. The rename is
#: the enforceable half of the move; a recreated `monarch` removes it.
FORBIDDEN: dict[str, dict[str, str]] = {
    "i1": {
        "monarch": (
            "the live name must not exist on i1: stale `incus start|exec|copy monarch` "
            "has to fail closed against the retired instance"
        ),
    },
}


@dataclass(frozen=True)
class Instance:
    """One instance as the host reports it."""

    name: str
    state: str
    autostart: str | None


def gather(target: str, names: tuple[str, ...]) -> dict[str, Instance]:
    """Read `incus list` once, then each named instance's autostart flag."""
    listing = read_host(target.split("@")[-1], target, "incus list -c ns -f csv", ssh)
    found: dict[str, Instance] = {}
    for line in listing.splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        name, state = parts[0].strip(), parts[1].strip()
        autostart: str | None = None
        if name in names:
            try:
                autostart = read_host(
                    target.split("@")[-1], target,
                    f"incus config get {name} boot.autostart", ssh,
                ).strip()
            except HostUnreadable:
                autostart = None
        found[name] = Instance(name=name, state=state, autostart=autostart)
    return found


def audit(views: dict[str, dict[str, Instance]]) -> Audit:
    """The three rules, over data the caller already has. Pure, so the rules are the test."""
    out = Audit()
    for host, expected in RETIRED.items():
        instances = views.get(host, {})
        for name, reason in expected.items():
            instance = instances.get(name)
            if instance is None:
                out.findings.append(Finding(
                    "retired_missing", "fail",
                    f"{host} does not have the retired instance {name} — {reason}"))
                continue
            if instance.state.upper() != "STOPPED":
                out.findings.append(Finding(
                    "retired_running", "fail",
                    f"{host}: {name} is {instance.state}, not STOPPED — {reason}"))
            if (instance.autostart or "").lower() != "false":
                out.findings.append(Finding(
                    "retired_autostart", "fail",
                    f"{host}: {name} has boot.autostart={instance.autostart or 'unset'}, so a "
                    f"reboot starts it — set `incus config set {name} boot.autostart false`"))
    for host, forbidden in FORBIDDEN.items():
        instances = views.get(host, {})
        for name, reason in forbidden.items():
            if name in instances:
                out.findings.append(Finding(
                    "forbidden_instance", "fail",
                    f"{host} has an instance named {name} ({instances[name].state}) — {reason}"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            if name.strip():
                hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran)
        except OSError as error:
            print(f"check-retired-instances: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-retired-instances: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    views: dict[str, dict[str, Instance]] = {}
    try:
        for name, target in hosts.items():
            wanted = tuple(RETIRED.get(name, {})) + tuple(FORBIDDEN.get(name, {}))
            views[name] = gather(target, wanted)
    except HostUnreadable as error:
        print(f"check-retired-instances: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    result = audit(views)
    emit(result, ran=True)
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(f"check-retired-instances: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
