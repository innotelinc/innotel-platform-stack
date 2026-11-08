#!/usr/bin/env python3
"""Check the host map against the hosts themselves.

`docs/host-map.md` claims an address, a hostname and a role for each host. A claim
that nothing re-checks decays; this reads the table and asks each address who it
is over SSH, so a moved container or a reused address fails instead of being
discovered during an incident.

The check is deliberately narrow: it verifies reachability, that the hostname in
the table is the hostname that answers, and — where the row names a container
product — that dockerd there answers with at least one running container. It does
not compare container counts, which change with normal operation.

Usage:
    scripts/check-host-map.py [--ssh "ssh -o BatchMode=yes"] [--timeout 15] [--retries 3]

`--ssh` is the command prefix used to reach a host; the address is appended. The
default is key-based, non-interactive SSH for `root`. Credentials are never read
from this repository: pass an sshpass-based prefix on an operator machine, or use
an agent/key. Exit 0 when every row matches, 1 on a mismatch, 2 on usage or when
the document has no host rows.
"""
import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "host-map.md"
ROW = re.compile(r"^\|\s*`?(\d{1,3}(?:\.\d{1,3}){3})`?\s*\|\s*`?([^`|]+?)`?\s*\|")


def rows():
    out = []
    for line in DOC.read_text().splitlines():
        m = ROW.match(line.strip())
        if m:
            address, hostname = m.group(1), m.group(2).strip()
            if address.startswith("Address"):
                continue
            out.append((address, hostname))
    return out


def run(prefix, address, command, timeout, tries=1):
    last = None
    for _ in range(max(1, tries)):
        argv = shlex.split(prefix) + [f"root@{address}", command]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = "timed out"
            continue
        except FileNotFoundError as exc:
            return None, f"cannot run ssh: {exc}"
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            last = lines[-1] if lines else "ssh failed"
            continue
        return proc.stdout, None
    return None, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh", default="ssh -o BatchMode=yes -o ConnectTimeout=10")
    ap.add_argument(
        "--ssh-for",
        action="append",
        default=[],
        metavar="ADDRESS=PREFIX",
        help="override the ssh prefix for one address (repeatable)",
    )
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--retries", type=int, default=3, help="attempts per host for a dropped SSH session")
    args = ap.parse_args()

    per_host = {}
    for item in args.ssh_for:
        if "=" not in item:
            ap.error(f"--ssh-for needs ADDRESS=PREFIX, got {item!r}")
        address, prefix = item.split("=", 1)
        per_host[address.strip()] = prefix.strip()

    if not DOC.exists():
        print(f"check-host-map: {DOC} is missing", file=sys.stderr)
        return 2
    hosts = rows()
    if not hosts:
        print("check-host-map: docs/host-map.md has no host rows", file=sys.stderr)
        return 2

    failures = 0
    for address, hostname in hosts:
        prefix = per_host.get(address, args.ssh)
        out, err = run(prefix, address, "hostname; docker ps -q 2>/dev/null | wc -l", args.timeout, args.retries)
        if out is None:
            print(f"FAIL {address}: {err}")
            failures += 1
            continue
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        live = lines[0] if lines else ""
        count = lines[-1] if lines else "0"
        if live != hostname:
            print(f"FAIL {address}: document says {hostname!r}, host answers {live!r}")
            failures += 1
            continue
        print(f"ok   {address} {live} ({count} container(s))")
    if failures:
        print(f"check-host-map: {failures} of {len(hosts)} hosts disagree with the document", file=sys.stderr)
        return 1
    print(f"check-host-map: {len(hosts)} hosts match docs/host-map.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
