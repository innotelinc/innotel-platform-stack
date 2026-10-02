#!/usr/bin/env python3
"""Fail when a dialled address does not answer, and warn when it answers slowly.

WHY THIS EXISTS
---------------
The estate reaches every service **by address** — the A records, the published ports and
the consumers all name `192.168.1.x`. `check-container-addresses.py` proves a container
*has* the address it is supposed to. Nothing proved the address **answers**, and those are
different failures:

  * a container that came up on `.20` instead of `.21` has the wrong address (caught);
  * a container that kept `.21` while the host stopped proxy-ARPing for it, or a route or
    a firewall changed, has the *right* address and is still unreachable (not caught).

The second is the same outage one layer down — the A record resolves to the right answer,
for a host that does not answer. This check is the difference: it pings every address the
estate dials, so "the address is there" and "the address answers" are both watched.

It also carries the number that made the 2026-10-02 `i4` WiFi **power-save** regression
visible. `i4`'s only uplink is WiFi, the edge sits behind it, and with power save on an
*idle* container answered in **100–900 ms** while the busy edge looked fine: the radio held
frames until the next DTIM beacon (~312 ms). Turning it off
(`systemd/wifi-powersave-off.service`) put every container back at **~7 ms**. The lesson is
not "turn power save off" — it is that the regression was **felt, not measured**, and a
host re-image or a driver default can put it back. See `docs/container-placement.md`
§The i4 host and §The i4 host's trust.

WHAT IT PROBES
--------------
Every dialled address: each estate host (`HOSTS`) **and** every container in the address
table (`EXPECTED`, read from `check-container-addresses.py` so the two cannot drift). The
probe is ICMP from wherever the check runs — on the edge, which is the place the estate is
dialled from and the only place the `i4` WiFi hop is on the path.

One ping per sample after an idle gap, because that is the state power save shows up in:
a ping every 200 ms keeps the radio awake and would measure the good case with power save
on. The median of the samples is the reading, and it is published so the number is
visible, not only the verdict.

It is deliberately not throughput: the symptom was latency, throughput on a 2-vCPU box is
dominated by the box rather than the hop, and a throughput probe needs a server and a load
to mean anything — three reasons a 30-minute check should not be made expensive to be
wrong.

THE RULES
---------
1. **Every dialled address answers.** ICMP from the edge, one ping per sample after an
   idle gap. An address that answers nothing is probed a **second round** before it is
   called dark: one lost burst on a WiFi hop (the first frame after the radio idles,
   before ARP settles) is not an outage, and a check that pages on one gets switched off.
   Silent across both rounds is a **failure**. Losing some of the replies is a note, not a
   warning, for the same reason — it is printed, but it does not page.
2. **A slow address is a warning.** A median at or above `SLOW_MS` (200 ms, ~25× the
   measured ~7 ms) means something is holding frames, and the likely something is the
   uplink's power save.
3. **An address that does not answer usefully is a failure.** Median at or above
   `UNUSABLE_MS`, near the ping timeout.
4. **A fast ping is not a pass for the service.** This says the path is quick and lit; it
   says nothing about whether the service on the port is healthy. The other checks, and
   the services themselves, own that.

Usage:
    ./scripts/check-address-latency.py
    ./scripts/check-address-latency.py --prom /path/to/textfile
    ./scripts/check-address-latency.py --samples 5 --idle 0.8

Exit codes: 0 = every dialled address answers promptly, 1 = one does not, 2 = the check
could not run (no `ping` on PATH — never silently a pass).
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, write_prom  # noqa: E402

CHECK = "address_latency"

#: Where each estate host is reachable. The probe runs where the check runs — on the
#: Cerulean edge, which is the only place the `i4` WiFi hop is on the path to all of them.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: One ping per sample, after an idle gap. The gap is what makes power save visible: a
#: radio kept awake answers as if power save were off.
SAMPLES = 3
IDLE_SECONDS = 0.5

#: Median RTT at/above which an address is reported. Measured healthy is ~7 ms; the
#: power-save case was 100–900 ms, so 200 ms is far above the good state and the bad one.
SLOW_MS = 200.0
#: Median at/above which the address is not answering usefully (near the 2 s ping timeout).
UNUSABLE_MS = 1000.0

_RTT = re.compile(r"time=([0-9.]+)\s*ms")


@dataclass(frozen=True)
class Target:
    """One address the estate dials, and what it is for."""

    name: str
    address: str
    kind: str  # "host" | "service"
    where: str = ""  # the host a service lives on
    role: str = ""  # what it is, for the finding

    @property
    def key(self) -> str:
        """A stable id — container names are unique, but the host is kept for clarity."""
        return f"host:{self.name}" if self.kind == "host" else f"service:{self.where}/{self.name}"

    @property
    def label(self) -> str:
        return f"{self.name} the host" if self.kind == "host" else f"{self.name} on {self.where}"


@dataclass
class Reading:
    """One address's samples, and how many pings were actually sent at it."""

    target: Target
    rtts: list[float]
    attempted: int = 0

    @property
    def lost(self) -> int:
        return max(0, self.attempted - len(self.rtts))


def _address_table() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """`EXPECTED` and `ROLES` from `check-container-addresses.py`.

    Read from the address check rather than copied, so "every address the estate dials"
    means the same set in both places: a container added to the table is probed here the
    time it is added there, and cannot be asserted-about in one check and dark in the
    other. The module is loaded by path (its filename has a hyphen); it has no side
    effects, so importing it is just reading its tables.
    """
    path = Path(__file__).resolve().parent / "check-container-addresses.py"
    spec = importlib.util.spec_from_file_location("check_container_addresses_for_latency", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.EXPECTED, module.ROLES


def targets(
    hosts: dict[str, str] = HOSTS,
    expected: dict[str, dict[str, str]] | None = None,
    roles: dict[str, str] | None = None,
) -> list[Target]:
    """Every address the estate dials: each host, and each container in the table."""
    expected, roles = _address_table() if expected is None else (expected, roles or {})
    out = [Target(name=name, address=target.split("@", 1)[-1], kind="host", role=roles.get(target.split("@", 1)[-1], "")) for name, target in hosts.items()]
    for host, containers in expected.items():
        for name, address in containers.items():
            out.append(Target(name=name, address=address, kind="service", where=host, role=roles.get(address, "")))
    return out


def probe_command(address: str, samples: int = SAMPLES, idle: float = IDLE_SECONDS) -> str:
    """The shell that produces `samples` single ICMP replies, each after an idle gap.

    One ping per invocation rather than `ping -c N`: consecutive pings train the radio, so
    only the first after a gap is a fair reading of an idle link.
    """
    return f"for _ in $(seq {samples}); do sleep {idle}; ping -c 1 -n -W 2 {address}; done 2>&1"


def parse_rtts(text: str) -> list[float]:
    """Every `time=<ms>` in a ping body, in order. A silent or failing ping yields []."""
    return [float(match) for match in _RTT.findall(text)]


def local(command: str, timeout: int = 60) -> str:
    """Run a command on this host (the check's own), returning its combined output."""
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout


def measure(
    targets: list[Target],
    runner: Callable[[str], str] = local,
    samples: int = SAMPLES,
    idle: float = IDLE_SECONDS,
    confirm: bool = True,
) -> list[Reading]:
    """Ping each target's address from here. `runner` is injectable for the tests.

    An address that answered **nothing** gets a second round before it is called dark. One
    lost burst on a WiFi hop — the first frame after the radio idles, before ARP settles —
    is not an outage, and a check that pages on one gets turned off the first week. Only an
    address silent across *both* rounds is a failure.
    """
    readings: list[Reading] = []
    for target in targets:
        rtts: list[float] = []
        attempted = 0
        for _ in range(2 if confirm else 1):
            rtts += parse_rtts(runner(probe_command(target.address, samples, idle)))
            attempted += samples
            if rtts:
                break
        readings.append(Reading(target=target, rtts=rtts, attempted=attempted))
    return readings


def audit(readings: list[Reading]) -> Audit:
    """Read the samples as findings. Pure, so what counts as dark or slow is under test."""
    out = Audit()
    for reading in sorted(readings, key=lambda r: (r.target.kind, r.target.name)):
        target = reading.target
        location = f"{target.label} ({target.address})"
        role = f" — {target.role}" if target.role else ""
        if not reading.rtts:
            out.findings.append(
                Finding(
                    "no_reply",
                    "fail",
                    f"{location}{role}: no ICMP reply in {reading.attempted} attempts — the "
                    f"address is dialled and is dark",
                )
            )
            continue
        # Partial loss is a note, not a warning: on this topology (routed NICs over a WiFi
        # uplink) a lost first burst is normal, and a warning here would page every run.
        # It is still printed, because a link that starts dropping more is worth seeing.
        if reading.lost:
            out.findings.append(
                Finding(
                    "lossy",
                    "note",
                    f"{location}{role}: {reading.lost} of {reading.attempted} pings "
                    f"unanswered — the reading is the median of the replies",
                )
            )
        median = statistics.median(reading.rtts)
        if median >= UNUSABLE_MS:
            out.findings.append(
                Finding(
                    "unusable",
                    "fail",
                    f"{location}{role}: answers in {median:.0f} ms — the address is dialled and "
                    f"effectively unreachable (fast: ~7 ms)",
                )
            )
        elif median >= SLOW_MS:
            out.findings.append(
                Finding(
                    "slow",
                    "warn",
                    f"{location}{role}: answers in {median:.0f} ms — ~7 ms is healthy; a radio "
                    f"holding frames until the next beacon is the usual cause "
                    f"(see systemd/wifi-powersave-off.service)",
                )
            )
    return out


def latency_series(readings: list[Reading]) -> str:
    """The per-address RTT gauge, so the reading is visible and not only the verdict."""
    lines = [
        f"# HELP {METRIC}_address_latency_ms Median ICMP round-trip to a dialled address",
        f"# TYPE {METRIC}_address_latency_ms gauge",
    ]
    for reading in sorted(readings, key=lambda r: (r.target.kind, r.target.name)):
        if reading.rtts:
            target = reading.target
            lines.append(
                f'{METRIC}_address_latency_ms{{check="{CHECK}",kind="{target.kind}",name="{target.name}",address="{target.address}"}} '
                f"{statistics.median(reading.rtts):.3f}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--samples", type=int, default=SAMPLES, help=f"single pings per address (default {SAMPLES})")
    parser.add_argument("--idle", type=float, default=IDLE_SECONDS, help=f"seconds to wait before each ping (default {IDLE_SECONDS})")
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
                write_prom(args.prom, CHECK, result, ran, extra=latency_series(readings) if readings else None)
            except OSError as error:
                print(f"check-address-latency: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ping") is None:
        print("check-address-latency: no ping on PATH — this check measures ICMP", file=sys.stderr)
        emit(None, ran=False)
        return 2

    probe = targets(hosts)
    try:
        readings = measure(probe, samples=args.samples, idle=args.idle)
    except Exception as error:  # a probe that cannot run is not a pass
        print(f"check-address-latency: could not probe an address: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    result = audit(readings)
    emit(result, ran=True, readings=readings)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(
        f"\ncheck-address-latency: {len(result.failures)} failure(s), {len(result.warnings)} warning(s), "
        f"{len(probe)} address(es) dialled"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
