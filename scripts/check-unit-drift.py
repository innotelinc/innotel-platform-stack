#!/usr/bin/env python3
"""Fail when a unit installed on this host is not the repository's copy.

WHY THIS EXISTS
---------------
`systemd/` is the source of every unit the estate installs by hand, and the copy in
`/etc/systemd/system` is what actually runs. Nothing watched the gap between them: an
`ExecStart=` edited on a host during an incident, a fix applied to the unit and never landed,
a path changed by hand and reverted — all of them leave the unit working, the repository
wrong, and the next person applying the repository's file over the host's fix or the other
way round.

`estate-checks` closed this for its own job: the installer installs the unit and the scheduled
check compares it. This is the same comparison for every other unit this host carries — the
units in `systemd/` that are installed here, against `systemd/<name>` at the revision.

WHY NOT EVERY UNIT INSTALLED HERE
---------------------------------
A host also carries units the estate does not own (Docker's, systemd's own), and they are not
this repository's business. The list comes from the revision, so a unit the estate keeps in
`systemd/` but never installed on this host is another host's job and silence is correct.

Usage:
    ./scripts/check-unit-drift.py
    ./scripts/check-unit-drift.py --prom /var/lib/node_exporter/textfile/estate-check-unit-drift.prom
    ./scripts/check-unit-drift.py --unit-dir /tmp/units --ref HEAD

Exit codes: 0 = every unit installed here is the revision's copy, 1 = one differs, 2 = could
not compare (no directory, no revision): never a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from estate_check import Audit, Finding, write_prom  # noqa: E402

    HAVE_TELEMETRY = True
except ImportError:  # the check still runs without the textfile beside it
    HAVE_TELEMETRY = False

CHECK = "unit_drift"

#: Where the host's units are installed.
DEFAULT_UNIT_DIR = Path("/etc/systemd/system")
#: A checkout of this repository. Sparse is fine: `git show` reads the object, not the disk.
DEFAULT_REPO = Path("/usr/src/projects/complete/ips")
DEFAULT_REF = "origin/main"


class Unreadable(RuntimeError):
    """The comparison could not be made, which is not the same thing as it passing."""


@dataclass
class Mismatch:
    """One unit whose installed bytes are not the revision's."""

    name: str
    installed: str
    revision: str


def digest(data: bytes) -> str:
    """A short digest, for a finding a person has to be able to read."""
    return hashlib.sha256(data).hexdigest()[:12]


def repository_units(repo: Path, ref: str) -> list[str]:
    """Every unit the revision carries in `systemd/`, as basenames."""
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True)
    if resolved.returncode != 0:
        raise Unreadable(f"{repo} has no {ref} to compare against")
    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only",
                             f"{ref}:systemd"], capture_output=True, text=True)
    if listed.returncode != 0:
        raise Unreadable(f"{ref} carries no systemd/ directory to compare against")
    return sorted(name for name in listed.stdout.split()
                  if name.endswith((".service", ".timer")))


def git_reader(repo: Path, ref: str):
    """A function from a path in the repository to its bytes at `ref`, or None if absent."""

    def read(path: str) -> bytes | None:
        got = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"],
                             capture_output=True)
        return got.stdout if got.returncode == 0 else None

    return read


def refresh(repo: Path, ref: str) -> None:
    """Bring `origin/<branch>` up to date, so a stale ref is not read as drift.

    Silent by design: this runs from a timer, and a fetch that fails must not turn a sound
    host into a failed check.
    """
    if not ref.startswith("origin/"):
        return  # a local revision is already whatever it is
    branch = ref.split("/", 1)[1]
    try:
        subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "--prune",
                        "origin", branch], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass


def compare(unit_dir: Path, names: list[str], read) -> tuple[list[Mismatch], int]:
    """Which of the revision's units are installed here, and which of those differ.

    The second number is how many were compared: a host that carries none of them has
    nothing to say, and a check that says "ok" after comparing nothing is the silence this
    exists to remove — so the count is published in the finding.
    """
    if not unit_dir.is_dir():
        raise Unreadable(f"no unit directory at {unit_dir}")
    found: list[Mismatch] = []
    compared = 0
    for name in names:
        installed = unit_dir / name
        if not installed.is_file():
            continue  # not this host's job
        compared += 1
        want = read(f"systemd/{name}")
        if want is None:
            continue  # the revision dropped it between the listing and the read
        if want != installed.read_bytes():
            found.append(Mismatch(name, digest(installed.read_bytes()), digest(want)))
    return found, compared


def audit(mismatches: list[Mismatch], compared: int) -> "Audit":
    """The findings every scheduled check publishes."""
    out = Audit()
    for mismatch in mismatches:
        out.findings.append(Finding(
            "drifted", "fail",
            f"{mismatch.name} is installed as {mismatch.installed} and the revision's "
            f"systemd/{mismatch.name} is {mismatch.revision}: what runs is not what main says"))
    if not mismatches:
        out.findings.append(Finding(
            "units_match", "note",
            f"all {compared} unit(s) this host carries are the revision's own copies"))
    return out


def report(mismatches: list[Mismatch], compared: int, ref: str) -> str:
    """The same verdict for a person reading the journal."""
    lines: list[str] = []
    for mismatch in mismatches:
        lines.append(f"{'DRIFT':8} {mismatch.name:40} installed {mismatch.installed:12} "
                     f"revision {mismatch.revision}")
    if not mismatches:
        lines.append(f"{'OK':8} {compared} unit(s) installed here match {ref}")
    lines.append("")
    lines.append(f"{len(mismatches)} finding(s) of {compared} unit(s) compared against {ref}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a unit installed on this host is not the repository's copy.")
    parser.add_argument("--unit-dir", type=Path, default=DEFAULT_UNIT_DIR,
                        help="where the host's units are installed (default %(default)s)")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help="a checkout to read the revision from (default %(default)s)")
    parser.add_argument("--ref", default=DEFAULT_REF,
                        help="the revision to hold the host to (default %(default)s)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    refresh(args.repo, args.ref)
    try:
        names = repository_units(args.repo, args.ref)
        mismatches, compared = compare(args.unit_dir, names, git_reader(args.repo, args.ref))
    except Unreadable as error:
        print(f"check-unit-drift: {error}", file=sys.stderr)
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, None, False)
        return 2

    result = audit(mismatches, compared)
    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-unit-drift: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, result, True)

    if args.json:
        print(json.dumps([vars(mismatch) for mismatch in mismatches], indent=2))
    else:
        print(report(mismatches, compared, args.ref))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
