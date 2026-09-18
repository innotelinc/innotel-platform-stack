#!/usr/bin/env python3
"""Fail when a service target names a gateway door that is not open.

WHY THIS EXISTS
---------------
The platform runs one OmniRoute gateway, and its own port (`20128`) is published on
its host's loopback and docker0 only. That is not tidiness: `make gateway-auth-mode`
sets `requireLogin=false` so Cerulean Authentik is the gateway's only gate, and after
that the reachability of that port *is* the control — a LAN binding would publish a
dashboard that can read every provider credential. The door for everything else is
the identity-aware proxy in front of it (`20129`), which exempts `/v1` for API
clients because they send a key, not a session cookie.

`5-dev/olympus/docs/gateway-sso.md` records what happened when the port stopped being
bound on the LAN: five consumers were still pointing at it, and every one of them
failed in a way that looked like something else —

  * `.30`'s n8n and dashboard-api dialled `host.docker.internal:20128`, which is
    *their own* docker0, and had been silently broken since the host split;
  * PLUTUS's Convex functions fell back to `localhost:20128` inside their own
    container, so every AI call failed with a connection refused;
  * Studio's own healthcheck reported `BLOCKED — gateway reachability` and nothing
    else said why;
  * `rizz-api` named `omniroute:20128` — a compose service on a different project's
    network, which can never resolve.

This is that check, so the next move does not have to be found by hand. It is the
estate-wide half, kept in the estate-wide repo, like `check-sign-in-posture.sh`.

THE RULES
---------
1. **The gateway's own port is not a routable target.** Any target naming
   `<host>:20128` is wrong, whatever the host is: the port answers on loopback and
   on the gateway host's docker0, and nowhere else.
2. **Not under a docker alias from another host.** `host.docker.internal:20128`
   resolves to the *dialer's* docker0, so it is right only in a file that declares
   the gateway itself (its compose, on the gateway's host) — or on the gateway's
   host, which the exemption table below records per deployment.
3. **A compose service name is not an address.** `omniroute:20128` in a file that
   does not declare an `omniroute` service can never resolve; a different compose
   project is not on that service's network.
4. **Loopback is conditional.** `127.0.0.1:20128` is the gateway's actual binding —
   on the gateway's own host, for a host-mode process. Inside a container, and on
   any other host, it is the caller itself. Reported, not failed.

Ports *bindings* (`- "172.17.0.1:20128:20128"`, `${OMNIROUTE_PORT:-20128}:20128`)
are where a port is listened on, not where a service is dialled, so they are
excluded — the same exclusion `2-voice/capstone`'s CI job makes. Comments are not
configuration either, and are stripped before scanning.

WHAT IT REPORTS BUT CANNOT DECIDE, in a third list (`conditional`). Both of these
were wrong in exactly one deployment and right in another, and both were found by
hand because this check said nothing:

  * **A file that declares the gateway, that runs on more than one host.**
    `2-voice/capstone/docker-compose.yml` declares it, so rule 2 makes its
    `host.docker.internal:20128` targets legitimate — on `.46` they are, and on
    `.30`, which also runs that compose, the alias is n8n's own docker0 and both
    were dead. Read a declaring file as "right only where the gateway runs", never
    as proof.
  * **Loopback** (rule 4). It is the gateway's real binding on its own host, so a
    file that names it may be the correct one — `olympus`'s SSO proxy upstream does.
    A container on that same host resolves it to itself, which is how `.46`'s shell
    export of `http://localhost:20128` reached `onyx-ai`.

Neither list decides anything: the scan fails only on a *reason*. Printing them is
the point — silence is what let both of these through.

USAGE
-----
    ./scripts/check-gateway-targets.py                  # the estate this lives in
    ./scripts/check-gateway-targets.py --root /path     # repeatable; default: estate
    ./scripts/check-gateway-targets.py --live           # also prove the assumption
    ./scripts/check-gateway-targets.py --json

`--live` asks Docker what the gateway actually publishes and fails if the port has
widened beyond loopback + the host's bridge — which would make this check's premise
false rather than merely stale. It is skipped, with a note, where there is no Docker
or no gateway container (this check runs on any host).

Exit codes:
    0  no violations (exemptions, and files scanned, are printed)
    1  at least one violation, or `--live` found the premise false
    2  cannot run (no root to scan)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The platform's one gateway and the door in front of it. Both are recorded here
# rather than discovered, because this has to run on a host that cannot reach the
# gateway at all — which is exactly the host where a stale target does damage.
GATEWAY_HOST = "192.168.1.46"
GATEWAY_PORT = 20128
PROXY_PORT = 20129

# The names the gateway answers to inside its own project. `2-voice/capstone` declares
# the service as `omniroute` and names the container `omniroute`, which compose makes a
# valid DNS name on that project's network. A different project has neither.
# `g2-omniroute` is kept as an accepted spelling, not because anything owns it: it is
# the group-prefixed name the hand-written group files used, and a file that still
# dials it is stale the same way one that dials the loopback port is — the tolerant
# name keeps this check reading as a *target* error, not a name error.
GATEWAY_NAMES = {"omniroute", "g2-omniroute"}

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

# Docker's name for the host's own bridge gateway.
DOCKER_ALIASES = {"host.docker.internal", "gateway.docker.internal", "docker.for.mac.host.internal"}

# Where a port is listened on, not dialled: `20128:20128`, `172.17.0.1:20128:20128`,
# `${OMNIROUTE_PORT:-20128}:20128`, optionally quoted, optionally a `- ` list item.
BINDING = re.compile(r"^\s*-?\s*[\"']?[^\s/\"'=]+:\d+(?::\d+)?(?:/(?:udp|tcp))?[\"']?\s*$")

# `host:port`, in a value or a URL, with or without a scheme.
TARGET = re.compile(r"(?P<host>[A-Za-z0-9_.-]+):(?P<port>\d{2,5})(?![0-9])")

# A saved copy of a config is not a target: `x.env.bak-20260916`, `x.env.pre-npm-edge-move`.
# Reported, these bury the findings that matter under a history of the file.
BACKUP = re.compile(r"(\.bak|\.orig|\.old|\.save|\.swp|\.tmp|~$|\.pre-|bak-)")

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    "site-packages",
    ".terraform",
}


@dataclass(frozen=True)
class Exemption:
    """A path whose target really is the gateway's own port, and why."""

    suffix: str
    why: str


# Every entry is a claim that needs to stay true. Keys are path suffixes, so they
# survive a checkout landing somewhere else.
EXEMPTIONS: tuple[Exemption, ...] = (
    Exemption(
        "5-dev/distro/.env",
        "the distro control plane runs ON the gateway's host, so its docker0 alias "
        "is the right address, and it logs in with the management password before "
        "every call (POST /api/auth/login still answers 200 with a cookie under "
        "requireLogin=false — measured, because a 4xx there would break tenant key "
        "provisioning)",
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    text: str
    reason: str
    exempt: str = ""
    # Set instead of a reason when the target is right in one deployment and wrong
    # in another, and this cannot tell which one it is looking at. Reported, never
    # failed: see the two cases in the module docstring.
    note: str = ""

    @property
    def is_violation(self) -> bool:
        return not self.exempt and not self.note

    @property
    def is_conditional(self) -> bool:
        return bool(self.note) and not self.exempt


def looks_like_config(path: Path) -> bool:
    """Is this a file a service target can live in?

    `.env`, `.env.local`, `.env.example` and friends, plus the plain `something.env`
    that `env_file:` and this estate's host-side scripts both use (`pbx.env`), and the
    three structured formats the stacks configure themselves with.
    """
    name = path.name
    if name == ".env" or name.startswith(".env."):
        return True
    return path.suffix in {".env", ".yml", ".yaml", ".json"}


def declared_names(path: Path) -> set[str]:
    """The DNS names this file's own project gives its services.

    A compose service name and a `container_name` are both valid addresses *within
    their project* — compose puts a project's services on one network and resolves
    both — so `g2-omniroute:20128` is right in the compose that declares it and wrong
    anywhere else. That is the whole of rule 3, and it is why this reads the file
    instead of assuming one name. Read by indentation rather than by parsing YAML:
    the same function is handed `.env` files and JSON, where it simply finds nothing.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    names = set(re.findall(r"container_name:\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", text))
    in_services = False
    for raw in text.splitlines():
        if re.match(r"^services:\s*$", raw):
            in_services = True
            continue
        if in_services and raw and not raw.lstrip().startswith("#") and re.match(r"^\S", raw):
            in_services = False
        if in_services:
            service = re.match(r"^\s{1,4}([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$", raw)
            if service:
                names.add(service.group(1))
    return names


def exemption_for(path: Path) -> str:
    as_posix = path.as_posix()
    for exemption in EXEMPTIONS:
        if as_posix.endswith(exemption.suffix):
            return exemption.why
    return ""


def classify(host: str, declared: set[str]) -> tuple[str, str]:
    """Why this target is wrong, or why it cannot be decided — `(reason, note)`.

    A *reason* is a violation. A *note* is a target that is correct in one
    deployment and wrong in another, which a file cannot tell us: a compose file
    runs on whichever hosts it is deployed to, and a loopback address is the
    gateway only where the gateway is. Both empty means silence — a sanctioned
    address (the door), or a name this very file's project defines.
    """
    if host in LOOPBACK:
        return "", (
            f"`{host}` is the gateway's own binding, so it is right for a host-mode "
            f"process on the gateway's host and is the caller itself everywhere else, "
            f"containers included; the routable door is {GATEWAY_HOST}:{PROXY_PORT}"
        )
    if host in DOCKER_ALIASES:
        if declared & GATEWAY_NAMES:
            return "", (
                f"this file declares the gateway, so `{host}` is its docker0 on the "
                f"gateway's host — and the caller's own docker0 on any other host that "
                f"runs this same file, where the target is dead. Dial "
                f"{GATEWAY_HOST}:{PROXY_PORT} from anywhere but the gateway's host"
            )
        return (
            f"`{host}` is the dialer's own docker0, where nothing listens unless the "
            f"dialer is on the gateway's host; dial the proxy at {GATEWAY_HOST}:{PROXY_PORT}"
        ), ""
    if host in declared:
        return "", ""
    if host in GATEWAY_NAMES:
        return (
            f"`{host}` is a compose service name and resolves only on that "
            f"service's network, which a different compose project is not on; dial the "
            f"proxy at {GATEWAY_HOST}:{PROXY_PORT}"
        ), ""
    return (
        f"port {GATEWAY_PORT} is bound to the gateway host's loopback and docker0 only "
        f"(no LAN binding, because that port's reachability is the entire control once "
        f"its own login is off); dial the proxy at {GATEWAY_HOST}:{PROXY_PORT}"
    ), ""


def scan_file(path: Path, root: Path) -> list[Finding]:
    declared = declared_names(path)
    exempt = exemption_for(path)
    findings: list[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return findings
    try:
        shown = path.relative_to(root).as_posix()
    except ValueError:
        shown = path.as_posix()
    for number, raw in enumerate(lines, start=1):
        code = raw.split("#", 1)[0].rstrip()
        if not code or BINDING.match(code):
            continue
        for match in TARGET.finditer(code):
            if int(match.group("port")) != GATEWAY_PORT:
                continue
            host = match.group("host")
            reason, note = classify(host, declared)
            if not reason and not note:
                continue
            findings.append(Finding(shown, number, match.group(0), reason, exempt, note))
    return findings


def walk(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and looks_like_config(path) and not BACKUP.search(path.name):
            found.append(path)
    return found


def default_roots(script: Path) -> list[Path]:
    """The estate this script lives in, or the checkout itself when it stands alone.

    `check-sign-in-posture.sh` uses the same convention: two levels up from `ips/` is
    the estate (`1-primary/` … `5-dev/`), and in a lone checkout there is nothing
    above to scan.
    """
    estate = script.resolve().parent.parent.parent
    if any((estate / group).is_dir() for group in ("1-primary", "2-voice", "3-media", "4-value", "5-dev")):
        return [estate]
    return [script.resolve().parent.parent]


def live_check(container: str) -> tuple[bool, str]:
    """Has the premise of this check gone stale? (ok, note)."""
    try:
        result = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .NetworkSettings.Ports}}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return True, f"skipped: docker is not usable here ({error})"
    if result.returncode != 0:
        return True, f"skipped: {container} is not running on this host, so its bindings cannot be read"
    try:
        ports = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return True, "skipped: docker reported the ports in a shape this could not read"

    hosts = {
        str(binding.get("HostIp") or "")
        for container_port, bindings in ports.items() if container_port.endswith("/tcp")
        for binding in (bindings or [])
    }
    if not hosts:
        return True, "skipped: the gateway container publishes no TCP port"

    bridge = _bridge_gateway()
    local = {"127.0.0.1", "::1"} | ({bridge} if bridge else set())
    beyond = {h for h in hosts if h not in local}
    if beyond:
        return False, (
            "the gateway publishes "
            + ", ".join(sorted(h or "0.0.0.0 (all interfaces)" for h in beyond))
            + f" — port {GATEWAY_PORT} is on the LAN again, so this check's premise is "
            "false: a requireLogin=false gateway reachable off-host publishes a "
            "dashboard that can read every provider credential"
        )
    return True, f"gateway publishes {GATEWAY_PORT} on {', '.join(sorted(hosts))} (this host only), as assumed"


def _bridge_gateway() -> str:
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", "bridge", "--format", "{{range .IPAM.Config}}{{.Gateway}} {{end}}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").split()[0] if (result.stdout or "").split() else ""


def main(argv: list[str] | None = None) -> int:
    script = Path(__file__)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", action="append", default=[], help="estate root to scan (repeatable)")
    parser.add_argument("--live", action="store_true", help="also check the gateway's real bindings")
    parser.add_argument("--container", default="omniroute", help="gateway container (default omniroute)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    roots = [Path(r).resolve() for r in args.root] or default_roots(script)
    roots = [r for r in roots if r.is_dir()]
    if not roots:
        print("nothing to scan — pass --root", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    scanned = 0
    for root in roots:
        for path in walk(root):
            scanned += 1
            findings.extend(scan_file(path, root))

    live_ok, live_note = (True, "")
    if args.live:
        live_ok, live_note = live_check(args.container)

    violations = [f for f in findings if f.is_violation]
    exemptions = [f for f in findings if f.exempt]
    # Right in one deployment, wrong in another. Printed rather than passed over:
    # both of the cases this cannot decide were found by hand while it stayed quiet.
    conditional = [f for f in findings if f.is_conditional]

    if args.json:
        print(json.dumps({
            "roots": [str(r) for r in roots],
            "scanned": scanned,
            "gateway": {"host": GATEWAY_HOST, "port": GATEWAY_PORT, "proxy_port": PROXY_PORT},
            "violations": [f.__dict__ for f in violations],
            "exempt": [f.__dict__ for f in exemptions],
            "conditional": [f.__dict__ for f in conditional],
            "live": {"ok": live_ok, "note": live_note} if args.live else None,
        }, indent=2))
        return 1 if violations or not live_ok else 0

    print(f"gateway   {GATEWAY_HOST}:{GATEWAY_PORT} — loopback + the gateway host's bridge only")
    print(f"the door  {GATEWAY_HOST}:{PROXY_PORT} (the SSO proxy in front of it, which exempts /v1)")
    for root in roots:
        print(f"root      {root}")
    print(f"scanned   {scanned} config file(s)")
    if args.live:
        print(("live      ok — " if live_ok else "live      FAILED — ") + live_note)

    if exemptions:
        print(f"\nexempt ({len(exemptions)}) — targets that really are the gateway's own port")
        for finding in exemptions:
            print(f"  {finding.path}:{finding.line}  {finding.text}")
            print(f"      {finding.exempt}")

    if violations:
        print(f"\nviolations ({len(violations)})")
        for finding in violations:
            print(f"  {finding.path}:{finding.line}  {finding.text}")
            print(f"      {finding.reason}")

    if conditional:
        print(f"\nconditional ({len(conditional)}) — right in one deployment, wrong in another")
        for finding in conditional:
            print(f"  {finding.path}:{finding.line}  {finding.text}")
            print(f"      {finding.note}")

    if violations:
        print("\nfailed: a service target names a gateway door that is not open")
        return 1

    tail = f" ({len(conditional)} conditional, above)" if conditional else ""
    print(f"\nok: no service target dials the gateway's own port{tail}")
    return 0 if live_ok else 1


if __name__ == "__main__":
    sys.exit(main())
