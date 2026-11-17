#!/usr/bin/env python3
"""Fail when a container that is *running* would not come back after a reboot.

WHY THIS EXISTS
---------------
`boot.autostart=false` is the quiet half of every move, and it always starts out right:
an instance is copied to a new host, the copy is left stopped with autostart cleared so a
reboot cannot produce two machines claiming one LAN address. The flag only becomes a
hazard when the move finishes and nobody clears it, because nothing is wrong while the
instance is up — every probe answers, every door serves, and the loss is scheduled for
the next reboot.

That is not hypothetical. After the 2026-11-15 rebalance **all nine** instances on `i2`
carried `boot.autostart=false`, and on 2026-11-17 the host rejoined the estate as a live
host: a reboot would have taken the 36-container media stack, its Prometheus (the one
these alerts run on), the NAS and the rest offline with nothing announcing it
(`docs/host-map.md` records the measurement; the nine were set to `true` that day).

`check-retired-instances.py` asserts the opposite direction for the instances that must
stay down — present, stopped, autostart off. Nothing asserted this one, and it cannot be
seen by looking at anything that is running, which is exactly the shape of finding this
estate collects: a correct-looking estate that a reboot turns into a different one.

THE RULES
---------
1. **A running instance must have `boot.autostart` on.** Unset counts as on: Incus reads
   a container with no value that way, and a row that says "unset" on a healthy estate
   would make every instance a finding. Off while running is a failure that names the
   host, the instance and the command that fixes it.
2. **A named exemption turns that failure into a note, never into silence.** `onyx` on
   `i1` is the estate's only instance that runs with the flag off on purpose: its object
   store is a USB disk passed through as `required: "false"`, so an automatic start
   without that disk comes up serving nothing. The exemption is keyed by host *and*
   instance — the same name elsewhere is still a failure — and it is published as a note
   so the deliberate case reaches the dashboard instead of reading as a missing row.
3. **A stopped instance with the flag off is not a finding.** That is what a parked copy
   or a rollback looks like, and `check-retired-instances.py` is the check that asserts it
   for the instances that must stay down.
4. **A host that cannot be read is exit 2, never a pass.** A powered-off host is precisely
   the state in which somebody sets this flag, so its silence is not evidence.

Usage:
    ./scripts/check-container-autostart.py
    ./scripts/check-container-autostart.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-container-autostart.py      # where the estate uses passwords
    ./scripts/check-container-autostart.py --hosts probe=root@192.168.1.50

Exit codes: 0 = every running instance would come back after a reboot, 1 = one would not,
2 = a host could not be read (never silently a pass).
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
from estate_check import Audit, Finding, read_host, ssh, write_prom  # noqa: E402

CHECK = "container_autostart"

HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: host -> instance -> why it is allowed to run with `boot.autostart` off. Empty values
#: are not allowed by the test, so an exemption always carries the reason it exists.
EXEMPT: dict[str, dict[str, str]] = {
    "i1": {
        "onyx": (
            "the object store's disk is a USB device passed through as required=\"false\", "
            "so an automatic start without it comes up serving nothing — it is started by "
            "hand once the disk is there (docs/host-map.md)"
        ),
    },
}

#: What Incus reads as "do not start this on boot". Anything else — including an absent
#: key, which is the default `true` — counts as on.
OFF = {"false", "0", "no", "off"}


@dataclass(frozen=True)
class Instance:
    """One instance as the host reports it."""

    name: str
    state: str
    autostart: str | None


def autostart_on(value: str | None) -> bool:
    """Whether a `boot.autostart` value starts the instance at boot.

    An unset key is `true` in Incus, and reading it as "not stated, so not my problem"
    is how the whole estate could be reported clean while nothing starts.
    """
    return (value or "").strip().lower() not in OFF


def gather(hosts: dict[str, str], runner=ssh) -> dict[str, list[Instance]]:
    """Read every instance on every host in one `incus list` per host.

    `--format json` carries `expanded_config` — profiles merged — so the flag read here is
    the one the host will act on at boot rather than only the one written on the instance.
    """
    views: dict[str, list[Instance]] = {}
    for host, target in hosts.items():
        listing = json.loads(read_host(host, target, "incus list --format json", runner) or "[]")
        views[host] = [
            Instance(
                name=entry.get("name", ""),
                state=(entry.get("status") or "").upper(),
                autostart=(entry.get("expanded_config") or {}).get("boot.autostart"),
            )
            for entry in listing
            if entry.get("name")
        ]
    return views


def audit(views: dict[str, list[Instance]]) -> Audit:
    """The four rules, over data the caller already has. Pure, so the rules are the test."""
    out = Audit()
    for host, instances in views.items():
        for instance in sorted(instances, key=lambda i: i.name):
            if instance.state != "RUNNING" or autostart_on(instance.autostart):
                continue
            where = f"{host} {instance.name}"
            reason = EXEMPT.get(host, {}).get(instance.name)
            if reason:
                out.findings.append(
                    Finding(
                        "autostart_off_deliberate",
                        "note",
                        f"{where}: running with boot.autostart={instance.autostart}, so a "
                        f"reboot leaves it down — deliberate: {reason}",
                    )
                )
                continue
            out.findings.append(
                Finding(
                    "autostart_off",
                    "fail",
                    f"{where}: running with boot.autostart={instance.autostart or 'unset'}, so "
                    f"a reboot leaves it down while everything else comes back — "
                    f"`incus config set {instance.name} boot.autostart true`",
                )
            )
    return out


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
            if name.strip():
                hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran)
        except OSError as error:
            print(f"check-container-autostart: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-container-autostart: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        views = gather(hosts)
    except Exception as error:
        print(f"check-container-autostart: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-container-autostart: reads hosts with key-based `ssh`; where the estate "
                "uses passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(views)
    emit(result, ran=True)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    running = sum(1 for instances in views.values() for i in instances if i.state == "RUNNING")
    print(
        f"\ncheck-container-autostart: {len(result.failures)} failure(s), "
        f"{len(result.warnings)} warning(s) — {running} running instance(s) on "
        f"{len(views)} host(s)"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
