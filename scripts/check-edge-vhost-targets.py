#!/usr/bin/env python3
"""Every edge vhost's target must answer — and must be the target the estate declares.

WHY THIS EXISTS
---------------
Nginx Proxy Manager's vhosts are files (`/data/nginx/proxy_host/NN.conf`) written by its
UI. They are also edited by hand, because this estate has had to, and a hand edit is
exactly the change nobody re-checks. Two failures came out of that on 2026-10-29:

  * `admin.monarch.innotel.us` still forwarded to `127.0.0.1:4180`, from the time the SSO
    gateway shared the edge's network namespace. The gateway moved to its own namespace
    (`docker compose` — `cerulean-npm-sso`, address `172.31.42.10`), so nothing listened
    on the edge's loopback any more and the admin door answered **502** while the door
    next to it (`proxy.innotel.us`) answered 302 and looked healthy. A loopback target is
    only a target while the service shares the namespace; after the split it names
    nothing, and nginx cannot tell you that — it just proxies into a closed port.

  * the same door's peer map read `$remote_addr`, which the realip module had already
    rewritten from `X-Real-IP` — every request through the gateway looked like it came
    from the browser, so the edge refused its own gateway. Fixing it made the *gateway's*
    address part of the vhost contract: `172.31.42.10` must be the peer the edge trusts.

So the estate keeps the declaration. `scripts/edge-vhost-targets.tsv` names the vhosts
whose target is not free to drift — today, the two doors that front the admin UI through
the SSO gateway — and this script holds the running edge to it. A vhost not named there
is still liveness-checked; it is just not pinned.

WHAT IT CHECKS
--------------
  1. DRIFT — for every declared domain, the vhost on disk must name the declared
     `host:port`. `--apply` rewrites `set $server` / `set $port` in place (a `.bak` is
     kept beside the file) and tells you to reload; it does not reload for you, because
     a reload of nginx is an ingress event and belongs to whoever is watching it.
  2. LIVENESS — every target in the running config is TCP-connected. A connection is
     refused, times out, or answers: three facts, and only the first two are failures.
     Port 9 is the discard port and is where the estate *parks* a name it has retired
     (`ai.lms.innotel.us` is parked there on purpose) — reported as PARKED, not dead.

Run it on the edge host (`proxy`), where `/data/nginx` and the target addresses are real:

    ./scripts/check-edge-vhost-targets.py
    ./scripts/check-edge-vhost-targets.py --apply      # only when drift is the finding
    ./scripts/check-edge-vhost-targets.py --prom ${TEXTFILE_DIR}/estate-check-edge-vhost-targets.prom

Scheduled on the edge by `estate-checks.timer`, so drift and a dead target are
published (`innotel_estate_check_*`) rather than found by whoever tries the door next.

Exit codes: 0 = no drift and every target answered, 1 = drift or a dead target,
2 = no vhosts to read (wrong host, or NPM has never written a config).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import sys
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
CHECK = "edge_vhost_targets"
DEFAULT_CONFIG_DIR = Path("/data/nginx")
DEFAULT_EXPECT = SCRIPT_DIR / "edge-vhost-targets.tsv"

#: The discard port. Parking a name here retires it: the target is meant not to answer.
PARKED_PORT = 9

SERVER_RE = re.compile(r"^\s*set\s+\$server\s+\"([^\"]*)\";", re.M)
PORT_RE = re.compile(r"^\s*set\s+\$port\s+(\d+);", re.M)
NAME_RE = re.compile(r"^\s*server_name\s+([^;]+);", re.M)


@dataclass(frozen=True)
class Vhost:
    """One proxy host file, reduced to what can drift."""

    path: Path
    domains: tuple[str, ...]
    host: str
    port: int

    @property
    def name(self) -> str:
        return self.domains[0] if self.domains else self.path.stem

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def parked(self) -> bool:
        return self.port == PARKED_PORT


@dataclass(frozen=True)
class Declared:
    """A vhost target the estate forbids from drifting."""

    domain: str
    host: str
    port: int
    note: str = ""

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"


def parse_vhost(path: Path) -> Vhost | None:
    """Read `set $server`, `set $port` and `server_name` out of one vhost file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    server = SERVER_RE.search(text)
    port = PORT_RE.search(text)
    names = NAME_RE.search(text)
    if not (server and port and names):
        return None
    domains = tuple(names.group(1).split())
    return Vhost(path=path, domains=domains, host=server.group(1), port=int(port.group(1)))


def load_vhosts(config_dir: Path) -> list[Vhost]:
    """Every proxy host under `<config_dir>/proxy_host`, in file order."""
    root = config_dir / "proxy_host"
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob("*.conf")):
        vhost = parse_vhost(path)
        if vhost is not None:
            found.append(vhost)
    return found


def load_declaration(path: Path) -> list[Declared]:
    """Read the tab-separated declaration. Blank lines and `#` comments are skipped."""
    declared = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            raise ValueError(f"{path}: expected domain<TAB>host<TAB>port, got: {line!r}")
        domain, host, port = (fields[0].strip(), fields[1].strip(), fields[2].strip())
        note = fields[3].strip() if len(fields) > 3 else ""
        declared.append(Declared(domain=domain, host=host, port=int(port), note=note))
    return declared


def drift(vhosts: list[Vhost], declared: list[Declared]) -> list[tuple[Declared, Vhost | None]]:
    """Declarations whose live target is different (or whose vhost is gone)."""
    out = []
    for want in declared:
        live = next((v for v in vhosts if want.domain in v.domains), None)
        if live is None or (live.host, live.port) != (want.host, want.port):
            out.append((want, live))
    return out


def rewrite(vhost: Vhost, want: Declared) -> bool:
    """Point one vhost file at the declared target, keeping a `.bak` beside it."""
    text = vhost.path.read_text(encoding="utf-8")
    patched = SERVER_RE.sub(f'  set $server         "{want.host}";', text, count=1)
    patched = PORT_RE.sub(f"  set $port           {want.port};", patched, count=1)
    if patched == text:
        return False
    shutil.copy2(vhost.path, vhost.path.with_suffix(vhost.path.suffix + ".bak"))
    vhost.path.write_text(patched, encoding="utf-8")
    return True


def answers(host: str, port: int, timeout: float = 2.0) -> bool:
    """Is anything listening there? A TCP connect is the whole question."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check(
    config_dir: Path,
    expect: Path | None,
    apply: bool = False,
    timeout: float = 2.0,
    probe=answers,
) -> tuple[int, list[dict]]:
    """Run both halves. Returns (exit code, findings) so a test can read the verdict."""
    vhosts = load_vhosts(config_dir)
    if not vhosts:
        return 2, []

    findings: list[dict] = []

    if expect is not None:
        for want, live in drift(vhosts, load_declaration(expect)):
            fixed = False
            if apply and live is not None:
                fixed = rewrite(live, want)
            findings.append({
                "check": "drift",
                "domain": want.domain,
                "want": want.target,
                "got": live.target if live else None,
                "applied": fixed,
                "ok": fixed,
            })

    seen: set[str] = set()
    for vhost in vhosts:
        if vhost.target in seen:
            continue
        seen.add(vhost.target)
        if vhost.parked:
            findings.append({"check": "liveness", "target": vhost.target,
                             "domains": list(vhost.domains), "state": "parked", "ok": True})
            continue
        alive = probe(vhost.host, vhost.port, timeout)
        findings.append({"check": "liveness", "target": vhost.target,
                         "domains": list(vhost.domains),
                         "state": "answers" if alive else "dead", "ok": alive})

    failed = any(not f["ok"] for f in findings)
    return (1 if failed else 0), findings


def to_audit(findings: list[dict]) -> "Audit":
    """The same verdict in the shape every scheduled check publishes."""
    audit = Audit()
    for f in findings:
        if f["check"] == "drift" and not f["ok"]:
            got = f["got"] or "vhost missing"
            audit.findings.append(Finding(
                code="drift", level="fail",
                message=f"{f['domain']} wants {f['want']}, has {got}"))
        elif f["check"] == "liveness" and not f["ok"]:
            audit.findings.append(Finding(
                code="dead_target", level="fail",
                message=f"{f['target']} does not answer ({','.join(f['domains']) or '?'})"))
        elif f["state"] == "parked":
            audit.findings.append(Finding(
                code="parked", level="note",
                message=f"{f['target']} is parked ({','.join(f['domains'][:1])})"))
    return audit


def report(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        if f["check"] == "drift":
            got = f["got"] or "vhost missing"
            verdict = "APPLIED" if f["applied"] else "DRIFT"
            lines.append(f"{verdict:8} {f['domain']:34} wants {f['want']:22} has {got}")
        else:
            domains = ",".join(f["domains"])[:60]
            lines.append(f"{f['state'].upper():8} {f['target']:22} {domains}")
    dead = sum(1 for f in findings if f["check"] == "liveness" and not f["ok"])
    drifted = sum(1 for f in findings if f["check"] == "drift" and not f["ok"])
    lines.append("")
    lines.append(f"{len(findings)} finding(s): {drifted} drift, {dead} dead target(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when an edge vhost names a dead target.")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--expect", type=Path, default=DEFAULT_EXPECT,
                        help="declaration to hold the config to ('' to skip drifting)")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite 'set $server'/'set $port' to the declaration")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    expect = args.expect if str(args.expect) not in ("", "none") else None
    if expect is not None and not expect.is_file():
        print(f"no declaration at {expect} — nothing to hold the config to", file=sys.stderr)
        expect = None

    code, findings = check(args.config_dir, expect, args.apply, args.timeout)
    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-edge-vhost-targets: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, to_audit(findings) if findings else None, code != 2)
    if code == 2:
        print(f"no proxy hosts under {args.config_dir}/proxy_host", file=sys.stderr)
        return 2
    print(json.dumps(findings, indent=2) if args.json else report(findings))
    return code


if __name__ == "__main__":
    sys.exit(main())
