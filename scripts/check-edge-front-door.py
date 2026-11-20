#!/usr/bin/env python3
"""Fail when a public name resolves straight at a container instead of the edge.

WHY THIS EXISTS
---------------
`next.atheniq.courses` was pointed at `192.168.1.120` rather than the edge on
2026-11-20. The stack itself was healthy and every container answered, but the
browser reached it over plain HTTP, so Django never set its Secure CSRF cookie and
the catalog's first search request was refused with `CSRF cookie not set`. The page
said "An unexpected error occurred" and nothing in the estate checks noticed,
because every check asked whether the *container* was healthy — none asked who
answers the name.

WHAT IT CHECKS
--------------
For each name: resolve it, and require that every address it resolves to is the
edge. An address inside the estate (RFC1918, loopback, link-local) is a bypass even
when the container behind it is up, because it skips the certificate, the shared
proxy and the redirect that make the secure-cookie path work. The check also
reports a name that does not resolve at all, separately: that is a different fault
with a different fix.

READ-ONLY
---------
It resolves names and prints findings. It changes no DNS record and no vhost; the
fix is a record that names the edge.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import socket
import subprocess
import sys

DEFAULT_EDGE = "73.68.203.71"
CHECK = "edge_front_door"


def is_public(address: str) -> bool:
    """True when the address is a name the internet could reach directly."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global


def classify(addresses: list[str], edge: str) -> str:
    """`edge`, `bypass`, `unresolved`, or `mixed` for one name.

    `mixed` matters: a name that answers from the edge *and* from a container
    still sends some clients around the front door, so it is a finding, not a pass.
    """
    if not addresses:
        return "unresolved"
    edge_parsed = ipaddress.ip_address(edge)
    on_edge = [address for address in addresses if ipaddress.ip_address(address) == edge_parsed]
    off_edge = [address for address in addresses if ipaddress.ip_address(address) != edge_parsed]
    if not off_edge:
        return "edge"
    return "mixed" if on_edge else "bypass"


def resolve_with_dig(name: str) -> list[str] | None:
    """A records for `name` from a resolver, or None when `dig` is not installed.

    Asking DNS rather than `getaddrinfo` is the difference between the question this
    check means and a different one: inside the edge container, the host's own
    `/etc/hosts` answers `proxy.innotel.us` with `127.0.1.1`, which reads as a bypass
    for the one name the estate serves from the edge itself. A browser asks a
    resolver, so this does too. `dig` is present on the hosts and in the edge
    container; where it is not, the caller falls back to the system resolver and the
    hosts-file answers come with it.
    """
    dig = shutil.which("dig")
    if dig is None:
        return None
    try:
        result = subprocess.run(
            [dig, "+short", "+time=3", "+tries=1", "A", name],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # `+short` prints a CNAME chain's names as well as its addresses, so only the
    # answers that parse as addresses are answers to this question.
    addresses = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return sorted({address for address in addresses if _is_address(address)})


def _is_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def resolve(name: str, port: int = 443) -> list[str]:
    """Every address a browser could get for the name, in a stable order.

    A name that cannot be looked up at all is a finding, not a crash: the pin file is
    hand-edited, and one malformed line (an empty label, a name over 63 bytes) must
    not take the other nine hundred with it. `idna` raises `UnicodeError` for those
    rather than the resolver's own `gaierror`, which is why both are caught here.
    """
    from_dig = resolve_with_dig(name)
    if from_dig is not None:
        return from_dig
    try:
        infos = socket.getaddrinfo(name, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    addresses = {info[4][0] for info in infos}
    return sorted(addresses)


def read_targets(path: str) -> list[str]:
    """The names a pin file declares, one per line, first column.

    `edge-vhost-targets.tsv` is `name<TAB>host<TAB>port<TAB>why`. Reading whole lines
    as names looked harmless and was not: a port reads as part of a label, a comment
    reads as a name, and the run either invents eight failures or dies in `idna` —
    which is exactly what happened the first time this was wired to that file.
    """
    names: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            entry = line.split("\t", 1)[0].split("#", 1)[0].strip()
            if entry:
                names.append(entry)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Every public name must answer from the edge.")
    parser.add_argument("names", nargs="*", help="names to check")
    parser.add_argument("--targets", help="file with one name per line (blank lines and # comments ignored)")
    parser.add_argument("--edge", default=DEFAULT_EDGE, help=f"the edge's public address (default {DEFAULT_EDGE})")
    parser.add_argument(
        "--prom",
        metavar="PATH",
        help="also write a Prometheus textfile here, for node-exporter's textfile collector",
    )
    args = parser.parse_args(argv)

    names = list(args.names)
    if args.targets:
        names.extend(read_targets(args.targets))
    names = [name for name in dict.fromkeys(names) if name]
    if not names:
        print("check-edge-front-door: nothing to check", file=sys.stderr)
        return 2

    findings: list[tuple[str, str]] = []
    failures = 0
    for name in names:
        addresses = resolve(name)
        verdict = classify(addresses, args.edge)
        shown = ", ".join(addresses) or "no address"
        if verdict == "edge":
            print(f"ok       {name} -> {shown}")
            continue
        failures += 1
        if verdict == "unresolved":
            print(f"FAIL     {name} -> does not resolve; nothing serves it")
            findings.append((f"{name}_unresolved", f"{name} does not resolve; nothing serves it"))
        else:
            internal = [address for address in addresses if not is_public(address)]
            hint = f" (container address: {', '.join(internal)})" if internal else ""
            print(f"FAIL     {name} -> {shown}{hint}; expected the edge {args.edge}")
            findings.append((f"{name}_off_edge", f"{name} -> {shown}; expected the edge {args.edge}"))
    print(f"check-edge-front-door: {len(names) - failures} on the edge, {failures} not")

    if args.prom:
        write_textfile(args.prom, findings)
    return 0 if failures == 0 else 1


def write_textfile(path: str, findings: list[tuple[str, str]]) -> None:
    """Publish the verdict the way every other estate check does.

    `estate_check` sits beside this file where the checks are installed, not in the
    repository's `scripts/`; a checkout that runs this by hand has no textfile
    collector to serve, so an absent module is reported rather than fatal.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)) if False else os.path.dirname(os.path.abspath(__file__)))
    try:
        from estate_check import Audit, Finding, write_prom  # noqa: PLC0415
    except ImportError as error:
        print(f"check-edge-front-door: could not write {path}: {error}", file=sys.stderr)
        return
    audit = Audit(findings=[Finding(code=code, level="fail", message=message) for code, message in findings])
    try:
        write_prom(path, CHECK, audit, ran_ok=True)
    except OSError as error:
        print(f"check-edge-front-door: could not write {path}: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
