#!/usr/bin/env python3
"""That the two services on 192.168.1.93 are still reachable by the name people use.

WHY THIS EXISTS
---------------
`192.168.1.93` is a VM whose inbound traffic is proxied by the Windows box at
`192.168.1.17` (`f4:28:9d:8c:e4:f3`), and that proxy answers ARP for the address but drops
roughly half the TCP connects it forwards. Measured 2026-11-03: 12 pinned-ARP connects to
the VM's real MAC were **0/12**, traffic through `.17` was **9/15**, ICMP was 20/20 clean.
Every tool that looked at the VM itself said healthy — containers up, no SYN drops, no
conntrack churn — because the loss was never on the VM.

The fix is a pair of reverse tunnels (`systemd/coolify-npm-tunnel.service` on the VM,
outbound `.93 -> .71`, which is clean) published on the edge's Docker bridge, and the NPM
proxy hosts forward to those instead of to `.93`:

    deploy.innotel.us           dials 172.18.0.1:8090  ->  127.0.0.1:8000  (Coolify panel)
    fax-staging.lab.innotel.us  dials 172.18.0.1:8091  ->  127.0.0.1:3997  (fax staging)

Both were retired with `.93` on 2026-11-14. `deploy.innotel.us` came back on 2026-11-15
without a tunnel: the Coolify panel (4.7.5) now runs in the `coolify` container on `i3`
(`192.168.1.85:8000`), which the edge reaches directly, so there is no listener to probe.
Since 2026-11-25 the name is one of the 27 NPM rows behind Authentik forward-auth, so a
browser gets provider 35's login page before the panel. That login **is** the gate, and
this check treats it as an open door rather than as somebody else's page; a login page
from any other host is still a failure. `fax-staging.lab.innotel.us` stays retired.

Three things break this without a word, and none of them is visible from either end alone:
the ssh session dies and autossh does not come back (the name answers 502), the VM's
service dies (same 502, tunnel fine), or the certificate on the name quietly lapses (the
name answers, and every browser refuses it). So this check asks all three from the edge:

  * the name answers over HTTPS, following redirects, with a status a working door gives;
  * the tunnel listener on the edge accepts a connect (so a 502 can be attributed);
  * the certificate the name is served covers it and has runway left.

It runs beside the other estate checks (`systemd/estate-checks.service` on the edge) and
publishes the shared `innotel_estate_check` contract, so `EstateCheckFailing` and
`EstateCheckAbsent` already cover it. Its own series name the number behind the verdict.

Exit codes, the estate's: 0 = every door open, 1 = one is not, 2 = nothing could be probed.

Usage:
    ./scripts/check-deploy-door.py
    ./scripts/check-deploy-door.py --prom /path/to/textfile
    ./scripts/check-deploy-door.py --names deploy.innotel.us
"""
from __future__ import annotations

import argparse
import datetime
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, write_prom  # noqa: E402

CHECK = "deploy_door"

#: The tunnels the edge publishes, and the port on the VM each one carries. Keyed by the
#: listener, because that is what a proxy host dials: a 502 from the name and a refused
#: connect here together say "the tunnel", while a 502 with the listener up says "the
#: service behind it".
#: Empty since 2026-11-14, when `192.168.1.93` (the estate's development VMware guest) was
#: retired and both proxy hosts were deleted rather than left answering 502 (backups
#: `/root/npm-host-321.json` and `/root/npm-host-281.json`). `deploy.innotel.us` returned
#: on 2026-11-15 straight to the panel's own host and needs no listener; the fax staging
#: door stays retired. An empty map here says "no door rides a tunnel", not "nobody
#: looked" — the check keeps its place in the unit so the series cannot vanish silently.
TUNNELS: dict[str, int] = {}

#: The public names a door answers on. `coolify` is the marker a working panel's page
#: carries; a proxy that answers 200 with somebody else's default page is not a working
#: door, and 200 alone would call it one. The panel answers `/` with a 302 to `/login`,
#: which is followed, so the marker is read from the login page (measured 2026-11-15).
DOORS: dict[str, str | None] = {"deploy.innotel.us": "coolify"}

#: Doors whose page is not the panel itself because the estate's own forward-auth gate
#: stands in front of it: name -> the auth host the browser is sent to. `deploy.innotel.us`
#: joined the 27 gated rows on 2026-11-25 (`docs/authentik-forward-auth-audit-2026-11-25.md`),
#: so a 200 carrying provider 35's login page is the gate answering, not a stranger's default
#: page. The host has to be the estate's auth host: a login page anywhere else is still a
#: wrong-page failure, which is the trap this marker exists for.
GATES: dict[str, str] = {"deploy.innotel.us": "auth.innotel.us"}

#: Statuses a working door gives. A 502/503/504 is the failure this exists for; the
#: rest are answers (a gate's 401/403 is still an open door).
GOOD_STATUS = {200, 301, 302, 303, 307, 308, 401, 403}

#: Certificate runway: warn three weeks out, fail when it has effectively gone.
#: Let's Encrypt renews at thirty days left, so a name under twenty-one has already
#: missed a renewal cycle and still has three weeks to be fixed. A fortnight left is
#: late enough that the warning arrives as part of the incident it was meant to prevent.
WARN_DAYS = 21
FAIL_DAYS = 3

#: A door that takes this long is "up" but not usable — the VM's lossy path showed up as
#: multi-second answers before it showed up as failures.
SLOW_MS = 2000


@dataclass
class Door:
    """One public name, as the edge answered it."""

    name: str
    status: int | None = None
    ms: float | None = None
    body_has_marker: bool | None = None
    #: Where the redirect chain ended, so a gate can be told from a stray 200.
    final_url: str = ""
    cert_days: float | None = None
    cert_sans: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class Tunnel:
    """One listener on the edge, and whether the VM's port is behind it."""

    endpoint: str
    ms: float | None = None
    error: str = ""


# The socket timeout is deliberately well past `SLOW_MS` below. A door that takes 2 s is
# a warning and one that never answers is a failure; at 15 s the loaded edge produced the
# failure instead, because the checks run six at a time against the same 2 vCPU and
# `deploy.innotel.us` answered in 15.06 s on the 14:32 run - and in 2.5 s on the next one,
# from a terminal. The status is what this check is for, so give a slow door the room to be
# measured: `Finding("slow", "warn", ...)` is the line that reports it.
def probe_door(name: str, timeout: float = 30.0) -> Door:
    """Fetch `name` over HTTPS from this host, following redirects."""
    door = Door(name)
    started = time.monotonic()
    context = ssl.create_default_context()
    request = urllib.request.Request(f"https://{name}/", headers={"User-Agent": f"estate-check/{CHECK}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            door.status = response.status
            door.final_url = response.geturl()
            body = response.read(65536).decode("utf-8", "replace")
            if DOORS.get(name):
                door.body_has_marker = DOORS[name] in body.lower()
    except urllib.error.HTTPError as error:  # a 4xx/5xx is an answer, and its status matters
        door.status = error.code
        door.final_url = error.geturl()
        try:
            body = error.read(65536).decode("utf-8", "replace")
            if DOORS.get(name):
                door.body_has_marker = (DOORS[name] or "") in body.lower()
        except Exception:  # noqa: BLE001 — the status is the finding, not the body
            pass
    except Exception as error:  # noqa: BLE001 — no answer at all is a finding
        door.error = f"{type(error).__name__}: {error}"
    door.ms = (time.monotonic() - started) * 1000.0
    door.cert_days, door.cert_sans = served_cert(name)
    return door


def served_cert(name: str, timeout: float = 10.0) -> tuple[float | None, list[str]]:
    """Days until the certificate served for `name` expires, and its SANs.

    Read with `openssl`, not Python's `ssl`, because `getpeercert()` returns an empty dict
    under `CERT_NONE` and this check must not fail a door for a certificate it cannot
    verify — it verifies the estate's own self-signed chain elsewhere. No `openssl` is a
    silent skip of the expiry question, never a pass for it.
    """
    if shutil.which("openssl") is None:
        return None, []
    try:
        handshake = subprocess.run(
            ["openssl", "s_client", "-servername", name, "-connect", f"{name}:443", "-showcerts"],
            input="", capture_output=True, text=True, timeout=timeout,
        ).stdout
        if "BEGIN CERTIFICATE" not in handshake:
            return None, []
        text = subprocess.run(
            ["openssl", "x509", "-noout", "-enddate", "-ext", "subjectAltName"],
            input=handshake, capture_output=True, text=True, timeout=timeout,
        ).stdout
    except Exception:  # noqa: BLE001 — the expiry is a bonus fact, not the verdict
        return None, []
    days = None
    for line in text.splitlines():
        if line.startswith("notAfter="):
            stamp = line.split("=", 1)[1].strip().replace(" GMT", "")
            try:
                expiry = datetime.datetime.strptime(stamp, "%b %d %H:%M:%S %Y").replace(
                    tzinfo=datetime.timezone.utc
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                days = (expiry - now).total_seconds() / 86400.0
            except ValueError:
                days = None
    sans = [part.strip().split(":", 1)[1] for part in text.replace("\n", ",").split(",") if part.strip().startswith("DNS:")]
    return days, sans


def probe_tunnel(endpoint: str, timeout: float = 5.0) -> Tunnel:
    host, _, port = endpoint.rpartition(":")
    tunnel = Tunnel(endpoint)
    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except OSError as error:
        tunnel.error = f"{type(error).__name__}: {error}"
    tunnel.ms = (time.monotonic() - started) * 1000.0
    return tunnel


def covers(sans: list[str], name: str) -> bool:
    """Whether a certificate's SANs cover `name` (exact, or a one-label wildcard)."""
    for entry in sans:
        entry = entry.lower()
        if entry == name:
            return True
        if entry.startswith("*.") and name.count(".") == entry.count(".") and name.endswith(entry[1:]):
            return True
    return False


def audit(doors: list[Door], tunnels: list[Tunnel], unprobeable: str = "") -> Audit:
    """Read the probes as findings. Pure, so what counts as open is the thing under test."""
    out = Audit()
    if unprobeable:
        out.findings.append(Finding("unprobeable", "fail", unprobeable))
        return out

    for door in doors:
        if door.error:
            out.findings.append(
                Finding("dark", "fail", f"{door.name}: no answer over HTTPS — {door.error}")
            )
            continue
        if door.status not in GOOD_STATUS:
            out.findings.append(
                Finding("bad_status", "fail", f"{door.name}: answered {door.status} (a working door gives one of {sorted(GOOD_STATUS)})")
            )
        elif door.body_has_marker is False:
            gate = GATES.get(door.name)
            gated = bool(gate and door.final_url
                         and urlsplit(door.final_url).hostname == gate)
            if gated:
                out.findings.append(
                    Finding(
                        "gated",
                        "note",
                        f"{door.name}: the {gate} forward-auth gate answers before the panel "
                        f"(answered {door.status}, and the login page is {gate}'s)",
                    )
                )
            else:
                out.findings.append(
                    Finding(
                        "wrong_page",
                        "fail",
                        f"{door.name}: answered {door.status} but the page is not the panel (no {DOORS[door.name]!r})",
                    )
                )
        if door.ms is not None and door.ms > SLOW_MS:
            out.findings.append(
                Finding("slow", "warn", f"{door.name}: answered in {door.ms:.0f} ms — up, but past the {SLOW_MS} ms line")
            )
        if door.cert_days is not None:
            if door.cert_days < 0:
                out.findings.append(
                    Finding("cert_expired", "fail", f"{door.name}: its certificate expired {-door.cert_days:.0f} day(s) ago")
                )
            elif door.cert_days <= FAIL_DAYS:
                out.findings.append(
                    Finding("cert_critical", "fail", f"{door.name}: its certificate has {door.cert_days:.1f} day(s) left")
                )
            elif door.cert_days <= WARN_DAYS:
                out.findings.append(
                    Finding("cert_soon", "warn", f"{door.name}: its certificate has {door.cert_days:.0f} day(s) left")
                )
            if door.cert_sans and not covers(door.cert_sans, door.name):
                out.findings.append(
                    Finding("cert_mismatch", "fail", f"{door.name}: the served certificate does not name it ({', '.join(door.cert_sans[:4])})")
                )

    for tunnel in tunnels:
        if tunnel.ms is None or tunnel.error:
            out.findings.append(
                Finding(
                    "tunnel_down",
                    "fail",
                    f"{tunnel.endpoint}: no listener on the edge — the reverse tunnel that carries "
                    f"{TUNNELS[tunnel.endpoint]} is down ({tunnel.error or 'refused'})",
                )
            )
    return out


def series(doors: list[Door], tunnels: list[Tunnel]) -> str:
    """The numbers behind the verdict: status, latency, certificate days, listener."""
    lines = [
        f"# HELP {METRIC}_door_status The HTTP status {CHECK} saw through the edge",
        f"# TYPE {METRIC}_door_status gauge",
        f"# HELP {METRIC}_door_ms Seconds-to-answer in milliseconds, following redirects",
        f"# TYPE {METRIC}_door_ms gauge",
        f"# HELP {METRIC}_door_cert_days Days until the served certificate expires",
        f"# TYPE {METRIC}_door_cert_days gauge",
        f"# HELP {METRIC}_tunnel_up 1 when the edge's tunnel listener accepted a connect",
        f"# TYPE {METRIC}_tunnel_up gauge",
    ]
    for door in doors:
        selector = f'{{check="{CHECK}",name="{door.name}"}}'
        if door.status is not None:
            lines.append(f"{METRIC}_door_status{selector} {door.status}")
        if door.ms is not None:
            lines.append(f"{METRIC}_door_ms{selector} {door.ms:.3f}")
        if door.cert_days is not None:
            lines.append(f"{METRIC}_door_cert_days{selector} {door.cert_days:.2f}")
    for tunnel in tunnels:
        selector = f'{{check="{CHECK}",endpoint="{tunnel.endpoint}"}}'
        lines.append(f"{METRIC}_tunnel_up{selector} {1 if tunnel.ms is not None and not tunnel.error else 0}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--names", default=",".join(DOORS), help="comma-separated public names (default: the tunnelled doors)")
    parser.add_argument("--tunnels", default=",".join(TUNNELS), help="comma-separated host:port listeners on the edge")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    if not [n for n in args.names.split(",") if n.strip()] and not [
        e for e in args.tunnels.split(",") if e.strip()
    ]:
        # Nothing to probe because the doors were retired with their host, not because the
        # check broke. Exit 0 with a note: a series that vanishes reads exactly like a
        # check that stopped running, and every alert rule here is built to notice that.
        note = (
            "no doors were named, so nothing was probed; deploy.innotel.us rides no "
            "tunnel since 2026-11-15 and fax-staging.lab.innotel.us was retired with "
            "192.168.1.93 on 2026-11-14"
        )
        retired = Audit()
        retired.findings.append(Finding("retired", "note", note))
        if args.prom:
            try:
                write_prom(args.prom, CHECK, retired, True)
            except OSError as error:
                print(f"check-deploy-door: could not write {args.prom}: {error}", file=sys.stderr)
        print(f"NOTE {note}")
        print("\ncheck-deploy-door: 0 failure(s), 0 warning(s)")
        return 0

    doors: list[Door] = []
    tunnels: list[Tunnel] = []
    ran = True
    try:
        for name in [n.strip().lower() for n in args.names.split(",") if n.strip()]:
            DOORS.setdefault(name, None)
            doors.append(probe_door(name))
        for endpoint in [e.strip() for e in args.tunnels.split(",") if e.strip()]:
            TUNNELS.setdefault(endpoint, 0)
            tunnels.append(probe_tunnel(endpoint))
    except Exception as error:  # noqa: BLE001 — nothing probed is exit 2, never a pass
        ran = False
        print(f"check-deploy-door: could not probe: {error}", file=sys.stderr)

    if not doors and not tunnels:
        ran = False
    result = None if not ran else audit(doors, tunnels, unprobeable="" if doors or tunnels else "nothing to probe")

    if args.prom:
        try:
            write_prom(args.prom, CHECK, result, ran, extra=series(doors, tunnels) if ran else None)
        except OSError as error:
            print(f"check-deploy-door: could not write {args.prom}: {error}", file=sys.stderr)

    if result is not None:
        for level in ("fail", "warn", "note"):
            for finding in [f for f in result.findings if f.level == level]:
                print(f"{level.upper():4} {finding.message}")
        for door in doors:
            state = f"{door.status}" if door.status is not None else "NO ANSWER"
            cert = f"{door.cert_days:.0f}d" if door.cert_days is not None else "cert?"
            print(f"       {door.name:34} {state:>9}  {door.ms or 0:.0f} ms  cert {cert}")
        for tunnel in tunnels:
            state = "up" if tunnel.ms is not None and not tunnel.error else "DOWN"
            print(f"       {tunnel.endpoint:34} {state:>9}  {tunnel.ms or 0:.0f} ms")
        print(f"\ncheck-deploy-door: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
        return 0 if result.ok else 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
