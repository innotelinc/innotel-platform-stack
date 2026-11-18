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

DROP-INS COUNT TOO
------------------
A unit file can be right while the definition that runs is not, because `*.d/*.conf` beside it
changes it: `swap-ceiling-check.service` deliberately carries no `OnFailure=`, and i1 wired
one anyway through `swap-ceiling-check.service.d/10-notify.conf` — a real override that lived
only on that host until it was landed in `hosts/i1/systemd/`. So every drop-in of an estate
unit is compared as well, against `systemd/<unit>.d/<file>` or, for the host it belongs to,
`hosts/<host>/systemd/<unit>.d/<file>`. A drop-in the repository does not record is a
finding in its own right, because that is exactly the override this check exists to surface.

WHY IT REACHES OTHER HOSTS
--------------------------
Only the edge's node_exporter is scraped, so a textfile written on `i1` or `i2` is read by
nobody: the comparison has to run where the series is published. `--host name=address`
compares a host's unit directory over `ssh` and reports what it finds in the same series,
with the host in the finding. A host that cannot be read is a finding too — silence from an
unreachable host is the failure this check exists to remove.

A HOST THAT ANSWERS WITH NOTHING
--------------------------------
`--host` says a host carries some of the revision's units, and the comparison can only find
drift in what it can see. i3 was the case that showed it: a live Incus host that had been
given none of the units its neighbours carry, so every run counted the neighbours' files,
published `OK`, and said nothing about the one host it could not describe. A listed host
whose listing holds neither a unit nor a drop-in of the revision is a finding now — either
nothing the estate keeps was installed there, or the listing reads a directory that is not
the one systemd loads. Both are worth a page; neither is a host that agrees.

WHY NOT EVERY UNIT INSTALLED HERE
---------------------------------
A host also carries units the estate does not own (Docker's, systemd's own), and they are not
this repository's business. The list comes from the revision, so a unit the estate keeps in
`systemd/` but never installed on a host is that host's neighbour's job and silence is right.

Usage:
    ./scripts/check-unit-drift.py
    ./scripts/check-unit-drift.py --host i1=192.168.1.51 --host i2=192.168.1.52
    ./scripts/check-unit-drift.py --unit-dir /tmp/units --ref HEAD
    ./scripts/check-unit-drift.py --prom /var/lib/node_exporter/textfile/estate-check-unit-drift.prom

Exit codes: 0 = everything installed on every host compared is the revision's, 1 = something
differs or a host could not be read, 2 = could not compare at all: never a pass.
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
    """One unit or drop-in whose installed bytes are not the revision's."""

    name: str
    installed: str
    revision: str
    #: Empty for this host, the `--host` name otherwise.
    host: str = ""
    #: "drifted" (both sides exist) or "unknown" (installed, the revision has no copy).
    kind: str = "drifted"

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


def _listed(repo: Path, ref: str, tree: str) -> list[str]:
    done = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only",
                           f"{ref}:{tree}"], capture_output=True, text=True)
    if done.returncode != 0:
        raise Unreadable(f"{repo} has no {tree} at {ref}")
    return done.stdout.split()


def repository_units(repo: Path, ref: str) -> list[str]:
    """Every unit the revision carries in `systemd/`, as basenames."""
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True)
    if resolved.returncode != 0:
        raise Unreadable(f"{repo} has no {ref} to compare against")
    names = [name for name in _listed(repo, ref, "systemd")
             if name.endswith((".service", ".timer")) and "/" not in name]
    if not names:
        raise Unreadable(f"{ref} carries no units under systemd/")
    return sorted(names)


def repository_drop_ins(repo: Path, ref: str, host: str = "") -> dict[str, str]:
    """`unit.d/file` -> its path in the repository, for this host and for every host.

    The host's own directory wins: that is the file the host is supposed to be carrying, and
    a universal `systemd/<unit>.d/` entry is the fallback for hosts that have no override of
    their own. Either way the file is in the repository, which is the point.
    """
    expected: dict[str, str] = {}
    for name in _listed(repo, ref, "systemd"):
        if ".d/" in name and name.endswith(".conf"):
            expected[name] = f"systemd/{name}"
    if host:
        tree = f"hosts/{host}/systemd"
        for name in _listed_optional(repo, ref, tree):
            if ".d/" in name and name.endswith(".conf"):
                expected[name] = f"{tree}/{name}"
    return expected


def _listed_optional(repo: Path, ref: str, tree: str) -> list[str]:
    done = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only",
                           f"{ref}:{tree}"], capture_output=True, text=True)
    return done.stdout.split() if done.returncode == 0 else []


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


def local_drop_ins(unit_dir: Path, names: list[str]) -> dict[str, str]:
    """`unit.d/file` -> digest, for every drop-in of an estate unit on this host."""
    found: dict[str, str] = {}
    for name in names:
        directory = unit_dir / f"{name}.d"
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.conf")):
            found[f"{directory.name}/{path.name}"] = digest(path.read_bytes())
    return found


def compare_drop_ins(installed: dict[str, str], expected: dict[str, str], read,
                     host: str = "") -> tuple[list[Mismatch], int]:
    """Check every installed drop-in against the repository's copy.

    A drop-in with no copy anywhere in the repository is reported as `unknown` rather than
    skipped: it is an override kept only on a host, which is the thing this comparison exists
    to make visible. A drop-in the repository keeps but the host does not carry is another
    host's job and stays silent, exactly like a unit.
    """
    found: list[Mismatch] = []
    for name, installed_digest in sorted(installed.items()):
        locus = expected.get(name)
        if locus is None:
            found.append(Mismatch(name, installed_digest, "", host, kind="unknown"))
            continue
        want = read(locus)
        if want is None:
            continue
        revision = digest(want)
        if installed_digest != revision:
            found.append(Mismatch(name, installed_digest, revision, host))
    return found, len(installed)


def host_listing(host: str, address: str, names: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """The host's unit digests and drop-in digests, in one ssh.

    One call per host rather than one per file: the listing is a shell loop and the answer is
    small enough to compare here. A host that does not answer raises, and the caller turns
    that into a finding — an unreachable host is a broken comparison, not a pass.
    """
    quoted = " ".join(shlex.quote(n) for n in names)
    # The trailing `true` matters: the loop exits with the status of its *last* file, so a
    # unit the host does not carry would make a sound listing look like an unreadable host.
    listing = (
        "cd /etc/systemd/system || exit 1; "
        f"for f in {quoted}; do [ -f \"$f\" ] && sha256sum \"$f\"; done; "
        f"for u in {quoted}; do for f in \"$u.d\"/*.conf; do [ -f \"$f\" ] && sha256sum \"$f\"; "
        "done; done; true")
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
    units: dict[str, str] = {}
    drop_ins: dict[str, str] = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64:
            continue
        name, short = parts[1].lstrip("*"), parts[0][:12]
        if "/" in name:
            drop_ins[name] = short
        else:
            units[name] = short
    return units, drop_ins


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


def audit(mismatches: list[Mismatch], compared: int, unreachable: list[str],
          empty: list[str] | None = None) -> "Audit":
    """The findings every scheduled check publishes."""
    out = Audit()
    for mismatch in mismatches:
        if mismatch.kind == "unknown":
            out.findings.append(Finding(
                "unknown", "fail",
                f"{mismatch.label} is installed ({mismatch.installed}) and the repository "
                "keeps no copy of it under systemd/ or this host's hosts/<host>/systemd/: an "
                "override that exists only on the host"))
            continue
        out.findings.append(Finding(
            "drifted", "fail",
            f"{mismatch.label} is installed as {mismatch.installed} and the revision's copy "
            f"is {mismatch.revision}: what runs is not what main says"))
    for reason in unreachable:
        out.findings.append(Finding(
            "unreachable", "fail",
            f"{reason}: the units there were not compared with anything"))
    for host in empty or []:
        out.findings.append(Finding(
            "no_files", "fail",
            f"{host} reported no unit and no drop-in of this revision: an empty listing is "
            "not a host that agrees, it is a host the comparison never reached"))
    if not mismatches and not unreachable and not empty:
        out.findings.append(Finding(
            "units_match", "note",
            f"all {compared} file(s) compared are the revision's own copies"))
    return out


def report(mismatches: list[Mismatch], compared: int, unreachable: list[str],
           ref: str, hosts: list[str], empty: list[str] | None = None) -> str:
    """The same verdict for a person reading the journal."""
    lines: list[str] = []
    for mismatch in mismatches:
        if mismatch.kind == "unknown":
            lines.append(f"{'LOCAL':8} {mismatch.label:44} installed {mismatch.installed:12} "
                         f"no copy in {ref}")
            continue
        lines.append(f"{'DRIFT':8} {mismatch.label:44} installed {mismatch.installed:12} "
                     f"revision {mismatch.revision}")
    for reason in unreachable:
        lines.append(f"{'UNREAD':8} {reason}")
    for host in empty or []:
        lines.append(f"{'EMPTY':8} {host} reported no unit and no drop-in of {ref}")
    where = ", ".join(["this host", *hosts])
    if not mismatches and not unreachable:
        lines.append(f"{'OK':8} {compared} file(s) on {where} match {ref}")
    lines.append("")
    lines.append(f"{len(mismatches) + len(empty or [])} finding(s) of {compared} file(s) "
                 f"compared "
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
    empty: list[str] = []
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

    # This host's drop-ins: no host name is known here, so only a universal
    # `systemd/<unit>.d/<file>` can vouch for one.
    found, counted = compare_drop_ins(local_drop_ins(args.unit_dir, names),
                                      repository_drop_ins(args.repo, args.ref), read)
    mismatches += found
    compared += counted

    for host, address in hosts:
        try:
            units, drop_ins = host_listing(host, address, names)
        except Unreadable as error:
            unreachable.append(str(error))
            continue
        if not units and not drop_ins:
            empty.append(host)
        compared += len(units) + len(drop_ins)
        mismatches += compare_hashes(host, units, read)
        found, _ = compare_drop_ins(drop_ins, repository_drop_ins(args.repo, args.ref, host),
                                    read, host)
        mismatches += found

    result = audit(mismatches, compared, unreachable, empty)
    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-unit-drift: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, result, True)

    if args.json:
        print(json.dumps({"mismatches": [vars(mismatch) for mismatch in mismatches],
                          "unreachable": unreachable, "empty": empty,
                          "compared": compared}, indent=2))
    else:
        print(report(mismatches, compared, unreachable, args.ref,
                     [host for host, _ in hosts], empty))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
