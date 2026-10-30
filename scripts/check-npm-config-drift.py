#!/usr/bin/env python3
"""NPM's own record of a host and the file it wrote must say the same thing.

WHY THIS EXISTS
---------------
Nginx Proxy Manager keeps the truth about a host in its database (MySQL on this estate —
`cerulean-npm-db`, which is why this reads the API and not `database.sqlite`) and writes
`nginx/proxy_host/NN.conf` from it when you press save. That write is the whole contract,
and on 2026-10-30 it turned out not to hold in either direction:

  * **A row deleted, the file left standing.** Twenty-five rows were deleted through the
    API for names whose backend had moved off the home edge. The API said they were gone
    (`rows 215 -> 190`) and `nginx -t` was clean — because the *files* were still there and
    still included. `/data/nginx/proxy_host/` held 207 files for 190 rows, and those 25
    doors were still proxying into ports that no longer answered (502 for anyone who
    typed the name). `check-edge-vhost-targets.py` did see them, as dead targets, but the
    cause — a UI that no longer knows the door exists — is this check's question.

  * **A row edited, the file not rewritten.** Parking `resend.innotel.us` set the row's
    `forward_port` to 9; the API read back 9 and `220.conf` still said `8099`. Disabling
    and re-enabling the row did not help either. nginx serves the file, so the estate's
    intent and the estate's behaviour were two different numbers with no symptom until
    somebody dialled it.

Neither is visible in a config diff, and neither is a liveness question: a door onto a
live port is just as wrong when its row is gone (nobody can turn it off from the UI) as
when its row says something else (turning it off does nothing).

WHAT IT CHECKS
--------------
For every proxy and redirection host in NPM, and every generated `.conf` on disk:

  1. UNMANAGED — a file whose `server_name`s belong to no host. The edge is serving a
     door NPM cannot delete, edit or show you. This is the 25.
  2. MISSING — an enabled host with no file. The UI shows a door nginx does not serve.
  3. STALE TARGET — the host and the file both exist and name different `host:port`.
     `set $server`/`set $port` is what nginx proxies to; the row is what the UI shows.
  4. DISABLED BUT SERVED — a host with `enabled: false` whose file is still in the
     include directory. Disabling is supposed to remove it; a disabled door that still
     answers is the worst version of (3), because everyone believes it is off.

Redirection hosts have no target to compare (`forward_domain_name` is resolved by
nginx), so they are checked for presence only.

The row side comes from the API, not the database file: this estate's NPM is MySQL-backed,
so `/data/database.sqlite` is a zero-byte leftover and a check that read it would have
reported an empty estate and called every door an orphan. The credential is
`NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` from the environment — the same pair the estate's
other NPM scripts use — and there is no default: a missing credential is exit 2, because
a check that cannot see the rows is not a check that found nothing.

Run it on the edge host, inside the container that holds NPM's data volume:

    NPM_ADMIN_EMAIL=… NPM_ADMIN_PASSWORD=… ./scripts/check-npm-config-drift.py \\
        --api http://192.168.1.71:81 --data-dir /usr/src/proxy/data
    ... --prom ${TEXTFILE_DIR}/estate-check-npm-config-drift.prom

Exit codes: 0 = the two copies agree, 1 = they disagree, 2 = a copy could not be read
(that is not a pass: an unreadable database means nothing is being compared).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
CHECK = "npm_config_drift"
DEFAULT_API = "http://192.168.1.71:81"
DEFAULT_DATA_DIR = Path("/data")
#: NPM's directory per kind, and the API path that lists the same kind.
KINDS = {
    "proxy_host": "/api/nginx/proxy-hosts",
    "redirection_host": "/api/nginx/redirection-hosts",
}

SERVER_RE = re.compile(r'^\s*set\s+\$server\s+"([^"]*)";', re.M)
PORT_RE = re.compile(r"^\s*set\s+\$port\s+(\d+);", re.M)
NAME_RE = re.compile(r"^\s*server_name\s+([^;]+);", re.M)


@dataclass(frozen=True)
class Row:
    """One host as NPM describes it."""

    kind: str
    id: int
    domains: tuple[str, ...]
    host: str | None = None
    port: int | None = None
    enabled: bool = True

    @property
    def target(self) -> str:
        if self.host is None:
            return "(redirect)"
        return f"{self.host}:{self.port}" if self.port is not None else self.host


@dataclass(frozen=True)
class Vhost:
    """One generated file, reduced to what can disagree with its host."""

    path: Path
    domains: tuple[str, ...]
    host: str | None = None
    port: int | None = None

    @property
    def target(self) -> str:
        if self.host is None:
            return "(no target)"
        return f"{self.host}:{self.port}" if self.port is not None else self.host


class Unreadable(RuntimeError):
    """NPM answered something other than a host list — a check that cannot see rows."""


def _call(url: str, payload: dict | None = None, token: str | None = None,
          timeout: float = 20.0):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
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
    try:
        reply = _call(api.rstrip("/") + "/api/tokens", {"identity": email, "secret": secret})
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM did not issue a token: {error}") from error
    if not isinstance(reply, dict) or not reply.get("token"):
        raise Unreadable("NPM refused the credential")
    return reply["token"]


def fetch_rows(api: str, token: str, kind: str) -> list[Row]:
    """Every host of one kind, in the shape the comparison needs."""
    try:
        hosts = _call(api.rstrip("/") + KINDS[kind], token=token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"NPM did not list {kind}: {error}") from error
    if not isinstance(hosts, list):
        raise Unreadable(f"NPM returned no {kind} list")
    rows = []
    for host in hosts:
        port = host.get("forward_port")
        rows.append(Row(
            kind=kind,
            id=int(host["id"]),
            domains=tuple(host.get("domain_names") or ()),
            host=host.get("forward_host") if kind == "proxy_host" else None,
            port=int(port) if port is not None and kind == "proxy_host" else None,
            enabled=bool(host.get("enabled", True)),
        ))
    return rows


def read_vhosts(config_dir: Path, kind: str) -> list[Vhost]:
    """Every generated `.conf` for one kind. `.bak`/`.orig` copies are not included."""
    root = config_dir / kind
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


def compare(rows: list[Row], vhosts: list[Vhost], kind: str) -> list[dict]:
    """Every way one kind's two copies can disagree."""
    by_domain: dict[str, Vhost] = {}
    for vhost in vhosts:
        for domain in vhost.domains:
            by_domain.setdefault(domain, vhost)
    owned = {domain for row in rows for domain in row.domains}

    findings: list[dict] = []
    for vhost in vhosts:
        if not any(domain in owned for domain in vhost.domains):
            findings.append({
                "check": "unmanaged_vhost",
                "kind": kind,
                "file": vhost.path.name,
                "domains": list(vhost.domains),
                "target": vhost.target,
            })

    for row in rows:
        live = next((by_domain[d] for d in row.domains if d in by_domain), None)
        if live is None:
            if row.enabled:
                findings.append({
                    "check": "missing_vhost",
                    "kind": kind,
                    "row": row.id,
                    "domains": list(row.domains),
                    "target": row.target,
                })
            continue
        if not row.enabled:
            findings.append({
                "check": "disabled_but_served",
                "kind": kind,
                "row": row.id,
                "file": live.path.name,
                "domains": list(row.domains),
                "target": live.target,
            })
        elif kind == "proxy_host" and row.port is not None and live.port is not None:
            if (row.host, row.port) != (live.host, live.port):
                findings.append({
                    "check": "stale_target",
                    "kind": kind,
                    "row": row.id,
                    "file": live.path.name,
                    "domains": list(row.domains),
                    "row_target": row.target,
                    "file_target": live.target,
                })
    return findings


def check(data_dir: Path, rows_by_kind: dict[str, list[Row]]) -> tuple[int, list[dict]]:
    """Compare both kinds. Returns (exit code, findings) so a test can read the verdict."""
    if not (data_dir / "proxy_host").is_dir():
        return 2, []
    findings: list[dict] = []
    for kind in KINDS:
        findings.extend(compare(rows_by_kind.get(kind, []),
                                read_vhosts(data_dir, kind), kind))
    return (1 if findings else 0), findings


def to_audit(findings: list[dict]) -> "Audit":
    """The same verdict in the shape every scheduled check publishes."""
    audit = Audit()
    for f in findings:
        domains = ",".join(f["domains"][:3])
        if f["check"] == "unmanaged_vhost":
            audit.findings.append(Finding(
                code="unmanaged_vhost", level="fail",
                message=(f"edge serves {domains} from {f['file']} -> {f['target']} "
                         "but no NPM host owns it")))
        elif f["check"] == "missing_vhost":
            audit.findings.append(Finding(
                code="missing_vhost", level="fail",
                message=f"NPM host {f['row']} ({domains}) is enabled but has no vhost file"))
        elif f["check"] == "stale_target":
            audit.findings.append(Finding(
                code="stale_target", level="fail",
                message=(f"{domains}: host {f['row']} says {f['row_target']}, "
                         f"{f['file']} proxies to {f['file_target']}")))
        elif f["check"] == "disabled_but_served":
            audit.findings.append(Finding(
                code="disabled_but_served", level="fail",
                message=(f"NPM host {f['row']} ({domains}) is disabled, yet {f['file']} "
                         f"still serves it -> {f['target']}")))
    return audit


def report(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        domains = ",".join(f["domains"])[:60]
        if f["check"] == "stale_target":
            lines.append(f"{'DRIFT':8} {domains:48} host {f['row_target']:20} file {f['file_target']}")
        elif f["check"] == "missing_vhost":
            lines.append(f"{'MISSING':8} {domains:48} host {f['row']} {f['target']}")
        elif f["check"] == "disabled_but_served":
            lines.append(f"{'SERVED':8} {domains:48} host {f['row']} disabled, file {f['file']}")
        else:
            lines.append(f"{'ORPHAN':8} {domains:48} file {f['file']} -> {f['target']}")
    lines.append("")
    unmanaged = sum(1 for f in findings if f["check"] == "unmanaged_vhost")
    lines.append(
        f"{len(findings)} finding(s): {unmanaged} orphan file(s), "
        f"{len(findings) - unmanaged} row/file mismatch(es)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when NPM's hosts and its generated vhosts disagree.")
    parser.add_argument("--api", default=os.environ.get("NPM_API", DEFAULT_API),
                        help="NPM's API base (default %(default)s)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="NPM's data directory (holds nginx/proxy_host)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        token = token_for(args.api, os.environ.get("NPM_ADMIN_EMAIL", ""),
                          os.environ.get("NPM_ADMIN_PASSWORD", ""))
        rows_by_kind = {kind: fetch_rows(args.api, token, kind) for kind in KINDS}
    except Unreadable as error:
        code, findings, rows_by_kind = 2, [], None
        print(f"check-npm-config-drift: {error}", file=sys.stderr)
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, None, False)
        return code

    code, findings = check(args.data_dir / "nginx", rows_by_kind)
    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-npm-config-drift: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, to_audit(findings) if code != 2 else None, code != 2)
    if code == 2:
        print(f"could not read NPM's vhosts under {args.data_dir}/nginx", file=sys.stderr)
        return 2
    print(json.dumps(findings, indent=2) if args.json else report(findings))
    return code


if __name__ == "__main__":
    sys.exit(main())
