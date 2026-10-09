#!/usr/bin/env python3
"""Fail when a container is not at the LAN address the estate expects it at.

WHY THIS EXISTS
---------------
Every service here is reached **by address**, not by name: the A records point at
`192.168.1.x`, each compose file publishes `192.168.1.x:port`, and every consumer dials
that address. So a container's address is not an implementation detail — it is the
interface, and the estate has no way to route around it.

That is exactly what went wrong on 2026-10-01. The `ontrak` container was recreated with
a new MAC, the router's DHCP reservation no longer matched it, and it came up on `.20`
instead of `.21`. Everything that binds a specific LAN address died at once:

  * `ontrak-sync-api` and `ontrak-sync-web` were `Exited (255)` — `docker compose` had
    been told `ONTRAK_API_BIND=192.168.1.21` and the box did not have that address, so
    the containers could never start;
  * the family stack (Genie, Sentinel, Tix, Training) kept *running* only because it
    binds `0.0.0.0` — they were up on an address nothing dials, which is down with
    extra steps;
  * `sync.ontrak.innotel.us` and `genie.ontrak.innotel.us` returned nothing.

Nothing reported any of that. `docker compose` failure looked like an application
problem, the DNS name resolved to the *right* answer for the wrong host, and the only
signal was a person noticing a dashboard was blank. This check is that signal.

THE RULES
---------
1. **The expectation table is the contract.** A running container whose IPv4 is not
   the address the table names is a failure, whatever its state looks like.
2. **A declared bind is a promise about the container's own address.** Where the table
   records the env vars a stack publishes on (`binds`), the value must be an address
   the container actually has. This is the `Exited (255)` case, caught by name rather
   than by symptom.
3. **DHCP is drift in waiting.** A container that gets its address from DHCP is
   reported: it is the state that produced this outage, and it will produce it again
   the next time the container is recreated or the lease expires. "Pinned" means a
   static address in whichever manager the container actually uses — netplan
   (`dhcp4: false`), systemd-networkd (an `Address=` and no `DHCP=ipv4`), or
   ifupdown (`iface eth0 inet static`). The estate has all three, so a check that
   only read netplan would call a pinned container DHCP.
4. **The table must not be stale in the other direction.** A running container the
   table does not know about is reported, because the table is only worth having if it
   is the estate as it is.
5. **A stopped container is not a finding.** It has no address to be wrong. It is
   reported so an operator knows what the table is covering.

The address table lives here rather than in a doc because a doc cannot fail a build.
`docs/container-placement.md` keeps the reasoning; this keeps the invariant.

    ./scripts/check-container-addresses.py                 # live, over ssh to each host
    ./scripts/check-container-addresses.py --hosts i1=root@192.168.1.51,...
    ./scripts/check-container-addresses.py --json

Reaching the hosts: with key-based `ssh` the check just runs. This estate reaches its
hosts with a password, so where `sshpass` is installed, set `SSHPASS` and it is handed
through (`SSHPASS='…' ./scripts/check-container-addresses.py`). Without either, the
check exits 2 rather than reporting a pass it cannot stand behind.

Exit codes: 0 = every address holds, 1 = at least one does not, 2 = the check could not
run (a host it needs was unreachable — never silently a pass).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, read_host, ssh, write_prom  # noqa: E402

#: The `check` label this check publishes its metrics under (see estate_check).
CHECK = "container_address"

# --------------------------------------------------------------------------------------
# The estate as it should be
# --------------------------------------------------------------------------------------

#: host → {container: IPv4}. The addresses every A record and every published port names.
#: The table is the estate as it is, not as any one change left it: `i4` received the edge
#: (`proxy`, `terminal`, `vault`, `vpn`) from `i1` on 2026-10-02, and on 2026-10-09
#: `terminal` and `proxy` went back to `i1` while `atheniq` and `mail` came onto `i4` —
#: every one of them keeping its address, because the address is pinned in its own manager
#: inside the container and `incus copy` carries the rootfs. Read the entry that matches
#: the host the container is on *now*; a stale row here is a false alarm on every run.
#: `acme` (`.49`) was retired off i1 on 2026-10-02 and is no longer an address to watch.
EXPECTED: dict[str, dict[str, str]] = {
    "i1": {
        "genie-preview": "192.168.1.24",
        "monarch": "192.168.1.56",
        "ontrak": "192.168.1.21",
        "proxy": "192.168.1.71",
        "terminal": "192.168.1.22",
    },
    "i2": {
        "atlas": "192.168.1.90",
        "capstone": "192.168.1.30",
        "dev": "192.168.1.74",
        "genesis": "192.168.1.66",
        "rizzaura": "192.168.1.62",
        "www": "192.168.1.80",
    },
    "i3": {
        "distro": "192.168.1.61",
        "magnate": "192.168.1.57",
        "onyx": "192.168.1.60",
        "pi": "192.168.1.70",
        "signara": "192.168.1.44",
        "subscribe": "192.168.1.58",
    },
    "i4": {
        "atheniq": "192.168.1.59",
        "mail": "192.168.1.15",
        "vault": "192.168.1.73",
        "vpn": "192.168.1.43",
    },
}

#: Where each host is reachable. Overridable with `--hosts`.
#: `i4` (`root@192.168.1.54`) joined this table on 2026-10-02: it took the edge and three
#: other containers off i1, so the addresses the estate dials now live there too, and a
#: table that skipped it would leave those four addresses unwatched.
#: See docs/container-placement.md §The i4 host.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: host → container → {env var: address it must have}. Rule 2, for the stacks whose whole
#: failure mode is "the address in `.env` is not the one the box has".
BINDS: dict[str, dict[str, dict[str, str]]] = {
    "i1": {
        "ontrak": {
            "ONTRAK_API_BIND": "192.168.1.21",
            "ONTRAK_WEB_BIND": "192.168.1.21",
        }
    }
}

#: What each address is for. Reported, so a failure names the thing that broke.
ROLES: dict[str, str] = {
    "192.168.1.21": "ontrak family + Ontrak Sync",
    "192.168.1.24": "the Genie preview (`innotel/ontrak-genie:main`)",
    "192.168.1.56": "monarch — media",
    "192.168.1.59": "AthenIQ LMS (Tutor / Open edX)",
    "192.168.1.71": "the Cerulean edge (NPM, Authentik, Vault, DNS)",
    "192.168.1.15": "mail (SMTP/IMAP)",
    "192.168.1.22": "the web terminal",
    "192.168.1.43": "WireGuard VPN",
    "192.168.1.73": "Vaultwarden + Linkwarden + Meilisearch",
    "192.168.1.30": "capstone / Zeus telephony",
    "192.168.1.74": "the dev container",
}


# --------------------------------------------------------------------------------------
# The decision — pure, so it is the thing under test
# --------------------------------------------------------------------------------------


@dataclass
class Instance:
    """One container as it actually is."""

    name: str
    state: str
    address: str | None = None
    #: False when no manager declares the address static — see rule 3.
    pinned: bool = True


def audit(observed: dict[str, Iterable[Instance]]) -> Audit:
    """Compare what the hosts report against `EXPECTED` / `BINDS`."""
    out = Audit()

    for host, wanted in EXPECTED.items():
        seen = {inst.name: inst for inst in observed.get(host, [])}

        for name, address in sorted(wanted.items()):
            inst = seen.pop(name, None)
            if inst is None:
                out.findings.append(
                    Finding("missing", "fail", f"{host} {name}: not present (the table expects {address})")
                )
                continue
            if inst.state != "RUNNING":
                out.findings.append(
                    Finding("stopped", "note", f"{host} {name}: {inst.state} — no address to be wrong")
                )
                continue
            if inst.address is None:
                out.findings.append(
                    Finding("no_address", "fail", f"{host} {name}: running with no IPv4 (the table expects {address})")
                )
                continue
            if inst.address != address:
                role = ROLES.get(address, "a published service")
                out.findings.append(
                    Finding(
                        "address_moved",
                        "fail",
                        f"{host} {name}: at {inst.address}, expected {address} — "
                        f"{role} is dialled at {address}; anything binding it cannot start",
                    )
                )
            if not inst.pinned:
                out.findings.append(
                    Finding(
                        "not_pinned",
                        "warn",
                        f"{host} {name}: address comes from DHCP — a renumber is how this outage happens",
                    )
                )
            # Rule 2: what the stack publishes on must be an address the stack has.
            for var, bound in sorted(BINDS.get(host, {}).get(name, {}).items()):
                if bound != (inst.address or ""):
                    out.findings.append(
                        Finding(
                            "bind_mismatch",
                            "fail",
                            f"{host} {name}: {var}={bound} but the container is at "
                            f"{inst.address or '(none)'} — the service cannot bind and will not start",
                        )
                    )

        for name, inst in sorted(seen.items()):
            if inst.state == "RUNNING":
                out.findings.append(
                    Finding("unexpected", "warn", f"{host} {name}: running and not in the table — update the table")
                )

    return out


# --------------------------------------------------------------------------------------
# The signal, so a renumber reaches someone
# --------------------------------------------------------------------------------------
#
# The textfile this check publishes is written by `estate_check.write_prom`, so every
# host-level check shares one metric family — `innotel_estate_check`, one series per check
# via the `check` label — and one set of alert rules
# (`extensions/monitoring/prometheus/rules/estate-checks.yml`). The contract (the metric
# names, and why a check that cannot reach a host publishes 0 rather than nothing) is
# described in `estate_check`. This check contributes only its `CHECK` name.


# --------------------------------------------------------------------------------------
# The estate, as it is
# --------------------------------------------------------------------------------------


#: Reaching a host is shared with the other estate checks (`estate_check.ssh`), so the
#: key/password behaviour — and why `BatchMode=yes` is dropped when SSHPASS is set — is
#: described in one place.
_ssh = ssh


def _first_lan_address(text: str) -> str | None:
    """The first LAN address in an `incus list -c ns4` address field.

    The field is a CSV-quoted, newline-separated list like
    `"192.168.1.56 (eth0)\n172.20.0.1 (br-…)"`, so it is split on comma, quote,
    whitespace and `/` (a CIDR suffix is cut) before the prefix test. A container whose
    only addresses are bridges (docker0, br-…) has no LAN address and returns None.
    """
    for token in re.split(r"[\s,\"/]+", text):
        if token.startswith("192.168.1."):
            return token
    return None


def gather(hosts: dict[str, str], runner: Callable[[str, str], str] = _ssh) -> dict[str, list[Instance]]:
    """Read each host's containers, their addresses and whether they are pinned.

    `pinned` is answered from the container's own configuration, read from whichever
    manager holds it: a static declaration in netplan, systemd-networkd or ifupdown is
    what survives a recreation, and it is what the fix for the 2026-10-01 outage put in
    place (see rule 3).
    """
    observed: dict[str, list[Instance]] = {}
    for host, target in hosts.items():
        listing = read_host(host, target, "incus list --format csv -c ns4", runner)
        instances: list[Instance] = []
        for line in listing.splitlines():
            if not line.strip():
                continue
            name, _, rest = line.partition(",")
            state, _, addresses = rest.partition(",")
            address = _first_lan_address(addresses)
            pinned = True
            if state == "RUNNING":
                try:
                    # One shell probe, because it runs inside the container and the
                    # three managers are told apart only there. `s` starts as dhcp and
                    # is set to static by the first manager that declares the address.
                    probe = runner(
                        target,
                        "incus exec %s -- bash -c '"
                        "s=dhcp; "
                        "grep -sq \"dhcp4: false\" /etc/netplan/*.yaml 2>/dev/null && s=static; "
                        "if [ \"$s\" = dhcp ]; then "
                        "grep -sqE \"^[[:space:]]*Address[[:space:]]*=\" /etc/systemd/network/*.network 2>/dev/null && "
                        "! grep -sqE \"^[[:space:]]*DHCP[[:space:]]*=[[:space:]]*(ipv4|yes|true)\" /etc/systemd/network/*.network 2>/dev/null && s=static; "
                        "fi; "
                        "if [ \"$s\" = dhcp ]; then "
                        "grep -sqE \"iface[[:space:]]+eth0[[:space:]]+inet[[:space:]]+static\" /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null && s=static; "
                        "fi; "
                        "echo $s' </dev/null" % name,
                    )
                    pinned = "static" in probe
                except Exception:  # a container we cannot exec into is not pinned *silently*
                    pinned = False
            instances.append(Instance(name=name.strip(), state=state.strip(), address=address, pinned=pinned))
        observed[host] = instances
    return observed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the table's)")
    parser.add_argument("--json", action="store_true", help="machine-readable findings")
    parser.add_argument(
        "--prom",
        metavar="PATH",
        help="also write a Prometheus textfile here, for node-exporter's textfile collector",
    )
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-container-addresses: could not write {args.prom}: {error}", file=sys.stderr)

    missing = [t for t in (shutil.which("ssh"),) if t is None]
    if missing:
        print("check-container-addresses: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        observed = gather(hosts)
    except Exception as error:  # unreachable host is not a pass — see the module docstring
        print(f"check-container-addresses: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-container-addresses: reads hosts with key-based `ssh`; where the "
                "estate uses passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(observed)
    emit(result, ran=True)
    if args.json:
        print(json.dumps([f.__dict__ for f in result.findings], indent=2))
    else:
        for level in ("fail", "warn", "note"):
            for finding in [f for f in result.findings if f.level == level]:
                print(f"{level.upper():4} {finding.message}")
        print(
            f"\ncheck-container-addresses: {len(result.failures)} failure(s), "
            f"{len([f for f in result.findings if f.level == 'warn'])} warning(s)"
        )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
