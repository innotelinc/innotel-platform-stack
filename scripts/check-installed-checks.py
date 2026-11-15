#!/usr/bin/env python3
"""Fail when the installed checks are not the ones the repository says.

WHY THIS EXISTS
---------------
`install-estate-checks.sh` copies the checks onto the edge and compares each copy with
`scripts/<name>` at a revision, so a bad install is refused. That guard runs when somebody
installs, and only then. On 2026-11-15 a deployed check was replaced by an older copy an
hour after a clean install; every check kept running, kept publishing, and had quietly
stopped asserting what `main` says. Nothing in the estate could say so.

This is the same comparison on a timer, published as a series — the drift is a failing
check rather than something somebody has to notice.

WHAT IT COMPARES
----------------
1. **What the unit runs** — every file `ExecStart=` names inside the install directory,
   against `scripts/<name>` at the ref. The list comes from the installed unit, the one
   systemd actually reads, so a stale checkout cannot say which files should be running.
2. **What is sitting there** — every other `check-*.py` in the directory. A script the
   revision no longer carries is drift too: `check-sso-signin.py` and
   `check-copilotkit-door.py` were still installed after their replacements landed.
3. **Nothing else.** `*.bak*` copies are ignored deliberately — the estate keeps the copy
   it replaced beside the check, and that is a record, not drift.

A revision it cannot read is exit 2, never a pass: a check that compares nothing and says
`ok` is the silence this exists to remove.

Usage:
    ./scripts/check-installed-checks.py
    ./scripts/check-installed-checks.py --prom /path/to/textfile
    ./scripts/check-installed-checks.py --dir /opt/innotel/estate-checks \\
        --repo /usr/src/projects/complete/ips --ref origin/main

Exit codes: 0 = the install is the revision's own copies, 1 = drift, 2 = could not compare.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
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

CHECK = "installed_checks"

#: Where the checks are installed, which is also the directory the unit names.
DEFAULT_DIR = Path("/opt/innotel/estate-checks")
#: A checkout of this repository. Sparse is fine: `git show` reads the object, not the disk.
DEFAULT_REPO = Path("/usr/src/projects/complete/ips")
DEFAULT_REF = "origin/main"
#: The unit systemd reads, not the one in a checkout — see "what the unit runs" above.
DEFAULT_UNIT = Path("/etc/systemd/system/estate-checks.service")

INSTALLED_PATH = re.compile(r"/opt/innotel/estate-checks/([A-Za-z0-9._-]+)")


class Unreadable(RuntimeError):
    """The comparison could not be made, which is not the same thing as it passing."""


@dataclass
class Mismatch:
    """One file that is not what it should be, and the two short digests if there are two."""

    kind: str  # "drifted" | "unknown" | "missing"
    name: str
    installed: str = ""
    revision: str = ""


def digest(data: bytes) -> str:
    """A short digest, for a finding a person has to be able to read."""
    return hashlib.sha256(data).hexdigest()[:12]


def unit_files(unit: Path) -> list[str]:
    """Every file the unit names inside the install directory, as basenames."""
    try:
        text = unit.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise Unreadable(f"cannot read {unit}: {error}") from error
    names: list[str] = []
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            names.extend(INSTALLED_PATH.findall(line))
    return sorted(set(names))


def git_reader(repo: Path, ref: str):
    """A function from a basename to the bytes at `ref:scripts/<name>`, or None if absent.

    `ref` is resolved once, here: an unresolvable revision raises rather than returning a
    reader that answers None for everything, because None means "the revision dropped this
    file" and every check would then be reported as drift against nothing.
    """
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True)
    if resolved.returncode != 0:
        raise Unreadable(f"{repo} has no {ref} to compare against")

    def read(name: str) -> bytes | None:
        got = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:scripts/{name}"],
                             capture_output=True)
        return got.stdout if got.returncode == 0 else None

    return read


def compare(directory: Path, names: list[str], read) -> list[Mismatch]:
    """Every file the install has, against the revision. Pure apart from the two readers."""
    if not directory.is_dir():
        raise Unreadable(f"no install directory at {directory}")
    on_disk = {path.name for path in directory.glob("check-*.py")}
    found: list[Mismatch] = []
    for name in sorted(on_disk | set(names)):
        path = directory / name
        installed = path.read_bytes() if path.is_file() else None
        if installed is None:
            found.append(Mismatch("missing", name))
            continue
        want = read(name)
        if want is None:
            found.append(Mismatch("unknown", name, installed=digest(installed)))
            continue
        if want != installed:
            found.append(Mismatch("drifted", name, digest(installed), digest(want)))
    return found


def audit(mismatches: list[Mismatch]) -> "Audit":
    """The findings every scheduled check publishes."""
    out = Audit()
    for mismatch in mismatches:
        if mismatch.kind == "drifted":
            out.findings.append(Finding(
                "drifted", "fail",
                f"{mismatch.name} is installed as {mismatch.installed} and the revision has "
                f"{mismatch.revision}: what is running is not what main asserts"))
        elif mismatch.kind == "unknown":
            out.findings.append(Finding(
                "unknown", "fail",
                f"{mismatch.name} is installed ({mismatch.installed}) and the revision does "
                "not carry it: a script nothing in main owns is still sitting there"))
        else:
            out.findings.append(Finding(
                "missing", "fail",
                f"{mismatch.name} is named by the unit and is not in the install directory"))
    if not mismatches:
        out.findings.append(Finding(
            "installed_matches", "note",
            "every installed check is the revision's own copy"))
    return out


def report(mismatches: list[Mismatch], ref: str) -> str:
    """The same verdict for a person reading the journal."""
    lines: list[str] = []
    for mismatch in mismatches:
        if mismatch.kind == "drifted":
            lines.append(f"{'DRIFT':8} {mismatch.name:44} installed {mismatch.installed:12} "
                         f"revision {mismatch.revision}")
        elif mismatch.kind == "unknown":
            lines.append(f"{'UNKNOWN':8} {mismatch.name:44} installed "
                         f"{mismatch.installed:12} not in {ref}")
        else:
            lines.append(f"{'MISSING':8} {mismatch.name:44} named by the unit, not installed")
    if not mismatches:
        lines.append(f"{'OK':8} every installed check matches {ref}")
    lines.append("")
    lines.append(f"{len(mismatches)} finding(s) against {ref}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when the installed checks are not the revision's own copies.")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help="the install directory (default %(default)s)")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help="a checkout to read the revision from (default %(default)s)")
    parser.add_argument("--ref", default=DEFAULT_REF,
                        help="the revision to hold the install to (default %(default)s)")
    parser.add_argument("--unit", type=Path, default=DEFAULT_UNIT,
                        help="the installed unit (default %(default)s)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        names = unit_files(args.unit)
        if not names:
            raise Unreadable(f"{args.unit} names nothing inside {args.dir}")
        mismatches = compare(args.dir, names, git_reader(args.repo, args.ref))
    except Unreadable as error:
        print(f"check-installed-checks: {error}", file=sys.stderr)
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, None, False)
        return 2

    result = audit(mismatches)
    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-installed-checks: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, result, True)

    if args.json:
        print(json.dumps([vars(mismatch) for mismatch in mismatches], indent=2))
    else:
        print(report(mismatches, args.ref))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
