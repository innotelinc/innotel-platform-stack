#!/usr/bin/env python3
"""Judge the vault's disk, not just its mount.

WHY THIS EXISTS
---------------
The 2026-11-14 outage was the USB link: the drive left the bus mid-write and came back
as `/dev/sdc`, so the *mount* was what needed catching and `check-vault-mount.py` is the
counterweight for that. SMART is the other half, and it was never installed: the only
evidence about the disk was ext4, which is to say none until bytes are already lost.

`vault-disk-health.sh` on `i1` is the human-facing report — it prints, it logs, it
alerts on failure. This is its scheduled counterpart, run from the Cerulean edge with
the rest of the estate checks, so the numbers reach Prometheus and the dashboard rather
than a log nobody opens. It reads the disk by **LABEL**, never `/dev/sdX`: the letter is
exactly what the incident proved unstable.

THE RULES
---------
1. **No block device with the label** → exit 2. The disk is absent; nothing can be
   judged, and an absent disk is never a pass.
2. **SMART unreadable through the bridge** → exit 2. `-d sat` is what makes the
   enclosure's USB-SATA bridge answer; a disk that will not answer is unknown, not well.
3. **Health not PASSED, or any reallocated/pending/uncorrectable/reported sector** →
   failure. Those counts do not go back down; a non-zero one is lost data.
4. **UDMA CRC errors** → warning. That is the cable or the bridge, not the platters,
   and it is the one number that would have shown the USB link degrading.
5. **Temperature** → warning at 50 °C, failure at 55 °C. The drive has already run at
   43 °C against a 45 °C threshold (`In_the_past`), so this is not a theoretical line.

It also publishes the numbers themselves — `innotel_estate_vault_disk_*` — because a
green tick is not diagnosable and the dashboard should show the temperature rising
before it crosses anything.

Usage:
    ./scripts/check-vault-disk.py
    ./scripts/check-vault-disk.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-vault-disk.py

Exit codes: 0 = the disk is healthy, 1 = a finding, 2 = the disk or host could not be
read (never silently a pass).
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
from estate_check import Audit, Finding, read_host, ssh, write_prom  # noqa: E402

CHECK = "vault_disk"

#: The host that carries the drive, and the address the estate reaches it on.
HOST_NAME = "i1"
HOST = "root@192.168.1.51"

LABEL = "VAULT"
#: SATA behind a USB-SATA bridge; override with SMART_DTYPE when the disk moves to a
#: real port (the attribute page is different and would read as "unreadable").
SMART_DTYPE = os.environ.get("SMART_DTYPE", "sat")

TEMP_WARN = float(os.environ.get("TEMP_WARN", "50"))
TEMP_CRIT = float(os.environ.get("TEMP_CRIT", "55"))
CRC_WARN = int(os.environ.get("CRC_WARN", "1"))

#: The attributes that mean data has been lost or cannot be read, and the CRC counter
#: that is about the link rather than the platters.
BAD_IDS = {
    "5": "Reallocated_Sector_Ct",
    "187": "Reported_Uncorrect",
    "197": "Current_Pending_Sector",
    "198": "Offline_Uncorrectable",
}

PROBE = (
    f'dev=$(readlink -f /dev/disk/by-label/{LABEL} 2>/dev/null); echo "DEV=$dev"; '
    '[ -n "$dev" ] && [ -b "$dev" ] || { echo "NO_DEVICE"; exit 0; }; '
    f'smartctl -H -d {SMART_DTYPE} "$dev" 2>&1; echo "@@"; '
    f'smartctl -i -d {SMART_DTYPE} "$dev" 2>&1; echo "@@"; '
    f'smartctl -A -d {SMART_DTYPE} "$dev" 2>&1'
)


@dataclass
class Reading:
    """What SMART said, kept as the raw text plus the few fields the rules use."""

    device: str = ""
    health: str = ""
    model: str = ""
    serial: str = ""
    attributes: dict[str, float] = field(default_factory=dict)
    raw_text: str = ""


def _number(text: str) -> float | None:
    """The first integer in a SMART raw-value cell ("5510 (207 0 0)" → 5510)."""
    match = re.search(r"\d+", text)
    return float(match.group()) if match else None


def parse(text: str) -> Reading:
    """Split the probe's `@@`-delimited output. Pure, for the tests."""
    parts = text.split("@@")
    reading = Reading(raw_text=text)

    head = parts[0] if parts else ""
    match = re.search(r"^DEV=(.*)$", head, re.MULTILINE)
    reading.device = match.group(1).strip() if match else ""
    match = re.search(
        r"SMART overall-health self-assessment test result:\s*(\S+)", head
    )
    reading.health = match.group(1).strip() if match else ""

    info = parts[1] if len(parts) > 1 else ""
    for key in ("Device Model", "Model Number", "Product"):
        match = re.search(rf"^{key}:\s*(.+)$", info, re.MULTILINE)
        if match:
            reading.model = match.group(1).strip()
            break
    match = re.search(r"^Serial Number:\s*(.+)$", info, re.MULTILINE)
    reading.serial = match.group(1).strip() if match else ""

    attrs = parts[2] if len(parts) > 2 else ""
    for line in attrs.splitlines():
        match = re.match(r"^\s*(\d+)\s+\S+\s+0x[0-9a-fA-F]+\s+(.*)$", line)
        if not match:
            continue
        attr_id, rest = match.group(1), match.group(2).split()
        if not rest:
            continue
        # Column order after the flag word: value, worst, threshold, type, updated,
        # when_failed, RAW…  Seagate appends extra numbers to some raw cells
        # (`35 (207 0 0)`), so taking the last token reads 0 °C for a 35 °C disk.
        raw_cell = rest[6] if len(rest) > 6 else rest[-1]
        value = _number(raw_cell)
        if value is not None:
            reading.attributes[attr_id] = value
    return reading


def gauge(name: str, reading: Reading, value: float) -> str:
    """One labelled sample, so a value can be read back to the disk it came from."""
    labels = f'device="{reading.model or "unknown"}",serial="{reading.serial or "unknown"}"'
    return f"{name}{{{labels}}} {value:g}"


def audit(reading: Reading) -> tuple[Audit, str]:
    """Judge one reading, and build the extra series for it. Pure, for the tests."""
    out = Audit()

    if not reading.device:
        out.findings.append(
            Finding(
                "no_device",
                "fail",
                f"no block device is labelled {LABEL} — the drive is absent, so nothing "
                "about its health can be said",
            )
        )
        return out, ""

    if not reading.health and not reading.attributes:
        out.findings.append(
            Finding(
                "smart_unreadable",
                "fail",
                f"SMART answered nothing for {reading.device} — the bridge ({SMART_DTYPE}) "
                "or the disk is not answering, which is unknown rather than well",
            )
        )
        return out, ""

    whose = f"{reading.model or 'unknown model'} ({reading.serial or 'no serial'})"

    if reading.health and reading.health.upper() != "PASSED":
        out.findings.append(
            Finding(
                "health_failed",
                "fail",
                f"overall SMART health is '{reading.health}' on {whose}",
            )
        )

    for attr_id, label in BAD_IDS.items():
        value = reading.attributes.get(attr_id)
        if value:
            out.findings.append(
                Finding(
                    f"attr_{attr_id}",
                    "fail",
                    f"{label}={value:g} on {whose} — the drive has already lost or cannot "
                    "read sectors",
                )
            )

    crc = reading.attributes.get("199")
    if crc is not None and crc >= CRC_WARN:
        out.findings.append(
            Finding(
                "udma_crc",
                "warn",
                f"UDMA_CRC_Error_Count={crc:g} on {whose} — the SATA link through the "
                "enclosure has errored; this is the cable or the bridge, not the platters",
            )
        )

    temp = reading.attributes.get("194", reading.attributes.get("190"))
    if temp is not None:
        if temp >= TEMP_CRIT:
            out.findings.append(
                Finding(
                    "temperature_critical",
                    "fail",
                    f"{temp:g} C (>= {TEMP_CRIT:g} C) on {whose}",
                )
            )
        elif temp >= TEMP_WARN:
            out.findings.append(
                Finding(
                    "temperature_warn",
                    "warn",
                    f"{temp:g} C (>= {TEMP_WARN:g} C) on {whose}",
                )
            )

    extra = []
    if temp is not None:
        extra.append(gauge("innotel_estate_vault_disk_temperature_celsius", reading, temp))
    hours = reading.attributes.get("9")
    if hours is not None:
        extra.append(gauge("innotel_estate_vault_disk_power_on_hours", reading, hours))
    for attr_id in ("5", "197", "198", "187", "199"):
        value = reading.attributes.get(attr_id)
        if value is not None:
            extra.append(gauge(f"innotel_estate_vault_disk_attribute_{attr_id}", reading, value))

    header = (
        "# HELP innotel_estate_vault_disk_temperature_celsius The vault disk's current temperature\n"
        "# TYPE innotel_estate_vault_disk_temperature_celsius gauge\n"
        "# HELP innotel_estate_vault_disk_power_on_hours Hours the vault disk has been powered on\n"
        "# TYPE innotel_estate_vault_disk_power_on_hours gauge\n"
        "# HELP innotel_estate_vault_disk_attribute_199 UDMA CRC errors: the cable or bridge, not the platters\n"
        "# TYPE innotel_estate_vault_disk_attribute_199 gauge\n"
    )
    return out, header + "\n".join(extra) if extra else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=HOST, help=f"ssh target (default: {HOST})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool, extra: str = "") -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=extra or None)
            except OSError as error:
                print(f"check-vault-disk: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-vault-disk: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        text = read_host(HOST_NAME, args.host, PROBE, ssh)
    except Exception as error:  # noqa: BLE001 — an unreachable host is one finding
        print(f"check-vault-disk: could not read {HOST_NAME}: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-vault-disk: reads the host with key-based `ssh`; where the estate "
                "uses passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    if "NO_DEVICE" in text or "smartctl: command not found" in text:
        result = Audit([Finding(
            "no_device",
            "fail",
            f"no block device is labelled {LABEL}, or smartctl is not installed on {HOST_NAME} "
            "— the disk cannot be judged",
        )])
        emit(result, ran=True)
        print(f"FAIL {result.findings[0].message}")
        return 1

    reading = parse(text)
    result, extra = audit(reading)
    emit(result, ran=True, extra=extra)

    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    if result.ok:
        temp = reading.attributes.get("194", reading.attributes.get("190"))
        print(
            f"ok   {reading.model} ({reading.serial}) at {reading.device}: health "
            f"{reading.health or 'unknown'}, {temp:g} C" if temp is not None else
            f"ok   {reading.model} ({reading.serial}) at {reading.device}: health "
            f"{reading.health or 'unknown'}"
        )
    print(
        f"\ncheck-vault-disk: {len(result.failures)} failure(s), "
        f"{len(result.warnings)} warning(s)"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
