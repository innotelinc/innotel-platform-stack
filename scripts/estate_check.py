#!/usr/bin/env python3
"""Shared support for the estate's host-level checks.

The Python counterpart of `stack-lib.sh`: the pieces every scheduled check needs —
reaching an incus host over ssh, the findings shape, and the Prometheus textfile the
alerts read. It is here so a new check is a check, rather than a fourth copy of the
same telemetry contract.

**Every check publishes the same metric family**, one series per check, with the
`check` label naming it. That is what lets one alert rule cover all of them and still
say which check fired:

    innotel_estate_check_last_status{check="…"}              1 ran and found nothing, 0 otherwise
    innotel_estate_check_last_run_timestamp{check="…"}       unix seconds, every run
    innotel_estate_check_last_success_timestamp{check="…"}   advanced only on a clean run
    innotel_estate_check_failures{check="…"}                 things that do not hold
    innotel_estate_check_warnings{check="…"}                 things worth a look

`last_status` is 1 only when the check ran AND found nothing, so a check that could
not reach a host reads as a failure rather than as silence — the 2026-10-01 outage
stayed invisible because nothing reported.

Exit codes are the estate's: 0 = clean, 1 = at least one failure, 2 = the check could
not run (an unreachable host is never silently a pass).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

METRIC = "innotel_estate_check"


@dataclass
class Finding:
    code: str
    level: str  # "fail" | "warn" | "note"
    message: str


@dataclass
class Audit:
    findings: list[Finding] = field(default_factory=list)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "fail"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.failures


# --------------------------------------------------------------------------------------
# Reaching a host
# --------------------------------------------------------------------------------------


def ssh_prefix() -> list[str]:
    """How to reach a host here.

    Key-based SSH is the clean case and the default (`BatchMode=yes`, so an unreachable
    host fails instead of hanging on a prompt). Where the estate reaches a host with a
    password, `sshpass` is used with `SSHPASS` from the environment — the same way every
    other script here talks to a host. `BatchMode=yes` is dropped in that case because it
    disables password auth outright, which would make a check exit 2 forever.
    """
    if os.environ.get("SSHPASS") and shutil.which("sshpass"):
        return ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no"]
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"]


def ssh(host: str, command: str, timeout: int = 25) -> str:
    return subprocess.run(
        [*ssh_prefix(), "-o", f"ConnectTimeout={timeout}", host, command],
        capture_output=True,
        text=True,
        timeout=timeout + 15,
        check=True,
    ).stdout


# --------------------------------------------------------------------------------------
# The signal — a textfile the alert rules read
# --------------------------------------------------------------------------------------


def _prior_metric(path: str, name: str) -> float | None:
    """The value of `name` in an existing textfile, so a bad run does not erase a good run."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(" ")
                if key == name:
                    return float(value)
    except (OSError, ValueError):
        return None
    return None


def prometheus_text(
    check: str,
    result: Audit | None,
    ran_ok: bool,
    now: float,
    prior_success: float | None = None,
) -> str:
    """The textfile body for one check's run. Pure, so the contract is the thing under test."""
    ok = bool(ran_ok and result is not None and result.ok)
    failures = len(result.failures) if result is not None else None
    warnings = len(result.warnings) if result is not None else None
    success = now if ok else prior_success
    selector = f'{{check="{check}"}}'

    lines = [
        f"# HELP {METRIC}_last_status 1 when the check ran and found nothing",
        f"# TYPE {METRIC}_last_status gauge",
        f"{METRIC}_last_status{selector} {1 if ok else 0}",
        f"# HELP {METRIC}_last_run_timestamp When the check last ran, whatever it found",
        f"# TYPE {METRIC}_last_run_timestamp gauge",
        f"{METRIC}_last_run_timestamp{selector} {now:.0f}",
    ]
    if failures is not None:
        lines += [
            f"# HELP {METRIC}_failures Things the check found that do not hold",
            f"# TYPE {METRIC}_failures gauge",
            f"{METRIC}_failures{selector} {failures}",
            f"# HELP {METRIC}_warnings Things the check found worth a look",
            f"# TYPE {METRIC}_warnings gauge",
            f"{METRIC}_warnings{selector} {warnings}",
        ]
    if success is not None:
        lines += [
            f"# HELP {METRIC}_last_success_timestamp When the check last ran and found nothing wrong",
            f"# TYPE {METRIC}_last_success_timestamp gauge",
            f"{METRIC}_last_success_timestamp{selector} {success:.0f}",
        ]
    return "\n".join(lines) + "\n"


def write_prom(path: str, check: str, result: Audit | None, ran_ok: bool, now: float | None = None) -> None:
    """Write the textfile atomically, so a scrape never reads a half-written file."""
    now = time.time() if now is None else now
    # The key carries the label selector, because that is what the metric line reads:
    # `name{check="…"} value`. Matching on the bare name would miss every series.
    prior = _prior_metric(path, f'{METRIC}_last_success_timestamp{{check="{check}"}}')
    body = prometheus_text(check, result, ran_ok, now, prior_success=prior)
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False, prefix=".estate-check-")
    try:
        handle.write(body)
        handle.close()
        # World-readable: the textfile collector (node-exporter) reads this as an
        # unprivileged user, and `NamedTemporaryFile` creates it 0600 — which reads as
        # "no metrics" rather than as a permission problem. Measured, 2026-10-01.
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise


def count_cpus(spec: str) -> int:
    """How many CPUs a Linux cpuset spec names (`0-1` → 2, `3` → 1, `` → 0)."""
    total = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            total += int(high) - int(low) + 1
        else:
            total += 1
    return total
