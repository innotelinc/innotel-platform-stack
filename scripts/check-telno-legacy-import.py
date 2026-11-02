#!/usr/bin/env python3
"""Fail when the Telno console and its legacy database have drifted apart.

WHY THIS EXISTS
---------------
The Telno node carries two databases: `/data/control-plane.db` (the new product) and
`/data/telno.db` (the Asterisk-era one, kept read-only). `scripts/import-legacy.mjs`
copies the handful of records that still mean something — devices, extensions, confirmed
directory listings, brand strings — and is idempotent, so on a healthy node every record
it knows about already exists and it reports nothing but `exists`.

The failure this watches for is silent in both directions:

* a row deleted in the console (or by a bad edit) is simply gone — the import never
  re-asserts it, and the only symptom is a name missing from a phone's directory;
* a device or extension that arrives in the legacy database after the last import is
  never copied, because nothing runs the importer again.

Neither makes a service unhealthy, so nothing else in the estate could see it. A
`telno-legacy-import-check` timer on the node already runs the check and journals its
exit status, but the node's journal is not where anybody looks — this publishes the same
answer as a metric beside every other estate check, so a failure arrives as an alert and
appears on the dashboard instead of sitting in `journalctl` on one host.

HOW IT READS THE NODE
---------------------
The check runs the node's own script *in the container* rather than reimplementing the
comparison here: the importer is the authority on what "present" means, and it is built
into the image that is running. The node is an incus container on `i4` with no sshd of
its own, so the read is two hops, both already established: the estate's key-only ssh to
the host, then `incus exec` into the container that owns the mounted volume. This is the
same path every other check takes to a host — the checks run on the Cerulean edge, which
has no incus trust relationship with `i4` and should not need one.

Exit status is the script's interface, exactly as the timer reads it:

    0  every record is present
    1  something would change (drift), or a record could not be mapped
    2  the check could not run at all

Mapping those onto the estate's contract: 0 is a clean run with the record counts stated
as a *note* (so the dashboard says what was checked, not just "ok"), 1 is a failure per
changed record, and everything else — including ssh's own 255 — is exit 2. An unreadable
node is never silently a pass.

Usage:
    ./scripts/check-telno-legacy-import.py
    ./scripts/check-telno-legacy-import.py --prom /path/to/textfile
    ./scripts/check-telno-legacy-import.py --node 192.168.1.54

Exit codes: 0 = clean, 1 = drifted, 2 = could not run (never silently a pass).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, ssh_prefix, write_prom  # noqa: E402

CHECK = "telno_legacy_import"

#: The host that runs the container (`i4`, per the container address table) and the node's
#: own check, in the order it has to be run. The outer `incus exec` is the hop this check
#: adds; the inner `docker exec` is where both databases are mounted.
NODE_TARGET = "root@192.168.1.54"
CONTAINER = "telno"
COMMAND = [
    "incus",
    "exec",
    CONTAINER,
    "--",
    "docker",
    "exec",
    CONTAINER,
    "node",
    "/app/scripts/check-legacy-import.mjs",
]

#: The importer reads two local databases and prints a small report; a minute is plenty,
#: and a hung one must not hold the whole estate-checks unit open.
TIMEOUT_SECONDS = 120

#: ssh's own failure code. Not a verdict about the data, so it must never read as drift.
SSH_FAILURE = 255


@dataclass
class Result:
    """What the node's check exited with and said."""

    code: int
    stdout: str = ""
    stderr: str = ""


def probe_node(target: str = NODE_TARGET, timeout: int = TIMEOUT_SECONDS) -> Result:
    """Run the node's own check over the estate's ssh path and return its verdict.

    `check=False` because a non-zero exit is the finding, not an error to raise — only a
    command that could not be started or that timed out is exceptional.
    """
    completed = subprocess.run(
        [
            *ssh_prefix(),
            "-o",
            "ConnectTimeout=25",
            target,
            " ".join(COMMAND),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def _line(text: str) -> str:
    """One report line, with the script's column alignment flattened."""
    return " ".join(text.split())


def audit(result: Result) -> Audit:
    """Read the node's report as findings. Pure, so the mapping is the thing under test.

    The script names each changed record on its own stderr line — `drift devices: …` or
    `skipped lines: …` — which is far more useful on a dashboard than a count, and the
    numbers are single digits, so each becomes its own finding.
    """
    out = Audit()

    if result.code == 0:
        # What was checked, stated rather than implied: the counts are the evidence that
        # the run compared something at all.
        summary = next(
            (line for line in reversed(result.stdout.splitlines()) if line.strip()),
            "importer reported no records",
        )
        out.findings.append(Finding("complete", "note", _line(summary)))
        return out

    named = [
        _line(line)
        for line in result.stderr.splitlines()
        if line.strip().startswith(("drift", "skipped"))
    ]
    for line in named:
        # `skipped` is not drift: it is a record the importer could not map (an unknown
        # device model). It is still a gap in the console, so it fails too.
        code = "unmapped" if line.startswith("skipped") else "drift"
        out.findings.append(Finding(code, "fail", line))

    if not named:
        detail = next(
            (line for line in reversed(result.stderr.splitlines()) if line.strip()),
            "no output",
        )
        out.findings.append(
            Finding(
                "no_report",
                "fail",
                f"the importer exited {result.code} without naming what changed: {_line(detail)}",
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--node", metavar="TARGET", default=NODE_TARGET, help="ssh target of the host running the node")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-telno-legacy-import: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-telno-legacy-import: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        result = probe_node(args.node)
    except Exception as error:  # noqa: BLE001 — any failure here is "could not run"
        print(f"check-telno-legacy-import: could not reach the node: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    # ssh's 255, the importer's 2, a container restart: the check did not run, which is a
    # different thing from having run and found nothing, and never a pass.
    if result.code not in (0, 1):
        detail = _line(result.stderr.strip().splitlines()[-1]) if result.stderr.strip() else "no output"
        why = "could not reach" if result.code == SSH_FAILURE else f"the node's check exited {result.code}"
        print(f"check-telno-legacy-import: {why} {args.node}: {detail}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    report = audit(result)
    emit(report, ran=True)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in report.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(f"\ncheck-telno-legacy-import: {len(report.failures)} failure(s)")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
