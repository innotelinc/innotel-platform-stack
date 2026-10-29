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
#: (`proxy`, `terminal`, `vault`, `vpn`) from `i1` on 2026-10-02 and holds it now; the
#: media and business containers (`atlas`, `genesis`, `rizzaura`, `www`) moved to `i1`,
#: `atheniq` to `i2`, and `mail` to `i3` on 2026-10-15 — every one of them keeping its
#: address, because the address is pinned in its own manager inside the container and
#: `incus copy` carries the rootfs. Read the entry that matches the host the container is
#: on *now*; a stale row here is a false alarm on every run.
#: `nas` (`.89`, the Orion desktop) and `regcache` (`.79`) joined the table on 2026-10-24,
#: when `auvik` (`.72`), `atheniq` (`.59`), `blueprint` (`.76`) and `regcache` (`.79`)
#: were converted from netplan `dhcp4: true` to static — the addresses had not moved,
#: but DHCP is how they would (rule 3), and those four were the warnings left in this
#: check. `clearpath` (`.83`, `app.denovocredit.com`) joined the same day: it went onto
#: `i2` statically in its own netplan, and a second, device-less `clearpath` left on `i4`
#: by the first attempt was deleted rather than listed — a name on two hosts is a warning
#: about the estate, not a row for the table.
#: `mail` moved on again: it runs on `i3` since 2026-10-15, and
#: `check-alert-delivery.py` derives the host it reads the mail log on from this table —
#: so a stale row here does not merely warn, it leaves the estate unable to prove that
#: its alerts are *delivered* rather than only configured (the probe reads a mail log on
#: a host the server has left, and the check exits 2 instead of reporting).
#: `acme` (`.49`) was retired off i1 on 2026-10-02 and is no longer an address to watch.
#: `www` (`.80`) was retired on 2026-10-29: the public landing page it was named for is
#: served from `ord-atheniq` (`104.238.162.5:8086`, the target of the edge's
#: `innotel.us www.innotel.us` vhost), the container has been stopped since its host was
#: shut down on 2026-10-28 00:42, its rootfs volume is empty and its `boot.autostart` is
#: false — so nothing dials `.80` and nothing would answer if something did. Keeping the
#: row made `check-address-latency.py` fail on an address no A record or vhost names,
#: which is a false alarm in the loud direction: the check is supposed to report a dark
#: address *someone dials*. The container itself is still there, empty; deleting it is a
#: separate decision and no check depends on it.
#: `monitor` (`.88`, SigNoz) joined on 2026-10-26: it was created on `i3` on 2026-10-25 and
#: was the estate's last running container no row named, so every run of this check warned
#: about a container that was behaving. It is static in its own netplan
#: (`/etc/netplan/50-static.yaml`, `dhcp4: false`) — the address is pinned, so it is a row,
#: not a DHCP conversion waiting to happen.
EXPECTED: dict[str, dict[str, str]] = {
    "i1": {
        "atlas": "192.168.1.90",
        "auvik": "192.168.1.72",
        "genesis": "192.168.1.66",
        "genie-preview": "192.168.1.24",
        "monarch": "192.168.1.56",
        "nas": "192.168.1.89",
        "ontrak": "192.168.1.21",
        "regcache": "192.168.1.79",
        "rizzaura": "192.168.1.62",
    },
    "i2": {
        "atheniq": "192.168.1.59",
        "blueprint": "192.168.1.76",
        "capstone": "192.168.1.30",
        "clearpath": "192.168.1.83",
        # `proxy` was recreated on i2 on 2026-10-26 22:00 EDT and is no longer an
        # i4 container; i4 holds `terminal`, `vault` and `vpn` only. The address is
        # unchanged (.71), which is why only the host row moves. Its `/data` bind
        # mount now exists (verified 2026-10-28: raw-mount path=/data,
        # source=/usr/src/proxy/data, 209 proxy_host confs readable), so rule 4
        # watches it rather than reporting the 2026-10-26 defect.
        "proxy": "192.168.1.71",
    },
    "i3": {
        "distro": "192.168.1.61",
        "magnate": "192.168.1.57",
        "mail": "192.168.1.15",
        # Running since before the table was written; the check kept reporting it as
        # "running and not in the table" on every run until 2026-10-28.
        "monitor": "192.168.1.88",
        "onyx": "192.168.1.60",
        "pi": "192.168.1.70",
        "signara": "192.168.1.44",
        "subscribe": "192.168.1.58",
    },
    "i4": {
        "terminal": "192.168.1.22",
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

#: host → container → the mount point that container's stack cannot work without.
#: Checked inside the container (rule 4), because a bind mount that did not arrive
#: leaves an address-correct container that still cannot serve.
DATA_MOUNTS: dict[str, dict[str, str]] = {
    "i2": {"proxy": "/data"},
}

#: What each address is for. Reported, so a failure names the thing that broke.
ROLES: dict[str, str] = {
    "192.168.1.21": "ontrak family + Ontrak Sync",
    "192.168.1.24": "the Genie preview (`innotel/ontrak-genie:main`)",
    "192.168.1.56": "monarch — media",
    # The LMS moved off this container on 2026-10-27 (NPM hosts 240/242/243 now forward
    # to 104.238.162.5:18080). The container itself did not stay on i2: by 2026-10-27 the
    # copy on i2 was STOPPED and this address belongs to the `atheniq` container running
    # on `ord` (the VPS, 104.238.162.5), reached over the `offload-l2` VXLAN/WireGuard
    # stretch. It still serves the apps that have not moved yet (the SSO pair, the two
    # python portals and the course-generation console), so a failure here is still an
    # outage for those — but no longer for the LMS.
    "192.168.1.59": "the AthenIQ container on ord (LMS migrated out; remaining apps here)",
    "192.168.1.71": "the Cerulean edge (NPM, Authentik, Vault, DNS)",
    "192.168.1.15": "mail (SMTP/IMAP)",
    "192.168.1.22": "the web terminal",
    "192.168.1.43": "WireGuard VPN",
    "192.168.1.73": "Vaultwarden + Linkwarden + Meilisearch",
    "192.168.1.30": "capstone / Zeus telephony",
    "192.168.1.76": "blueprint",
    "192.168.1.79": "regcache",
    "192.168.1.83": "Clearpath (`app.denovocredit.com`)",
    "192.168.1.88": "the monitoring stack (Prometheus, Grafana, Alertmanager)",
    "192.168.1.89": "the NAS desktop (`nas.innotel.us`)",
    "192.168.1.88": "SigNoz (`monitor`) — traces, metrics and logs for the estate",
}


# --------------------------------------------------------------------------------------
# The decision — pure, so it is the thing under test
# --------------------------------------------------------------------------------------


@dataclass
class Instance:
    """One instance as it actually is."""

    name: str
    state: str
    address: str | None = None
    #: False when no manager declares the address static — see rule 3.
    pinned: bool = True
    #: `container` or `virtual-machine`. A VM is not a container: it has no cgroup to
    #: probe and, unless something runs an agent inside the guest, no address incus can
    #: report — so the rules that read those are container-shaped (see `audit`).
    kind: str = "container"
    #: Whether the container's required mount point (rule 4) was observed to be a
    #: mount point. None means "not observed": the probe is only run for the
    #: containers in `DATA_MOUNTS`, and a caller (a test, or a host that could not
    #: be read) may supply no answer. None is never a finding — an absent
    #: observation is not evidence that the mount is missing.
    data_mounted: bool | None = None


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
                if inst.kind == "virtual-machine":
                    out.findings.append(
                        Finding(
                            "vm_no_address",
                            "note",
                            f"{host} {name}: a virtual machine with no address incus can see — "
                            "its guest reports one only with an agent installed, so this is "
                            "not the renumber rule 1 is about",
                        )
                    )
                    continue
                out.findings.append(
                    Finding("no_address", "fail", f"{host} {name}: running with no IPv4 (the table expects {address})")
                )
                continue
            # The LMS moved off this container on 2026-10-27 (NPM hosts 240/242/243
            # now forward to 104.238.162.5:18080). The container itself did not stay on
            # i2: by 2026-10-27 the copy on i2 was STOPPED and this address belongs to the
            # atheniq container running on ord (the VPS, 104.238.162.5), reached over the
            # offload-l2 VXLAN/WireGuard stretch. It still serves the apps that have not
            # moved yet (the SSO pair, the two python portals and the course-generation
            # console), so a failure here is still an outage for those — but no longer for
            # the LMS.
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
            # Rule 4: an address-correct container can still be unusable. `proxy`
            # serves NPM out of a `/data` bind mount (proxy_host configs,
            # custom_ssl, letsencrypt-acme-challenge, database.sqlite, keys.json).
            # If that mount did not arrive — an incus raw.mount that lost a race
            # with nginx startup — the container is at the right address and still
            # cannot route: it looks like a dead secondary to NPM while every other
            # rule passes. Report it separately for that reason.
            #
            # `data_mounted` is None when nobody observed it (see `Instance`), and
            # None is deliberately silent: this decision function must not invent a
            # failure out of a missing observation.
            if inst.data_mounted is False:
                out.findings.append(
                    Finding(
                        "data_mount_missing",
                        "fail",
                        f"{host} {name}: {DATA_MOUNTS[host][name]} is not a mount point — "
                        "the stack reads its state from there and will serve nothing; "
                        "restore the incus bind mount (type=bind,source=/usr/src/proxy/data,target=/data)",
                    )
                )

        for name, inst in sorted(seen.items()):
            if inst.state == "RUNNING":
                if inst.kind == "virtual-machine":
                    # A VM that answers to no address is not the table being stale; the
                    # first Windows build VM in the estate (2026-10-26) warned on every
                    # run for exactly this.
                    out.findings.append(
                        Finding(
                            "vm_unlisted",
                            "note",
                            f"{host} {name}: running virtual machine, not in the table — "
                            "add it only if the estate dials its address",
                        )
                    )
                    continue
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
        listing = read_host(host, target, "incus list --format csv -c nts4", runner)
        instances: list[Instance] = []
        for line in listing.splitlines():
            if not line.strip():
                continue
            name, _, rest = line.partition(",")
            kind, _, rest = rest.partition(",")
            state, _, addresses = rest.partition(",")
            # `incus list -c t` prints the type upper-case: CONTAINER, VIRTUAL-MACHINE.
            kind = kind.strip().lower()
            address = _first_lan_address(addresses)
            pinned = True
            mount = DATA_MOUNTS.get(host, {}).get(name.strip())
            data_mounted: bool | None = None
            if mount and state.strip() == "RUNNING" and kind != "virtual-machine":
                try:
                    # Field 5 of /proc/self/mountinfo is the mount point. A mount
                    # that is absent falls through to the root filesystem, which is
                    # exactly the state this rule exists to catch.
                    probe = runner(
                        target,
                        "incus exec %s -- bash -c "
                        "'grep -q \" %s \" /proc/self/mountinfo "
                        "&& echo mounted || echo absent' </dev/null" % (name.strip(), mount),
                    )
                    data_mounted = probe.strip().endswith("mounted")
                except Exception:  # not observed is not the same as not mounted
                    data_mounted = None
            if state == "RUNNING" and kind != "virtual-machine":
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
            instances.append(
                Instance(
                    name=name.strip(),
                    state=state.strip(),
                    address=address,
                    pinned=pinned,
                    kind=kind or "container",
                    data_mounted=data_mounted,
                )
            )
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
