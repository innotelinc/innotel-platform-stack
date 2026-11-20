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
import socket
import sys

DEFAULT_EDGE = "73.68.203.71"


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


def resolve(name: str, port: int = 443) -> list[str]:
    """Every address a browser could get for the name, in a stable order."""
    try:
        infos = socket.getaddrinfo(name, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    addresses = {info[4][0] for info in infos}
    return sorted(addresses)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Every public name must answer from the edge.")
    parser.add_argument("names", nargs="*", help="names to check")
    parser.add_argument("--targets", help="file with one name per line (blank lines and # comments ignored)")
    parser.add_argument("--edge", default=DEFAULT_EDGE, help=f"the edge's public address (default {DEFAULT_EDGE})")
    args = parser.parse_args(argv)

    names = list(args.names)
    if args.targets:
        with open(args.targets, encoding="utf-8") as handle:
            names.extend(
                line.split("#", 1)[0].strip()
                for line in handle
                if line.split("#", 1)[0].strip()
            )
    names = [name for name in dict.fromkeys(names) if name]
    if not names:
        print("check-edge-front-door: nothing to check", file=sys.stderr)
        return 2

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
        else:
            internal = [address for address in addresses if not is_public(address)]
            hint = f" (container address: {', '.join(internal)})" if internal else ""
            print(f"FAIL     {name} -> {shown}{hint}; expected the edge {args.edge}")
    print(f"check-edge-front-door: {len(names) - failures} on the edge, {failures} not")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
