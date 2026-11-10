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

`--apply` fixes (2) and (3) by PUTting the row with every other field it already had,
then reading the whole row back and requiring it to be unchanged apart from the target
and `modified_on` — a field that goes missing on the way through is exactly how a
"small" API edit breaks a door. It then waits for the generated file to agree. Nothing
is written unless a pin is actually drifted, and `--apply` on a clean estate writes
nothing at all.

Run it on the edge host, where the API and the vhost tree are both real:

    ./scripts/check-edge-vhost-pins.py
    ./scripts/check-edge-vhost-pins.py --apply      # only when drift is the finding
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
import sys
import time
import urllib.error
import urllib.request
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
PROXY_HOSTS = "/api/nginx/proxy-hosts"

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


def check(pins: list[Pin], hosts: list[Host], vhosts: list[Vhost]) -> tuple[int, list[dict]]:
    """Returns (exit code, findings) so a test can read the verdict."""
    findings = compare(pins, hosts, vhosts)
    return (1 if findings else 0), findings


def to_audit(findings: list[dict]) -> "Audit":
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
    return audit


def report(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        if f["check"] == "no_host":
            lines.append(f"{'NO-HOST':9} {f['domain']:32} pin {f['pin']:22} no NPM host")
        elif f["check"] == "row_drift":
            lines.append(f"{'ROW-DRIFT':9} {f['domain']:32} pin {f['pin']:22} "
                         f"row {f['row_target']} (host {f['row']})")
        else:
            got = f["file_target"] or "no file"
            lines.append(f"{'FILE-LAG':9} {f['domain']:32} pin {f['pin']:22} "
                         f"file {got}")
    lines.append("")
    drifted = sum(1 for f in findings if f["check"] == "row_drift")
    lagged = sum(1 for f in findings if f["check"] == "file_lag")
    orphan = sum(1 for f in findings if f["check"] == "no_host")
    lines.append(f"{len(findings)} finding(s): {drifted} row drift, {lagged} file lag, "
                 f"{orphan} pinned name NPM does not have")
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


def put_host(api: str, token: str, host_id: int, body: dict) -> dict:
    """Update one host through the API — the write that also regenerates its file."""
    try:
        reply = _call(f"{api.rstrip('/')}{PROXY_HOSTS}/{host_id}", body, token=token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM would not update host {host_id}: {error}") from error
    return reply if isinstance(reply, dict) else {}


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
    parser.add_argument("--apply", action="store_true",
                        help="write each drifted pin through NPM's API")
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

    try:
        token = token_for(args.api, os.environ.get("NPM_ADMIN_EMAIL", ""),
                          os.environ.get("NPM_ADMIN_PASSWORD", ""))
        hosts = fetch_hosts(args.api, token)
        code, findings = check(pins, hosts, vhosts)
        problems = (apply_pins(args.api, token, pins, findings, args.config_dir)
                    if args.apply else [])
        if args.apply and findings:
            hosts = fetch_hosts(args.api, token)
            code, findings = check(pins, hosts, read_vhosts(args.config_dir))
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
            write_prom(args.prom, CHECK, to_audit(findings), True)
    print(json.dumps({"findings": findings, "problems": problems}, indent=2)
          if args.json else report(findings))
    for problem in problems:
        print(f"{problem['check']}: {problem['domain']} — {problem['detail']}",
              file=sys.stderr)
    return code if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
