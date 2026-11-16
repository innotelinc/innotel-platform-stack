#!/usr/bin/env python3
"""Fail when a unit installed on a host is not the repository's copy.

WHY THIS EXISTS
---------------
`systemd/` is the source of every unit the estate installs by hand, and the copy in
`/etc/systemd/system` is what actually runs. Nothing watched the gap between them: an
`ExecStart=` edited on a host during an incident, a fix applied to the unit and never landed,
a path changed by hand and reverted — all of them leave the unit working, the repository
wrong, and the next person applying the repository's file over a host's fix or the other way
round.

`estate-checks` closed this for its own job: the installer installs the unit and the scheduled
check compares it. This is the same comparison for every other unit the estate keeps — the
units in `systemd/` that a host actually carries, against `systemd/<name>` at the revision.

WHY IT REACHES OTHER HOSTS
--------------------------
Only the edge's node_exporter is scraped, so a textfile written on `i1` or `i2` is read by
nobody: the comparison has to run where the series is published. `--host name=address`
compares a host's unit directory over `ssh` and reports what it finds in the same series,
with the host in the finding. A host that cannot be read is a finding too — silence from an
unreachable host is the failure this check exists to remove.

WHY NOT EVERY UNIT INSTALLED HERE
---------------------------------
A host also carries units the estate does not own (Docker's, systemd's own), and they are not
this repository's business. The list comes from the revision, so a unit the estate keeps in
`systemd/` but never installed on a host is that host's neighbour's job and silence is right.

Usage:
    ./scripts/check-unit-drift.py
    ./scripts/check-unit-drift.py --host i1=192.168.1.51 --host i3=192.168.1.53
    ./scripts/check-unit-drift.py --unit-dir /tmp/units --ref HEAD
    ./scripts/check-unit-drift.py --prom /var/lib/node_exporter/textfile/estate-check-unit-drift.prom

Exit codes: 0 = every unit installed on every host compared is the revision's copy,
1 = one differs or a host could not be read, 2 = could not compare at all (no directory, no
revision): never a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
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
#: How long to wait for a host to answer, and for the whole of its listing.
SSH_TIMEOUT = 8
LIST_TIMEOUT = 60


class Unreadable(RuntimeError):
    """The comparison could not be made, which is not the same thing as it passing."""


@dataclass
class Mismatch:
    """One unit whose installed bytes are not the revision's."""

    name: str
    installed: str
    revision: str
    #: Empty for this host, the `--host` name otherwise.
    host: str = ""

    @property
    def label(self) -> str:
        return f"{self.host}:{self.name}" if self.host else self.name


def digest(data: bytes) -> str:
    """A short digest, for a finding a person has to be able to read."""
    return hashlib.sha256(data).hexdigest()[:12]


def parse_hosts(specs: list[str]) -> list[tuple[str, str]]:
    """`name=address` pairs; a bare address is its own name."""
    hosts: list[tuple[str, str]] = []
    for spec in specs:
        if not spec:
            raise Unreadable("--host needs name=address or an address")
        name, _, address = spec.partition("=")
        hosts.append((name, address or name))
    return hosts


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


def compare(unit_dir: Path, names: list[str], read, host: str = "") -> tuple[list[Mismatch], int]:
    """Which of the revision's units this host carries, and which of those differ.

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
        data = installed.read_bytes()
        if want != data:
            found.append(Mismatch(name, digest(data), digest(want), host))
    return found, compared


def host_hashes(host: str, address: str, names: list[str]) -> dict[str, str]:
    """The digest of every one of `names` that exists in the host's unit directory.

    One `ssh` per host rather than one per unit: the listing is a shell loop, and the answer
    is small enough to compare here. A host that does not answer raises, and the caller
    turns that into a finding — an unreachable host is a broken comparison, not a pass.
    """
    # The trailing `true` matters: the loop exits with the status of its *last* file, so a
    # unit the host does not carry would make a sound listing look like an unreadable host.
    listing = "cd /etc/systemd/system || exit 1; for f in " \
        + " ".join(shlex.quote(n) for n in names) + '; do [ -f "$f" ] && sha256sum "$f"; done; true'
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT}",
             f"root@{address}", listing],
            capture_output=True, text=True, timeout=LIST_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unreadable(f"{host} ({address}) could not be read: {error}") from error
    if done.returncode != 0:
        raise Unreadable(f"{host} ({address}) could not be read: "
                         f"{done.stderr.strip() or f'exit {done.returncode}'}")
    hashes: dict[str, str] = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            hashes[parts[1].lstrip("*")] = parts[0][:12]
    return hashes


def compare_hashes(host: str, hashes: dict[str, str], read) -> list[Mismatch]:
    """The host's digests against the revision's, without moving the files."""
    found: list[Mismatch] = []
    for name, installed in sorted(hashes.items()):
        want = read(f"systemd/{name}")
        if want is None:
            continue  # the revision dropped it between the listing and the read
        revision = digest(want)
        if installed != revision:
            found.append(Mismatch(name, installed, revision, host))
    return found


def audit(mismatches: list[Mismatch], compared: int, unreachable: list[str]) -> "Audit":
    """The findings every scheduled check publishes."""
    out = Audit()
    for mismatch in mismatches:
        out.findings.append(Finding(
            "drifted", "fail",
            f"{mismatch.label} is installed as {mismatch.installed} and the revision's "
            f"systemd/{mismatch.name} is {mismatch.revision}: what runs is not what main says"))
    for reason in unreachable:
        out.findings.append(Finding(
            "unreachable", "fail",
            f"{reason}: the units there were not compared with anything"))
    if not mismatches and not unreachable:
        out.findings.append(Finding(
            "units_match", "note",
            f"all {compared} unit(s) compared are the revision's own copies"))
    return out


def report(mismatches: list[Mismatch], compared: int, unreachable: list[str],
           ref: str, hosts: list[str]) -> str:
    """The same verdict for a person reading the journal."""
    lines: list[str] = []
    for mismatch in mismatches:
        lines.append(f"{'DRIFT':8} {mismatch.label:44} installed {mismatch.installed:12} "
                     f"revision {mismatch.revision}")
    for reason in unreachable:
        lines.append(f"{'UNREAD':8} {reason}")
    where = ", ".join(["this host", *hosts])
    if not mismatches and not unreachable:
        lines.append(f"{'OK':8} {compared} unit(s) on {where} match {ref}")
    lines.append("")
    lines.append(f"{len(mismatches)} finding(s) of {compared} unit(s) compared "
                 f"({where}) against {ref}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a unit installed on a host is not the repository's copy.")
    parser.add_argument("--unit-dir", type=Path, default=DEFAULT_UNIT_DIR,
                        help="where this host's units are installed (default %(default)s)")
    parser.add_argument("--host", action="append", default=[], metavar="NAME=ADDRESS",
                        help="also compare a host's units over ssh (repeatable)")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help="a checkout to read the revision from (default %(default)s)")
    parser.add_argument("--ref", default=DEFAULT_REF,
                        help="the revision to hold the hosts to (default %(default)s)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    refresh(args.repo, args.ref)
    unreachable: list[str] = []
    try:
        hosts = parse_hosts(args.host)
        names = repository_units(args.repo, args.ref)
        read = git_reader(args.repo, args.ref)
        mismatches, compared = compare(args.unit_dir, names, read)
    except Unreadable as error:
        print(f"check-unit-drift: {error}", file=sys.stderr)
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, None, False)
        return 2

    for host, address in hosts:
        try:
            hashes = host_hashes(host, address, names)
        except Unreadable as error:
            unreachable.append(str(error))
            continue
        compared += len(hashes)
        mismatches += compare_hashes(host, hashes, read)

    result = audit(mismatches, compared, unreachable)
    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-unit-drift: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, result, True)

    if args.json:
        print(json.dumps({"mismatches": [vars(mismatch) for mismatch in mismatches],
                          "unreachable": unreachable, "compared": compared}, indent=2))
    else:
        print(report(mismatches, compared, unreachable, args.ref,
                     [host for host, _ in hosts]))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
