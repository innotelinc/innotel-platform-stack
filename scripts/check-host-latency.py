#!/usr/bin/env python3
"""Warn when an estate host stops answering quickly — the WiFi power-save regression.

WHY THIS EXISTS
---------------
`i4`'s only uplink is WiFi (`wlp1s0`), and the edge — every address the estate dials,
plus DNS — now sits behind it. On 2026-10-02 the edge and three services moved there and
the estate got slow: an *idle* container answered in **100–900 ms**, while the edge itself,
busy with traffic of its own, looked fine. The hop was not at fault. The radio had
**power save** on, so it held a frame until the next DTIM beacon (~312 ms) whenever its own
traffic did not keep it awake. Turning it off (`systemd/wifi-powersave-off.service`) put
every container back at **~7 ms**. See `docs/container-placement.md` §The i4 host.

The lesson is not "turn power save off" — that is done, and it is a one-line unit. The
lesson is that the regression was **felt, not measured**: nothing in the estate had a
number that would have said "everything just got 40× slower". A host re-image, a networkd
rewrite, or a driver default can put power save back, and then the only signal is a person
noticing the estate is sluggish. This is that number.

WHAT IT MEASURES
----------------
ICMP round-trip time, from wherever the check runs — on the edge, which is the place the
estate is dialled from and the only place the WiFi hop is on the path. It sends **one ping
per sample after an idle gap**, because that is the state the power save shows up in: a
ping every 200 ms keeps the radio awake and would measure the good case even with power
save on. The idle gap is a little over the ~312 ms DTIM interval. The median of the samples
is the reading, and it is published so the number is visible, not just the verdict.

It is deliberately not throughput. The symptom was latency; throughput on a 2-vCPU box is
dominated by the box rather than the hop; and a throughput probe needs a server and load to
mean anything — three reasons a 30-minute check should not be made expensive to be wrong.
Latency is the cheap, unambiguous signal.

THE RULES
---------
1. **Every estate host answers promptly.** Median RTT at or above `SLOW_MS` (200 ms, ~25×
   the measured ~7 ms) is a warning: something is holding frames, and the likely something
   is the uplink's power save.
2. **A host that does not answer usefully is a failure.** Median at or above
   `UNUSABLE_MS`, or no reply at all, is a failure — the address is dialled and is not
   answering.
3. **A fast ping is not a pass for the host.** This says the path is quick; it says nothing
   about the host's services. The other estate checks own that.

Usage:
    ./scripts/check-host-latency.py
    ./scripts/check-host-latency.py --prom /path/to/textfile
    ./scripts/check-host-latency.py --samples 5 --idle 0.8

Exit codes: 0 = every host answers promptly, 1 = one does not, 2 = the check could not run
(no `ping` on PATH — never silently a pass).
"""
from __future__ import annotations

import argparse
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, write_prom  # noqa: E402

CHECK = "host_latency"

#: Where each estate host is reachable. The probe is ICMP to the address, so it runs where
#: the check runs — on the Cerulean edge, which is the only place the `i4` WiFi hop is on
#: the path to every one of these. Same table as the other checks, so a host cannot be
#: watched for its address and not for its latency.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: One ping per sample, after an idle gap. The gap is what makes power save visible: a
#: radio that is kept awake answers as if power save were off.
SAMPLES = 3
IDLE_SECONDS = 0.6

#: Median RTT at/above which a host is reported. Measured healthy is ~7 ms; the power-save
#: case was 100–900 ms, so 200 ms is far above the good state and far below the bad one.
SLOW_MS = 200.0
#: Median at/above which the host is not answering usefully (near the 2 s ping timeout).
UNUSABLE_MS = 1000.0

_RTT = re.compile(r"time=([0-9.]+)\s*ms")


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
    hosts: dict[str, str],
    runner=local,
    samples: int = SAMPLES,
    idle: float = IDLE_SECONDS,
) -> dict[str, list[float]]:
    """Ping each host's address from here, returning the RTTs seen. `runner` is injectable."""
    observed: dict[str, list[float]] = {}
    for name, target in hosts.items():
        address = target.split("@", 1)[-1]
        observed[name] = parse_rtts(runner(probe_command(address, samples, idle)))
    return observed


def audit(samples: dict[str, list[float]]) -> Audit:
    """Read the samples as findings. Pure, so what counts as slow is the thing under test."""
    out = Audit()
    for host in sorted(samples):
        times = samples[host]
        if not times:
            out.findings.append(
                Finding(
                    "no_reply",
                    "fail",
                    f"{host}: no ICMP reply — the address is dialled and is not answering",
                )
            )
            continue
        median = statistics.median(times)
        if median >= UNUSABLE_MS:
            out.findings.append(
                Finding(
                    "unusable",
                    "fail",
                    f"{host}: answers in {median:.0f} ms — the address is dialled and effectively "
                    f"unreachable (fast: ~7 ms)",
                )
            )
        elif median >= SLOW_MS:
            out.findings.append(
                Finding(
                    "slow",
                    "warn",
                    f"{host}: answers in {median:.0f} ms — ~7 ms is healthy; a radio holding "
                    f"frames until the next beacon is the usual cause "
                    f"(see systemd/wifi-powersave-off.service)",
                )
            )
    return out


def latency_series(samples: dict[str, list[float]]) -> str:
    """The per-host RTT gauge, so the reading is visible and not only the verdict."""
    lines = [
        f"# HELP {METRIC}_host_latency_ms Median ICMP round-trip to an estate host",
        f"# TYPE {METRIC}_host_latency_ms gauge",
    ]
    for host in sorted(samples):
        times = samples[host]
        if times:
            lines.append(f'{METRIC}_host_latency_ms{{check="{CHECK}",host="{host}"}} {statistics.median(times):.3f}')
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--samples", type=int, default=SAMPLES, help=f"single pings per host (default {SAMPLES})")
    parser.add_argument("--idle", type=float, default=IDLE_SECONDS, help=f"seconds to wait before each ping (default {IDLE_SECONDS})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool, samples: dict[str, list[float]] | None = None) -> None:
        if args.prom:
            try:
                extra = latency_series(samples) if samples else None
                write_prom(args.prom, CHECK, result, ran, extra=extra)
            except OSError as error:
                print(f"check-host-latency: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ping") is None:
        print("check-host-latency: no ping on PATH — this check measures ICMP", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        samples = measure(hosts, samples=args.samples, idle=args.idle)
    except Exception as error:  # a probe that cannot run is not a pass
        print(f"check-host-latency: could not probe a host: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    result = audit(samples)
    emit(result, ran=True, samples=samples)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    readings = ", ".join(
        f"{host} {statistics.median(times):.0f} ms" if times else f"{host} —"
        for host, times in sorted(samples.items())
    )
    print(f"\ncheck-host-latency: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    print(f"  median RTT: {readings}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
