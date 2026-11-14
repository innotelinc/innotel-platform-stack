#!/usr/bin/env python3
"""Fail when the artifact vault's path is not the vault.

WHY THIS EXISTS
---------------
On 2026-11-14 the vault's USB drive dropped off the bus and came back as `/dev/sdc`.
`srv-vault.mount` went inactive, `/srv/vault` stayed behind as an ordinary *empty*
directory, and every consumer of that path kept working: `vault-ro.service` had already
bound the path, nginx answered `200` on `:8080`, the NFS export was still listed, and
`ls /srv/vault` printed nothing rather than an error. Nothing in the estate could tell
"the vault is empty" from "the vault is gone", and the first report of the outage was a
person noticing.

`nofail` in fstab is correct — a boot must not wait for a USB disk — but it is exactly
what makes an absent drive look like a healthy mountpoint downstream. This check is the
counterweight: it asks whether the path is a mount, whether the mount is *the vault*
(by a sentinel file that lives on the filesystem), and whether the read-only view the
guests bind is still there and still read-only.

A warning is not enough here. A missing vault is not degraded service, it is empty
service: guests read zero files and callers get empty listings that look like answers.

THE RULES
---------
1. **Not a mountpoint** → failure. The disk is absent; nginx/NFS are publishing an
   empty directory.
2. **Mounted, but no sentinel** → failure. It is a filesystem at the path, not the
   artifact vault (a blank replacement disk, a wrong label).
3. **Read-only view missing or writable** → failure. The guests' guarantee is gone even
   if the vault itself is fine.
4. **A host that cannot be read is not a pass** → exit 2, like every check here.

Usage:
    ./scripts/check-vault-mount.py
    ./scripts/check-vault-mount.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-vault-mount.py

Exit codes: 0 = the vault is mounted and its view is read-only, 1 = it is not,
2 = the host could not be read (never silently a pass).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, read_host, ssh, write_prom  # noqa: E402

CHECK = "vault_mount"

#: The host that carries the drive, and the address the estate reaches it on.
HOST_NAME = "i1"
HOST = "root@192.168.1.51"

VAULT = "/srv/vault"
ROVIEW = "/srv/vault-ro"
SENTINEL = ".vault-id"
SENTINEL_TEXT = "innotel-vault-v1"

#: One read, three questions. `findmnt --target` resolves the mount that *contains* the
#: path, so a plain directory with a parent filesystem prints that parent's device; the
#: sentinel is what proves it is our filesystem, and `TARGET` is what proves the path is
#: the mountpoint itself rather than a directory inside one.
PROBE = (
    f"findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS --target {VAULT}; echo '@@'; "
    f"cat {VAULT}/{SENTINEL} 2>/dev/null; echo '@@'; "
    f"findmnt -no TARGET,SOURCE,OPTIONS --target {ROVIEW}; echo '@@'; "
    f"ls -A {VAULT} 2>/dev/null | wc -l"
)


@dataclass
class Reading:
    """What the host said, as three questions and the raw text that answered them."""

    mount_target: str = ""
    mount_source: str = ""
    mount_fstype: str = ""
    mount_options: str = ""
    sentinel: str = ""
    ro_target: str = ""
    ro_source: str = ""
    ro_options: str = ""
    entries: int | None = None


def parse(text: str) -> Reading:
    """Split the probe's `@@`-delimited output into a Reading. Pure, for the tests."""
    parts = text.split("@@")
    reading = Reading()

    mount = parts[0].strip() if len(parts) > 0 else ""
    if mount:
        fields = mount.split()
        # findmnt prints TARGET SOURCE FSTYPE OPTIONS; SOURCE may contain spaces only
        # for a network mount, which this one is not, so positional parsing is stable.
        reading.mount_target = fields[0]
        reading.mount_source = fields[1] if len(fields) > 1 else ""
        reading.mount_fstype = fields[2] if len(fields) > 2 else ""
        reading.mount_options = " ".join(fields[3:])

    reading.sentinel = parts[1].strip() if len(parts) > 1 else ""

    ro = parts[2].strip() if len(parts) > 2 else ""
    if ro:
        fields = ro.split()
        reading.ro_target = fields[0]
        reading.ro_source = fields[1] if len(fields) > 1 else ""
        reading.ro_options = " ".join(fields[2:])

    if len(parts) > 3 and parts[3].strip().isdigit():
        reading.entries = int(parts[3].strip())
    return reading


def audit(reading: Reading) -> Audit:
    """Judge one reading. Pure, so the rules are the thing under test."""
    out = Audit()

    mounted_here = reading.mount_target == VAULT and reading.mount_fstype not in ("", "overlay")
    if not mounted_here:
        out.findings.append(
            Finding(
                "not_mounted",
                "fail",
                f"{VAULT} is not a mountpoint — the vault drive is absent and "
                f"{reading.entries if reading.entries is not None else 'an unknown number of'} "
                f"entries are being served from the empty directory",
            )
        )
        # Nothing below can mean anything: the sentinel and the ro view belong to the
        # filesystem that is not there.
        return out

    if reading.sentinel != SENTINEL_TEXT:
        out.findings.append(
            Finding(
                "wrong_filesystem",
                "fail",
                f"{VAULT} is mounted ({reading.mount_source}, {reading.mount_fstype}) but does not "
                f"carry the '{SENTINEL_TEXT}' sentinel — this is not the artifact vault",
            )
        )
        return out

    if reading.ro_target != ROVIEW:
        out.findings.append(
            Finding(
                "no_ro_view",
                "fail",
                f"{ROVIEW} is not a mountpoint — the guests' read-only view is gone",
            )
        )
    elif "ro" not in reading.ro_options.split(","):
        out.findings.append(
            Finding(
                "ro_view_writable",
                "fail",
                f"{ROVIEW} is mounted but not read-only ({reading.ro_options}) — the guests' "
                "guarantee is gone",
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=HOST, help=f"ssh target (default: {HOST})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-vault-mount: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-vault-mount: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        text = read_host(HOST_NAME, args.host, PROBE, ssh)
    except Exception as error:  # noqa: BLE001 — an unreachable host is one finding
        print(f"check-vault-mount: could not read {HOST_NAME}: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-vault-mount: reads the host with key-based `ssh`; where the estate "
                "uses passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    reading = parse(text)
    result = audit(reading)
    emit(result, ran=True)

    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    if result.ok:
        print(
            f"ok   {VAULT} on {reading.mount_source} ({reading.mount_fstype}, "
            f"{reading.entries} entries); {ROVIEW} read-only"
        )
    print(f"\ncheck-vault-mount: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
