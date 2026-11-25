#!/usr/bin/env python3
"""Is the FreePBX GUI still *only* reachable through Cerulean Authentik?

WHY THIS EXISTS
---------------
`docs/pbx-sso-2026-11-25.md` made Authentik the only way into the FreePBX GUI, and
nothing else in the estate asserts that. Three separate things carry the property, and
each has an owner outside this repository that can undo it without saying so:

  * the rule lives in **NPM's database**, not in a file a reviewer reads. NPM writes
    `advanced_config` out at server level, and `location /` sets its own
    `proxy_set_header` lines, so the identity headers are only forwarded when they are
    repeated *inside* `location /admin`. Move them back to server level (the obvious
    "cleanup") and every request reaches the PBX with no identity — which the shim then
    refuses, so the GUI silently stops opening for everybody;
  * the rule lives in **`gui_auth.php`, which is FreePBX framework code**. A framework
    update overwrites it and the password form comes back on every public name. The
    marker `Sign-in is managed by Cerulean Authentik` is the only sign the SSO block is
    still there;
  * the rule lives in **iptables on the PBX host**, in `raw PREROUTING` (by
    `DOCKER-USER` the published port is already DNATed, so `--dport 8083` no longer
    matches). A docker restart or a hand `iptables -F` loses it, and the local password
    form is reachable from the whole LAN again.

There is also one thing that bites *after* the change rather than during it: the
break-glass 403 has to be guarded by `empty($_SESSION['AMP_user'])`. Without that guard
the shim authenticates the request and then 403s it anyway — a total lockout that looks
exactly like "no identity arrived", and cost an hour on 2026-11-25.

WHAT IT ASSERTS
---------------
  1. every FreePBX GUI name's NPM host still has `location /admin` with the five
     `X-authentik-*` proxy headers inside it, and the gate at server level;
  2. `pbx.innotel.site` (the Asterisk Console) still calls the PBX outpost and signs in
     through its own name, not the zone's;
  3. `gui_auth.php` still carries the marker, the break-glass path, and the session
     guard on the break-glass 403;
  4. `:8083` is still LAN-only — `raw PREROUTING` returns 192.168.1.0/24 and drops the
     rest, in that order — and the rule is still in the script that persists it;
  5. the break-glass flag is not left armed (a warning: an armed flag is a LAN-only
     password door).

Run it on the edge (where the NPM API and the PBX are both reachable), or anywhere with
the token and a key to the PBX:

    ./scripts/check-pbx-sso.py
    ./scripts/check-pbx-sso.py --npm-url http://192.168.1.71:81 --token-file /root/.npm-token
    ./scripts/check-pbx-sso.py --pbx 192.168.1.52 --ssh-key /root/.ssh/id_ed25519
    ./scripts/check-pbx-sso.py --prom ${TEXTFILE_DIR}/estate-check-pbx-sso.prom

Exit codes: 0 = all hold, 1 = drift, 2 = something could not be read.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # the estate's shared telemetry contract, beside this file when it is installed
    from estate_check import Audit, Finding, write_prom  # noqa: E402
    HAVE_TELEMETRY = True
except ImportError:  # a bare copy still has to run: --prom is then simply unavailable
    Audit = Finding = write_prom = None  # type: ignore[assignment]
    HAVE_TELEMETRY = False

#: The `check` label this check publishes under (`estate-checks.yml` alerts on it).
CHECK = "pbx_sso"

#: Every name that serves the FreePBX GUI. `fax.telno.services` is AvantFAX under `/fax/`
#: but the GUI itself is on the same host and the same shim, so it is one of these.
GUI_NAMES = (
    "voice.innotel.site",
    "voice.innotel.us",
    "pbx.telno.services",
    "pbx.it.com",
    "fax.telno.services",
)

CONSOLE_NAME = "pbx.innotel.site"
#: The dedicated outpost that carries only the console provider, so the `authentik Admins`
#: binding can be enforced. The zone outpost cannot enforce it (domain mode).
CONSOLE_OUTPOST = "authentik-pbx-outpost:9000"

SHIM_MARKER = "Sign-in is managed by Cerulean Authentik"
BREAK_GLASS = "/run/freepbx-sso-break-glass"
BREAK_GLASS_GUARD = "empty($_SESSION['AMP_user']) && !is_file(" + repr(BREAK_GLASS) + ")"
IDENTITY_HEADERS = ("username", "groups", "email", "name", "uid")
ACL_SCRIPT = "/usr/local/sbin/capstone-wan-acl.sh"
LAN = "192.168.1.0/24"


class Drift(Exception):
    """One assertion did not hold."""


class Unreadable(Exception):
    """The state could not be read — which is not the same as drift."""


def run(command: list[str], timeout: int = 25) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unreadable(f"{' '.join(command[:3])}…: {error}") from error
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise Unreadable(f"{' '.join(command[:3])}…: {detail[0] if detail else 'failed'}")
    return done.stdout


def location_block(config: str, name: str = "location /admin") -> str:
    """The text of one `location … { … }` block, or '' if it is not there."""
    start = config.find(name)
    if start < 0:
        return ""
    depth, index = 0, config.find("{", start)
    if index < 0:
        return ""
    for position in range(index, len(config)):
        if config[position] == "{":
            depth += 1
        elif config[position] == "}":
            depth -= 1
            if depth == 0:
                return config[index:position + 1]
    return ""


def npm_hosts(url: str, token: str) -> list[dict]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/nginx/proxy-hosts",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception as error:  # noqa: BLE001 — any failure here is "could not read"
        raise Unreadable(f"NPM API at {url}: {error}") from error


def host_for(hosts: list[dict], name: str) -> dict:
    for host in hosts:
        if name in (host.get("domain_names") or []):
            return host
    raise Drift(f"no NPM host serves {name}")


def check_gui_host(hosts: list[dict], name: str) -> list[str]:
    """`location /admin` carries the identity headers; the gate covers the rest."""
    config = host_for(hosts, name).get("advanced_config") or ""
    failures = []
    block = location_block(config)
    if not block:
        return [f"{name}: no `location /admin` in NPM advanced_config — the GUI is served "
                "by `location /`, which cannot forward the identity headers"]
    for field in IDENTITY_HEADERS:
        header = f"proxy_set_header X-authentik-{field} $authentik_{field};"
        if header not in block:
            failures.append(f"{name}: `location /admin` is missing `{header}`")
    # The gate must also cover the rest of the GUI (`/`, the FreePBX landing page).
    outside = config.replace(block, "")
    if "auth_request /outpost.goauthentik.io/auth/nginx;" not in outside:
        failures.append(f"{name}: the Authentik gate is not at server level, so `/` is open")
    if "error_page 401 = @goauthentik_proxy_signin;" not in outside:
        failures.append(f"{name}: no `error_page 401` handler at server level — an "
                        "unauthenticated request would get a bare 401")
    return failures


def check_console_host(hosts: list[dict]) -> list[str]:
    """The console keeps its own outpost and its own sign-in name."""
    config = host_for(hosts, CONSOLE_NAME).get("advanced_config") or ""
    failures = []
    outpost = location_block(config, "location /outpost.goauthentik.io")
    if CONSOLE_OUTPOST not in outpost:
        failures.append(f"{CONSOLE_NAME}: `location /outpost.goauthentik.io` does not proxy "
                        f"to {CONSOLE_OUTPOST}; the zone outpost cannot enforce the "
                        "`authentik Admins` binding")
    signin = location_block(config, "location @goauthentik_proxy_signin")
    if f"https://{CONSOLE_NAME}/outpost.goauthentik.io/start" not in signin:
        failures.append(f"{CONSOLE_NAME}: sign-in does not return through "
                        f"https://{CONSOLE_NAME}/outpost.goauthentik.io/start — the "
                        "console is using another domain's outpost")
    if "auth_request /outpost.goauthentik.io/auth/nginx;" not in config:
        failures.append(f"{CONSOLE_NAME}: no Authentik gate on the console")
    return failures


def check_shim(pbx: str, container: str, ssh_key: str | None) -> list[str]:
    """The FreePBX auth path still has the SSO block and the guarded break-glass."""
    path = "/var/www/html/admin/libraries/gui_auth.php"
    source = run(ssh_command(pbx, f"docker exec {container} cat {path}", ssh_key))
    failures = []
    if SHIM_MARKER not in source:
        failures.append(f"{path}: the SSO marker `{SHIM_MARKER}` is gone — a FreePBX "
                        "framework update has overwritten the file and the password "
                        "form is back on every public name")
        return failures
    if BREAK_GLASS not in source:
        failures.append(f"{path}: the break-glass path `{BREAK_GLASS}` is gone")
    if BREAK_GLASS_GUARD not in source:
        failures.append(f"{path}: the break-glass 403 is not guarded by the session — an "
                        "authenticated request is refused after SSO succeeds (lockout)")
    return failures


def ssh_command(pbx: str, command: str, ssh_key: str | None) -> list[str]:
    prefix = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
              "-o", "ConnectTimeout=10"]
    if ssh_key:
        prefix += ["-i", ssh_key, "-o", "IdentitiesOnly=yes"]
    return [*prefix, f"root@{pbx}", command]


def container_on(pbx: str, ssh_key: str | None) -> str:
    """The FreePBX container's name, which compose renames on every recreate."""
    names = [line.strip() for line in run(ssh_command(
        pbx, "docker ps --format '{{.Names}}' | grep '^freepbx-'", ssh_key)).splitlines()
        if line.strip()]
    if len(names) != 1:
        raise Unreadable(f"{pbx}: expected one freepbx-* container, found {names or 'none'}")
    return names[0]


def check_acl(pbx: str, ssh_key: str | None) -> list[str]:
    """`:8083` is LAN-only in `raw PREROUTING`, and the rule survives a reboot."""
    failures = []
    live = run(ssh_command(pbx, "iptables -t raw -S PREROUTING", ssh_key))
    lines = [line.strip() for line in live.splitlines()]
    allow = [i for i, line in enumerate(lines)
             if "-A PREROUTING" in line and f"-s {LAN}" in line
             and "--dport 8083" in line and line.endswith("-j RETURN")]
    drop = [i for i, line in enumerate(lines)
            if "-A PREROUTING" in line and "--dport 8083" in line
            and line.endswith("-j DROP")]
    if not allow:
        failures.append(f"{pbx}: no `raw PREROUTING` RETURN for {LAN} on :8083 — the LAN "
                        "cannot reach the break-glass form")
    if not drop:
        failures.append(f"{pbx}: no `raw PREROUTING` DROP for :8083 — the port is open "
                        "to whatever can route to the host")
    if allow and drop and allow[0] > drop[0]:
        failures.append(f"{pbx}: the :8083 DROP is before the {LAN} RETURN, so the LAN is "
                        "dropped too (the first matching rule wins)")
    persisted = run(ssh_command(pbx, f"grep -c -- '--dport 8083' {ACL_SCRIPT}", ssh_key)).strip()
    if persisted in ("", "0"):
        failures.append(f"{pbx}: {ACL_SCRIPT} does not carry the :8083 rule — it is lost "
                        "on the next reboot or docker restart")
    return failures


def check_break_glass(pbx: str, container: str, ssh_key: str | None) -> list[str]:
    """An armed flag is a password door; report it, do not fail on it (it is deliberate)."""
    armed = run(ssh_command(pbx, f"docker exec {container} test -e {BREAK_GLASS} && echo armed "
                                 "|| echo disarmed", ssh_key)).strip()
    return [] if armed == "disarmed" else [f"{BREAK_GLASS} is armed in {container}: the "
                                           "local password form is open to the LAN"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the PBX's Authentik-only sign-in.")
    parser.add_argument("--npm-url", default="http://127.0.0.1:81")
    parser.add_argument("--token-file", default="/root/.npm-token")
    parser.add_argument("--pbx", default="192.168.1.52")
    parser.add_argument("--container", help="FreePBX container (default: discover freepbx-*)")
    parser.add_argument("--ssh-key")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    audit = Audit() if HAVE_TELEMETRY else None

    def fail(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="fail", message=message))

    def warn(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="warn", message=message))

    def publish(ran_ok: bool) -> None:
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, audit, ran_ok)
        elif args.prom:
            print("check-pbx-sso: --prom needs estate_check.py beside this file",
                  file=sys.stderr)

    failures: list[str] = []
    warnings: list[str] = []
    try:
        with open(args.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
        hosts = npm_hosts(args.npm_url, token)
    except (OSError, Unreadable) as error:
        print(f"FAIL  {error}", file=sys.stderr)
        publish(False)
        return 2

    for name in GUI_NAMES:
        found = check_gui_host(hosts, name)
        failures += found
        for message in found:
            fail("gui_headers", message)
        print(f"{'FAIL' if found else 'PASS'}  {name:24} location /admin forwards identity")
    found = check_console_host(hosts)
    failures += found
    for message in found:
        fail("console_outpost", message)
    print(f"{'FAIL' if found else 'PASS'}  {CONSOLE_NAME:24} own outpost and sign-in name")

    try:
        container = args.container or container_on(args.pbx, args.ssh_key)
        found = check_shim(args.pbx, container, args.ssh_key)
        failures += found
        for message in found:
            fail("shim", message)
        print(f"{'FAIL' if found else 'PASS'}  {'gui_auth.php':24} SSO block present and "
              "break-glass guarded")
        found = check_acl(args.pbx, args.ssh_key)
        failures += found
        for message in found:
            fail("port_acl", message)
        print(f"{'FAIL' if found else 'PASS'}  {':8083':24} LAN-only in raw PREROUTING")
        warnings += check_break_glass(args.pbx, container, args.ssh_key)
    except Unreadable as error:
        print(f"FAIL  {error}", file=sys.stderr)
        if audit is not None:
            audit.findings.append(Finding(code="unreadable", level="fail", message=str(error)))
        publish(False)
        return 2

    for warning in warnings:
        warn("break_glass_armed", warning)
    for failure in failures:
        print(f"      {failure}")
    for warning in warnings:
        print(f"WARN  {warning}")
    print()
    print("the PBX signs in through Authentik only" if not failures
          else f"{len(failures)} assertion(s) no longer hold")
    publish(True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
