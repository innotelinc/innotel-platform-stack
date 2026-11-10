#!/usr/bin/env python3
"""A pinned vhost must hold in NPM's own record — not only in the file nginx reads.

WHY THIS EXISTS
---------------
`scripts/edge-vhost-targets.tsv` pins the targets that are a contract: the two admin
doors, `sage.innotel.tech`, `blueprint.innotel.us`, and the TV door. Two checks already
read it, and on 2026-11-09 neither could see the failure that mattered:

  * `check-edge-vhost-targets.py` compares the pin to the **generated .conf**.
  * `check-npm-config-drift.py` compares the **row** to that same `.conf`.

So the pin and the row were never compared to each other. That is exactly the window
the TV door fell into. Its vhost file was repaired at ~15:30 and NPM's row was not;
at 17:53 NPM regenerated the file from the row, putting a Monarch SSO gateway
(`192.168.1.56:14002`) back in front of a door whose clients cannot complete a browser
flow. From that moment both checks read two copies that agreed — with each other, and
not with the estate. The only thing that noticed was `check-media-doors.py`, two hours
later, because it is the one check that asks what a door *is for*.

The lesson is not that a check was missing; it is that the row is the authority. NPM
regenerates `proxy_host/NN.conf` from it, on its own schedule, with no announcement, so
a file rewrite is a repair until the next regeneration. This check holds the pin to the
row, and `--apply` writes the pin *through the API* — the one write that updates the
row, the file and the running nginx together, in that order.

WHAT IT CHECKS
--------------
For every line in the declaration:

  1. NO HOST — NPM has no proxy host with that domain. The pin names a door the panel
     cannot show, which means the door is being served from a file alone.
  2. ROW DRIFT — NPM's row names a different `forward_host:forward_port` than the pin.
     This is the one that was invisible: the file may still be right, and the next
     regeneration is what makes it wrong.
  3. FILE LAG — the generated `.conf` names something other than the pin (usually
     because the row moved and the file has not been rewritten yet), or is missing
     entirely. Reported beside the row so the run says *which copy* to fix.

WHAT IT CHECKS FOR REDIRECTIONS
-------------------------------
`scripts/edge-redirect-targets.tsv` names the names the estate promises are redirects and
where each must go (28 names on 2026-11-09, every one of them a retired name that was
moved rather than deleted). The same three-way comparison runs against the redirection row
and `redirection_host/NN.conf`, plus one assertion no file comparison can make:

  4. SERVED AS A PROXY — an enabled proxy host lists that name. A redirect is how the
     estate retires a name; a proxy host on it means the name is being served again and
     the redirect it was promised to have stopped happening. Both files would still look
     right, so only the proxy-host list can answer this.

Redirection drift is not repaired by `--apply`: where a name should point is a product
decision (a customer TLD, not a container address), so a wrong destination is a person's
call. It has its own switch, `--apply-redirects`, because once a person has written the
destination into `edge-redirect-targets.tsv` it *is* that decision — the flag writes the
destination of a drifted row and nothing else. A name that came back as a proxy, a
redirection that was disabled, and a name NPM has no row for are three different decisions,
and none of them is a destination edit.

WHAT IT CHECKS ABOUT DESTINATIONS
---------------------------------
Every destination in the declaration is asked the one question neither the panel nor the
files can answer: does this name still take a visitor anywhere? A redirect to a name with
no address, or to one whose address accepts nothing on 443, sends every visitor of that
retired name into a browser error — and the row, the file and the declaration all agree
while it does.

No address is `redirect_dead`; an address that accepts nothing is `redirect_unreachable`.
If the check's own control name does not answer, the facet reports `destination_unchecked`
instead: a host that has lost its view of the internet has not learned that its 28
destinations are dead, and saying so is the only honest answer. That one is a warning
rather than exit 2, because the rest of the run did happen and its findings stand.

`--apply` fixes (2) and (3) by PUTting the row with every other field it already had,
then reading the whole row back and requiring it to be unchanged apart from the target
and `modified_on` — a field that goes missing on the way through is exactly how a
"small" API edit breaks a door. It then waits for the generated file to agree. Nothing
is written unless a pin is actually drifted, and `--apply` on a clean estate writes
nothing at all.

Run it on the edge host, where the API and the vhost tree are both real:

    ./scripts/check-edge-vhost-pins.py
    ./scripts/check-edge-vhost-pins.py --check-destinations
    ./scripts/check-edge-vhost-pins.py --apply              # only when drift is the finding
    ./scripts/check-edge-vhost-pins.py --apply-redirects    # only when a redirect drifted
    ./scripts/check-edge-vhost-pins.py --prom ${TEXTFILE_DIR}/estate-check-edge-vhost-pins.prom

`NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` come from the environment (the same pair the
estate's other NPM tools use). There is no default credential: one that cannot see the
rows is not a check that found nothing, it is exit 2.

Exit codes: 0 = every pin holds in both copies, 1 = at least one does not, 2 = could not
run (no credential, an unreachable panel, or no declaration to read).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
try:  # the estate's shared telemetry contract, beside this file when it is installed
    from estate_check import Audit, Finding, write_prom  # noqa: E402
    HAVE_TELEMETRY = True
except ImportError:  # a bare copy still has to run: --prom is then simply unavailable
    Audit = Finding = write_prom = None  # type: ignore[assignment]
    HAVE_TELEMETRY = False

#: The `check` label this check publishes under (`estate-checks.yml` alerts on it).
CHECK = "edge_vhost_pins"
DEFAULT_API = "http://192.168.1.71:81"
DEFAULT_CONFIG_DIR = Path("/data/nginx")
DEFAULT_PINS = SCRIPT_DIR / "edge-vhost-targets.tsv"
DEFAULT_REDIRECT_PINS = SCRIPT_DIR / "edge-redirect-targets.tsv"
PROXY_HOSTS = "/api/nginx/proxy-hosts"
REDIRECTION_HOSTS = "/api/nginx/redirection-hosts"

#: The fields NPM's redirection update accepts. This panel's redirection schema is stricter
#: than the proxy one: `enabled`, `hsts_enabled` and `hsts_subdomains` are rejected as
#: additional properties (read from `/app/schema/paths/nginx/redirection-hosts/` on
#: 2026-11-09; a body carrying them answers 400 `data must NOT have additional properties`),
#: while the proxy update accepts them. A destination edit therefore travels with the
#: certificate, the status code and the flags the redirect was configured with, and leaves
#: `enabled` alone — which is what this flag wants in any case: it never re-enables a row.
REDIRECT_UPDATABLE = (
    "domain_names",
    "forward_scheme",
    "forward_domain_name",
    "forward_http_code",
    "preserve_path",
    "certificate_id",
    "ssl_forced",
    "block_exploits",
    "advanced_config",
    "http2_support",
)

#: A destination the estate keeps answering, used to tell "this name is dead" apart from
#: "this host cannot see the internet". Without it every lost uplink would read as 28 dead
#: redirects, and a check that cries that loudly on a network blip is one people mute.
CONTROL_DESTINATION = "innotel.us"

#: The fields NPM's update endpoint accepts, in the shape its own UI sends them. The rest
#: of a row is bookkeeping (`id`, `created_on`, `modified_on`, `owner_user_id`) or the
#: panel's own health note (`meta`) — sending those back is how a two-field edit quietly
#: turns into a row with a lost field, and `advanced_config`, `certificate_id` and
#: `ssl_forced` are load-bearing for a door that serves video.
UPDATABLE = (
    "domain_names",
    "forward_scheme",
    "forward_host",
    "forward_port",
    "access_list_id",
    "certificate_id",
    "ssl_forced",
    "caching_enabled",
    "block_exploits",
    "advanced_config",
    "allow_websocket_upgrade",
    "http2_support",
    "enabled",
    "locations",
    "hsts_enabled",
    "hsts_subdomains",
    "trust_forwarded_proto",
)

SERVER_RE = re.compile(r'^\s*set\s+\$server\s+"([^"]*)"\s*;', re.M)
PORT_RE = re.compile(r"^\s*set\s+\$port\s+(\d+)\s*;", re.M)
NAME_RE = re.compile(r"^\s*server_name\s+([^;]+);", re.M)
#: A redirection file's `return 301 https://dest$request_uri` — the destination is the
#: contract, so it is read without its scheme, path or the `$request_uri` suffix.
RETURN_RE = re.compile(r"^\s*return\s+3\d\d\s+https?://([^/\s;$]+)", re.M)


@dataclass(frozen=True)
class Pin:
    """One declared target the estate forbids from drifting."""

    domain: str
    host: str
    port: int
    note: str = ""

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Host:
    """One proxy host as NPM's API reports it — the row, kept whole."""

    raw: dict

    @property
    def id(self) -> int:
        return int(self.raw["id"])

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(self.raw.get("domain_names") or ())

    @property
    def target(self) -> str:
        return f"{self.raw.get('forward_host')}:{self.raw.get('forward_port')}"

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", True))


@dataclass(frozen=True)
class RedirectPin:
    """A name the estate promises is a redirect, and where it must go."""

    domain: str
    destination: str
    note: str = ""


@dataclass(frozen=True)
class Redirect:
    """One redirection host as NPM's API reports it."""

    raw: dict

    @property
    def id(self) -> int:
        return int(self.raw["id"])

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(self.raw.get("domain_names") or ())

    @property
    def destination(self) -> str:
        return str(self.raw.get("forward_domain_name") or "")

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", True))


@dataclass(frozen=True)
class RedirectFile:
    """The generated redirection file, reduced to name and destination."""

    path: Path
    domains: tuple[str, ...]
    destination: str | None


@dataclass(frozen=True)
class Destination:
    """What one destination name actually does, as seen from the check's own host."""

    name: str
    addresses: tuple[str, ...] = ()
    reachable: bool = False
    error: str = ""


@dataclass(frozen=True)
class Vhost:
    """The generated file, reduced to what can disagree with its row."""

    path: Path
    domains: tuple[str, ...]
    host: str | None
    port: int | None

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"


class Unreadable(RuntimeError):
    """The panel could not be read — a check that cannot see rows has found nothing."""


def load_pins(path: Path) -> list[Pin]:
    """Read the declaration. Blank lines and `#` comments are skipped."""
    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            raise ValueError(f"{path}: expected domain<TAB>host<TAB>port, got: {line!r}")
        domain, host, port = (fields[0].strip(), fields[1].strip(), fields[2].strip())
        note = fields[3].strip() if len(fields) > 3 else ""
        pins.append(Pin(domain=domain, host=host, port=int(port), note=note))
    return pins


def read_vhosts(config_dir: Path) -> list[Vhost]:
    """Every generated proxy host file. `.bak` copies beside them are not doors."""
    root = config_dir / "proxy_host"
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob("*.conf")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names = NAME_RE.findall(text)
        if not names:
            continue
        domains: list[str] = []
        for line in names:
            domains.extend(line.split())
        server = SERVER_RE.search(text)
        port = PORT_RE.search(text)
        found.append(Vhost(path=path, domains=tuple(dict.fromkeys(domains)),
                           host=server.group(1) if server else None,
                           port=int(port.group(1)) if port else None))
    return found


def load_redirect_pins(path: Path) -> list[RedirectPin]:
    """Read the redirection declaration. `name<TAB>destination<TAB>note`."""
    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            raise ValueError(f"{path}: expected name<TAB>destination, got: {line!r}")
        domain, destination = fields[0].strip(), fields[1].strip()
        note = fields[2].strip() if len(fields) > 2 else ""
        pins.append(RedirectPin(domain=domain, destination=destination, note=note))
    return pins


def read_redirect_files(config_dir: Path) -> list[RedirectFile]:
    """Every generated redirection file. `.bak` copies beside them are not redirects."""
    root = config_dir / "redirection_host"
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob("*.conf")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names = NAME_RE.search(text)
        if not names:
            continue
        destination = RETURN_RE.search(text)
        found.append(RedirectFile(
            path=path,
            domains=tuple(dict.fromkeys(names.group(1).split())),
            destination=destination.group(1) if destination else None))
    return found


def payload_for(raw: dict, pin: Pin) -> dict:
    """The body that sets one row's target and carries every other field it had.

    Built from the row as the API returned it, through `UPDATABLE`, so a field the estate
    does not know about still travels — the failure being avoided is a rewrite that is
    correct about the port and silent about everything else.
    """
    body = {key: raw[key] for key in UPDATABLE if key in raw}
    body["forward_host"] = pin.host
    body["forward_port"] = pin.port
    return body


def round_trip(before: dict, after: dict, pin: Pin) -> list[str]:
    """Fields that did not survive an apply, given what the estate meant to change.

    `modified_on` moves by design, `id` identifies the row, and the target is the point —
    everything else must be identical or the write dropped something.
    """
    if int(before["id"]) != int(after.get("id", -1)):
        return ["id"]
    # `meta` is the panel's own health note (`nginx_online`), written by NPM during the
    # update; holding it to its previous value would report the panel working as a lost
    # field. Everything else is the estate's, and must come back unchanged.
    lost = [key for key in before
            if key not in ("id", "modified_on", "meta",
                           "forward_host", "forward_port")
            and before[key] != after.get(key, "<missing>")]
    if after.get("forward_host") != pin.host:
        lost.append("forward_host")
    if int(after.get("forward_port", -1)) != pin.port:
        lost.append("forward_port")
    return lost


def redirect_payload_for(raw: dict, pin: RedirectPin) -> dict:
    """The body that sets one redirection's destination and carries every other field."""
    body = {key: raw[key] for key in REDIRECT_UPDATABLE if key in raw}
    body["forward_domain_name"] = pin.destination
    return body


def redirect_round_trip(before: dict, after: dict, pin: RedirectPin) -> list[str]:
    """Fields that did not survive a redirection update, given what it meant to change."""
    if int(before["id"]) != int(after.get("id", -1)):
        return ["id"]
    lost = [key for key in before
            if key not in ("id", "modified_on", "meta", "forward_domain_name")
            and before[key] != after.get(key, "<missing>")]
    if after.get("forward_domain_name") != pin.destination:
        lost.append("forward_domain_name")
    return lost


def compare(pins: list[Pin], hosts: list[Host], vhosts: list[Vhost]) -> list[dict]:
    """Every way a pin can fail to hold in the row, or in the file that follows it."""
    by_domain: dict[str, Host] = {}
    for host in hosts:
        for domain in host.domains:
            by_domain.setdefault(domain, host)
    files: dict[str, Vhost] = {}
    for vhost in vhosts:
        for domain in vhost.domains:
            files.setdefault(domain, vhost)

    findings: list[dict] = []
    for pin in pins:
        row = by_domain.get(pin.domain)
        if row is None:
            findings.append({"check": "no_host", "domain": pin.domain, "pin": pin.target})
            continue
        if row.target != pin.target:
            findings.append({"check": "row_drift", "domain": pin.domain, "row": row.id,
                             "pin": pin.target, "row_target": row.target})
        live = files.get(pin.domain)
        if live is None:
            findings.append({"check": "file_lag", "domain": pin.domain, "row": row.id,
                             "pin": pin.target, "file_target": None,
                             "file": None})
        elif live.target != pin.target:
            findings.append({"check": "file_lag", "domain": pin.domain, "row": row.id,
                             "pin": pin.target, "file_target": live.target,
                             "file": live.path.name})
    return findings


def compare_redirects(pins: list[RedirectPin], redirects: list[Redirect],
                      files: list[RedirectFile], hosts: list[Host]) -> list[dict]:
    """Where a promised redirect can fail: the row, the file, or a proxy host on it."""
    by_domain: dict[str, Redirect] = {}
    for redirect in redirects:
        for domain in redirect.domains:
            by_domain.setdefault(domain, redirect)
    files_by_domain: dict[str, RedirectFile] = {}
    for redirect_file in files:
        for domain in redirect_file.domains:
            files_by_domain.setdefault(domain, redirect_file)
    # An enabled proxy host on a pinned redirect name is the failure this file exists for:
    # the name is being served again rather than moved. A disabled or deleted host on it is
    # history, and says nothing about what nginx is serving.
    proxied = {domain for host in hosts if host.enabled for domain in host.domains}

    findings: list[dict] = []
    for pin in pins:
        row = by_domain.get(pin.domain)
        if row is None:
            findings.append({"check": "no_redirect", "domain": pin.domain,
                             "pin": pin.destination})
        else:
            if not row.enabled:
                findings.append({"check": "redirect_disabled", "domain": pin.domain,
                                 "row": row.id, "pin": pin.destination})
            elif row.destination != pin.destination:
                findings.append({"check": "redirect_drift", "domain": pin.domain,
                                 "row": row.id, "pin": pin.destination,
                                 "row_destination": row.destination})
            live = files_by_domain.get(pin.domain)
            if live is None:
                findings.append({"check": "redirect_file_lag", "domain": pin.domain,
                                 "row": row.id, "pin": pin.destination,
                                 "file_destination": None, "file": None})
            elif live.destination != pin.destination:
                findings.append({"check": "redirect_file_lag", "domain": pin.domain,
                                 "row": row.id, "pin": pin.destination,
                                 "file_destination": live.destination,
                                 "file": live.path.name})
        if pin.domain in proxied:
            findings.append({"check": "served_as_proxy", "domain": pin.domain,
                             "pin": pin.destination})
    return findings


def check(pins: list[Pin], hosts: list[Host], vhosts: list[Vhost],
          redirect_pins: list[RedirectPin] | tuple = (),
          redirects: list[Redirect] | tuple = (),
          redirect_files: list[RedirectFile] | tuple = ()) -> tuple[int, list[dict]]:
    """Returns (exit code, findings) so a test can read the verdict."""
    findings = compare(pins, hosts, vhosts)
    findings += compare_redirects(list(redirect_pins), list(redirects),
                                  list(redirect_files), hosts)
    return (1 if findings else 0), findings


def probe_destination(name: str, resolver=None, connector=None,
                      timeout: float = 5.0) -> Destination:
    """Resolve a name and open 443 on it, without asking what the site says.

    Transport only, on purpose: what a destination *says* is a product question, while
    whether a visitor's browser can reach it at all is this check's. A 403 or a 404 still
    means the name takes the visitor somewhere; a name with no address, or an address that
    accepts nothing, does not.
    """
    resolver = resolver or (lambda host: socket.getaddrinfo(host, 443,
                                                            proto=socket.IPPROTO_TCP))
    try:
        infos = resolver(name)
    except OSError as error:
        return Destination(name=name, error=str(error) or "no address")
    addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        return Destination(name=name, error="no address")
    connector = connector or (lambda host: socket.create_connection((host, 443),
                                                                    timeout=timeout))
    try:
        connector(addresses[0])
    except OSError as error:
        return Destination(name=name, addresses=addresses,
                           error=str(error) or "nothing answers on 443")
    return Destination(name=name, addresses=addresses, reachable=True)


def check_destinations(pins: list[RedirectPin], probe=probe_destination,
                       workers: int = 8) -> tuple[list[dict], str | None]:
    """Every declared destination, probed once. Returns (findings, why it could not run).

    The control name is probed first: if it does not answer, this host cannot see the
    internet, and reporting 28 dead destinations would be a worse lie than reporting that
    the facet went unchecked.
    """
    names = list(dict.fromkeys(pin.destination for pin in pins))
    if not names:
        return [], None
    control = probe(CONTROL_DESTINATION)
    if not control.reachable:
        return [], (f"{CONTROL_DESTINATION} did not answer ({control.error or 'no address'}), "
                    "so this host cannot see the internet and no destination was judged")
    pinned_by: dict[str, list[str]] = {}
    for pin in pins:
        pinned_by.setdefault(pin.destination, []).append(pin.domain)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        probed = list(pool.map(probe, names))
    findings = []
    for result in probed:
        if result.reachable:
            continue
        findings.append({
            "check": "redirect_dead" if not result.addresses else "redirect_unreachable",
            "destination": result.name,
            "domains": pinned_by[result.name],
            "addresses": list(result.addresses),
            "detail": result.error,
        })
    return findings, None


def to_audit(findings: list[dict], unchecked: str | None = None) -> "Audit":
    """The same verdict in the shape every scheduled check publishes."""
    audit = Audit()
    for f in findings:
        if f["check"] == "no_host":
            audit.findings.append(Finding(
                code="no_host", level="fail",
                message=(f"{f['domain']} is pinned to {f['pin']}, but NPM has no host "
                         "for it — the door is being served by a file alone")))
        elif f["check"] == "row_drift":
            audit.findings.append(Finding(
                code="row_drift", level="fail",
                message=(f"{f['domain']}: NPM host {f['row']} says {f['row_target']}, "
                         f"the estate pins {f['pin']} — the next regeneration serves "
                         f"{f['row_target']}")))
        elif f["check"] == "file_lag":
            got = f["file_target"] or "no file"
            audit.findings.append(Finding(
                code="file_lag", level="fail",
                message=(f"{f['domain']}: {f['file'] or 'the vhost'} serves {got}, the "
                         f"estate pins {f['pin']}")))
        elif f["check"] == "no_redirect":
            audit.findings.append(Finding(
                code="no_redirect", level="fail",
                message=(f"{f['domain']} is pinned as a redirect to {f['pin']}, but NPM "
                         "has no redirection host for it")))
        elif f["check"] == "redirect_disabled":
            audit.findings.append(Finding(
                code="redirect_disabled", level="fail",
                message=(f"{f['domain']} is pinned as a redirect to {f['pin']}, but its "
                         f"redirection host {f['row']} is disabled")))
        elif f["check"] == "redirect_drift":
            audit.findings.append(Finding(
                code="redirect_drift", level="fail",
                message=(f"{f['domain']} redirects to {f['row_destination']}, the estate "
                         f"pins {f['pin']} (host {f['row']})")))
        elif f["check"] == "redirect_file_lag":
            got = f["file_destination"] or "no file"
            audit.findings.append(Finding(
                code="redirect_file_lag", level="fail",
                message=(f"{f['domain']}: {f['file'] or 'the redirection'} answers with "
                         f"{got}, the estate pins {f['pin']}")))
        elif f["check"] == "served_as_proxy":
            audit.findings.append(Finding(
                code="served_as_proxy", level="fail",
                message=(f"{f['domain']} is pinned as a redirect to {f['pin']}, but an "
                         "enabled proxy host serves it — the name is back, not moved")))
        elif f["check"] in ("redirect_dead", "redirect_unreachable"):
            names = ", ".join(f["domains"][:3]) + (" …" if len(f["domains"]) > 3 else "")
            what = ("does not resolve" if f["check"] == "redirect_dead"
                    else f"resolves to {f['addresses'][0]} but nothing answers on 443")
            audit.findings.append(Finding(
                code=f["check"], level="fail",
                message=(f"{f['destination']} is a pinned destination ({names}) and {what}: "
                         f"{f['detail']} — every visitor of those names lands nowhere")))
    if unchecked:
        audit.findings.append(Finding(code="destination_unchecked", level="warn",
                                      message=unchecked))
    return audit


def report(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        if f["check"] == "no_host":
            lines.append(f"{'NO-HOST':9} {f['domain']:32} pin {f['pin']:22} no NPM host")
        elif f["check"] == "row_drift":
            lines.append(f"{'ROW-DRIFT':9} {f['domain']:32} pin {f['pin']:22} "
                         f"row {f['row_target']} (host {f['row']})")
        elif f["check"] == "file_lag":
            got = f["file_target"] or "no file"
            lines.append(f"{'FILE-LAG':9} {f['domain']:32} pin {f['pin']:22} "
                         f"file {got}")
        elif f["check"] == "no_redirect":
            lines.append(f"{'NO-REDIR':9} {f['domain']:32} pin {f['pin']:22} "
                         "no NPM redirection")
        elif f["check"] == "redirect_disabled":
            lines.append(f"{'REDIR-OFF':9} {f['domain']:32} pin {f['pin']:22} "
                         f"row {f['row']} is disabled")
        elif f["check"] == "redirect_drift":
            lines.append(f"{'REDIR-DRIFT':11} {f['domain']:32} pin {f['pin']:22} "
                         f"row {f['row_destination']} (host {f['row']})")
        elif f["check"] == "redirect_file_lag":
            got = f["file_destination"] or "no file"
            lines.append(f"{'REDIR-LAG':9} {f['domain']:32} pin {f['pin']:22} "
                         f"file {got}")
        elif f["check"] in ("redirect_dead", "redirect_unreachable"):
            label = "REDIR-DEAD" if f["check"] == "redirect_dead" else "REDIR-NO-ANSWER"
            where = f"{f['addresses'][0]}:443" if f["addresses"] else "no address"
            lines.append(f"{label:14} {f['destination']:32} {where:22} "
                         f"{len(f['domains'])} name(s) — {f['detail']}")
        else:
            lines.append(f"{'PROXIED':9} {f['domain']:32} pin {f['pin']:22} "
                         "served by an enabled proxy host")
    lines.append("")
    drifted = sum(1 for f in findings if f["check"] == "row_drift")
    lagged = sum(1 for f in findings if f["check"] == "file_lag")
    orphan = sum(1 for f in findings if f["check"] == "no_host")
    redirects = sum(1 for f in findings
                    if f["check"] in ("redirect_drift", "redirect_disabled"))
    redirect_lag = sum(1 for f in findings if f["check"] == "redirect_file_lag")
    redirect_orphan = sum(1 for f in findings if f["check"] == "no_redirect")
    proxied = sum(1 for f in findings if f["check"] == "served_as_proxy")
    dead = sum(1 for f in findings if f["check"] == "redirect_dead")
    unanswered = sum(1 for f in findings if f["check"] == "redirect_unreachable")
    lines.append(f"{len(findings)} finding(s): {drifted} row drift, {lagged} file lag, "
                 f"{orphan} pinned name NPM does not have, {redirects} redirect drift, "
                 f"{redirect_lag} redirect file lag, "
                 f"{redirect_orphan} pinned redirect NPM does not have, "
                 f"{proxied} pinned redirect served as a proxy, "
                 f"{dead} destination with no address, "
                 f"{unanswered} destination not answering")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# The panel
# --------------------------------------------------------------------------------------


def _call(url: str, payload: dict | None = None, token: str | None = None,
          timeout: float = 20.0):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="PUT" if data else "GET")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode()
        return json.loads(body) if body else None


def token_for(api: str, email: str, secret: str) -> str:
    """An NPM API token, or Unreadable if the credential is missing or refused."""
    if not email or not secret:
        raise Unreadable("NPM_ADMIN_EMAIL/NPM_ADMIN_PASSWORD are not set")
    request = urllib.request.Request(
        api.rstrip("/") + "/api/tokens",
        data=json.dumps({"identity": email, "secret": secret}).encode(),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            reply = json.loads(response.read().decode() or "null")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM did not issue a token: {error}") from error
    if not isinstance(reply, dict) or not reply.get("token"):
        raise Unreadable("NPM refused the credential")
    return reply["token"]


def fetch_hosts(api: str, token: str) -> list[Host]:
    try:
        hosts = _call(api.rstrip("/") + PROXY_HOSTS, token=token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM did not list proxy hosts: {error}") from error
    if not isinstance(hosts, list):
        raise Unreadable("NPM returned no proxy host list")
    return [Host(raw=host) for host in hosts]


def fetch_redirects(api: str, token: str) -> list[Redirect]:
    try:
        rows = _call(api.rstrip("/") + REDIRECTION_HOSTS, token=token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM did not list redirection hosts: {error}") from error
    if not isinstance(rows, list):
        raise Unreadable("NPM returned no redirection host list")
    return [Redirect(raw=row) for row in rows]


def put_host(api: str, token: str, host_id: int, body: dict) -> dict:
    """Update one host through the API — the write that also regenerates its file."""
    try:
        reply = _call(f"{api.rstrip('/')}{PROXY_HOSTS}/{host_id}", body, token=token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM would not update host {host_id}: {error}") from error
    return reply if isinstance(reply, dict) else {}


def put_redirect(api: str, token: str, redirect_id: int, body: dict) -> dict:
    """Update one redirection — the write that also regenerates its file."""
    try:
        reply = _call(f"{api.rstrip('/')}{REDIRECTION_HOSTS}/{redirect_id}", body,
                      token=token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM would not update redirection {redirect_id}: {error}") from error
    return reply if isinstance(reply, dict) else {}


def apply_redirects(api: str, token: str, pins: list[RedirectPin], findings: list[dict],
                    config_dir: Path = DEFAULT_CONFIG_DIR,
                    sleep=time.sleep, attempts: int = 10) -> list[dict]:
    """Write each *drifted* destination to its row, then prove the row survived.

    Only `redirect_drift`. A name served by a proxy host, a disabled redirection and a
    missing row are three decisions this flag must not make on its own — it leaves them
    among the run's findings, where a person can see them.
    """
    by_id = {f["row"]: f for f in findings if f["check"] == "redirect_drift"}
    problems: list[dict] = []
    for redirect_id, finding in by_id.items():
        before = next((r.raw for r in fetch_redirects(api, token)
                       if r.id == redirect_id), None)
        if before is None:
            problems.append({"check": "apply_failed", "domain": finding["domain"],
                             "detail": f"redirection {redirect_id} vanished before the update"})
            continue
        pin = next(p for p in pins if p.domain == finding["domain"])
        after = put_redirect(api, token, redirect_id, redirect_payload_for(before, pin))
        lost = redirect_round_trip(before, after, pin)
        if lost:
            problems.append({"check": "apply_lost", "domain": finding["domain"],
                             "row": redirect_id, "detail": ", ".join(sorted(lost))})
        for _ in range(attempts):
            live = next((file for file in read_redirect_files(config_dir)
                         if pin.domain in file.domains), None)
            if live is not None and live.destination == pin.destination:
                break
            sleep(0.5)
    return problems


def apply_pins(api: str, token: str, pins: list[Pin], findings: list[dict],
               config_dir: Path = DEFAULT_CONFIG_DIR,
               sleep=time.sleep, attempts: int = 10) -> list[dict]:
    """Write each drifted pin to its row, then prove the row survived the write.

    Returns the findings for what went wrong; an empty list means every apply held. The
    file is allowed a moment: NPM writes it as part of the same update, but a check that
    read the directory in the same instant would call its own success a lag.
    """
    by_id = {f["row"]: f for f in findings if f["check"] == "row_drift"}
    problems: list[dict] = []
    for host_id, finding in by_id.items():
        before = next((h.raw for h in fetch_hosts(api, token) if h.id == host_id), None)
        if before is None:
            problems.append({"check": "apply_failed", "domain": finding["domain"],
                             "detail": f"host {host_id} vanished before the update"})
            continue
        pin = next(p for p in pins if p.domain == finding["domain"])
        after = put_host(api, token, host_id, payload_for(before, pin))
        lost = round_trip(before, after, pin)
        if lost:
            problems.append({"check": "apply_lost", "domain": finding["domain"],
                             "row": host_id, "detail": ", ".join(sorted(lost))})
        for _ in range(attempts):
            live = next((v for v in read_vhosts(config_dir)
                         if pin.domain in v.domains), None)
            if live is not None and live.target == pin.target:
                break
            sleep(0.5)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a pinned vhost's row or file names another target.")
    parser.add_argument("--api", default=os.environ.get("NPM_API", DEFAULT_API),
                        help="NPM's API base (default %(default)s)")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                        help="NPM's nginx directory (holds proxy_host)")
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS,
                        help="the declaration to hold both copies to")
    parser.add_argument("--redirect-pins", type=Path, default=DEFAULT_REDIRECT_PINS,
                        help="names that must stay redirects ('' to skip)")
    parser.add_argument("--apply", action="store_true",
                        help="write each drifted pin through NPM's API")
    parser.add_argument("--apply-redirects", action="store_true",
                        help="write each drifted redirect destination through NPM's API")
    parser.add_argument("--check-destinations", action="store_true",
                        help="also resolve and reach every pinned redirect destination")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.pins.is_file():
        print(f"no declaration at {args.pins} — nothing to hold the panel to",
              file=sys.stderr)
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, None, False)
        return 2
    pins = load_pins(args.pins)
    vhosts = read_vhosts(args.config_dir)
    if not vhosts:
        print(f"no proxy hosts under {args.config_dir}/proxy_host", file=sys.stderr)
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, None, False)
        return 2

    redirect_pins: list[RedirectPin] = []
    if str(args.redirect_pins) not in ("", "none"):
        if not args.redirect_pins.is_file():
            print(f"no redirection declaration at {args.redirect_pins} — a missing "
                  "declaration is not an empty one", file=sys.stderr)
            if args.prom and HAVE_TELEMETRY:
                write_prom(args.prom, CHECK, None, False)
            return 2
        redirect_pins = load_redirect_pins(args.redirect_pins)

    problems: list[dict] = []
    unchecked: str | None = None
    try:
        token = token_for(args.api, os.environ.get("NPM_ADMIN_EMAIL", ""),
                          os.environ.get("NPM_ADMIN_PASSWORD", ""))
        hosts = fetch_hosts(args.api, token)
        redirects = fetch_redirects(args.api, token) if redirect_pins else []
        code, findings = check(pins, hosts, vhosts, redirect_pins, redirects,
                               read_redirect_files(args.config_dir))
        if args.apply:
            problems += apply_pins(args.api, token, pins, findings, args.config_dir)
        if args.apply_redirects:
            problems += apply_redirects(args.api, token, redirect_pins, findings,
                                        args.config_dir)
        if findings and (args.apply or args.apply_redirects):
            hosts = fetch_hosts(args.api, token)
            redirects = fetch_redirects(args.api, token) if redirect_pins else []
            code, findings = check(pins, hosts, read_vhosts(args.config_dir),
                                   redirect_pins, redirects,
                                   read_redirect_files(args.config_dir))
        if args.check_destinations and redirect_pins:
            dead, unchecked = check_destinations(redirect_pins)
            findings += dead
            code = 1 if findings else 0
    except Unreadable as error:
        print(f"check-edge-vhost-pins: {error}", file=sys.stderr)
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, None, False)
        return 2

    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-edge-vhost-pins: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, to_audit(findings, unchecked), True)
    print(json.dumps({"findings": findings, "problems": problems}, indent=2)
          if args.json else report(findings))
    for problem in problems:
        print(f"{problem['check']}: {problem['domain']} — {problem['detail']}",
              file=sys.stderr)
    return code if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
