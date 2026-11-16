#!/usr/bin/env python3
"""Fail when an OIDC sign-in can't complete because its redirect URI is wrong.

WHY THIS EXISTS
---------------
`redirect_uri` is the one part of a sign-in that is not discovered at runtime.
The provider keeps a list; the application sends one value; Authentik compares
them byte for byte and, when they differ, shows *Redirect URI Error* and never
sends the browser back. Both halves look healthy from the outside — the app
answers 200, the provider answers 200 — so the only way to find it is to compare
the two lists.

It has broken twice in this estate for the same reason: a hostname moved (a
product renamed, a name retired, an alias added) and the provider's list was not
updated with it. The page that results says "missing, invalid, or mismatching
redirection URI (redirect_uri)" and nothing else.

WHAT IT CHECKS
--------------
1. **Every registered URI is still reachable.** Its hostname must resolve and be
   served by an NPM proxy host, and Authentik must accept the URI itself (an
   authorize request must not come back with the Redirect URI Error page). A
   registration for a name nothing serves is dead weight that hides the live
   one; a registration Authentik rejects is a bug on this side.
2. **Every app's real request is registered.** For each NPM hostname the check
   probes the estate's OIDC entry points (`/api/auth/login`,
   `/api/v1/auth/oidc/login`, `/oauth2/start`, `/auth/login`) with redirects
   off. When an app hands off to Authentik it names a `client_id` and a
   `redirect_uri`; that exact pair must be in the provider's list. This is the
   direction that catches a renamed host: the app starts sending a URI nobody
   registered, and only this comparison notices.

READ-ONLY
---------
Nothing is written: no provider is patched, no host is changed. The fix for a
finding is a human decision — add the URI, retire the app, or point the app at
the registered name.

USAGE
-----
It runs from `estate-checks.service` on the edge, where `docker` is the CLI *inside*
the `proxy` container rather than on the host, so the prefix is an argument:

    ./scripts/check-sso-redirects.py --docker 'incus exec proxy -- docker' \
        --prom /var/lib/node_exporter/textfile/estate-check-sso-redirects.prom

and by hand anywhere the two APIs are reachable:

    ./scripts/check-sso-redirects.py
    ./scripts/check-sso-redirects.py --json report.json --no-probe-apps

Two facts were learned by running it on 2026-11-16 and are now rules here:

* **An unreachable authorize endpoint is a warning, not a registration bug.** Two
  registered callbacks (`bazarr`/`prowlarr` on `monarch`) came back as `Connection
  reset by peer` and `SSL: UNEXPECTED_EOF_WHILE_READING` in the middle of a 300-name
  sweep, and both authorize 200 when asked directly a minute later. The check retries
  once and then says `UNREACHABLE`, which reads as "ask again", not as "fix the
  provider" — the difference decides whether somebody patches a correct registration.
* **The provider is read at the container address, not the public name.** Under load
  the public name answers 504 through the edge and the audit then reports every
  registration as unreadable; `--auth` names the address that bypasses the proxy.

Exit status: 0 when every check passes, 1 when anything fails, 2 when the audit could
not run at all (no provider token, no NPM rows) — never a pass.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, write_prom  # noqa: E402

CHECK = "sso_redirects"

AUTH = "https://auth.cerulean.innotel.us"
API = AUTH + "/api/v3"
NPM_DB = "cerulean-npm-db"
NPM_CONTAINER = "cerulean-npm"

#: How to reach the Docker CLI, when it has to be said. Empty means "work it out":
#: this runs both inside the `proxy` container (where `docker` is the CLI) and on the
#: edge host beside it (where it is `incus exec proxy -- docker`). A unit file was tried
#: first and systemd split the quoted prefix into six arguments, so the audit exited on
#: argparse instead of running — the quoting, not the check, decided whether it ran.
DOCKER: list[str] = []


def docker_prefixes() -> list[list[str]]:
    """Every way the Docker CLI might be reached here, best first.

    **incus before a local `docker` on purpose.** `i4` carries a `docker` binary that
    cannot see the `proxy` container's containers, so asking it first answered "no such
    container" for the identity provider and the audit exited saying it could not run.
    The edge's runtime is incus; a local CLI is only the fallback.
    """
    if DOCKER:
        return [DOCKER]
    prefixes: list[list[str]] = []
    if shutil.which("incus"):
        prefixes.append(["incus", "exec", "proxy", "--", "docker"])
    if shutil.which("docker"):
        prefixes.append(["docker"])
    return prefixes or [["docker"]]


def docker_prefix() -> list[str]:
    return docker_prefixes()[0]


def container_address(name: str) -> str | None:
    """The container's own address, asked of whichever Docker can actually see it."""
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"
    for prefix in docker_prefixes():
        out = subprocess.run([*prefix, "inspect", "-f", fmt, name],
                             capture_output=True, text=True)
        addresses = out.stdout.split()
        if addresses:
            return addresses[0]
    return None


class CouldNotRun(RuntimeError):
    """The audit could not be made: exit 2, never a silent pass."""

# The paths an estate app uses to start an OIDC sign-in. Probed in order, first
# hand-off wins. Every one of these is in service somewhere in the estate.
SSO_ENTRY_PATHS = (
    "/api/auth/login",
    "/api/v1/auth/oidc/login",
    "/oauth2/start",
    "/auth/login",
    "/api/sso/login",
)

USER_AGENT = "Mozilla/5.0 sso-redirect-audit"


def docker(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([*docker_prefix(), *args], input=stdin, capture_output=True, text=True)


def authentik_token() -> str:
    out = docker("exec", "cerulean", "printenv", "AUTHENTIK_BOOTSTRAP_TOKEN")
    token = out.stdout.strip()
    if not token:
        raise CouldNotRun("no AUTHENTIK_BOOTSTRAP_TOKEN in the cerulean container")
    return token


def api(token: str, path: str) -> dict:
    request = urllib.request.Request(API + path)
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def npm_hosts() -> dict[str, dict]:
    """domain -> {id, forward_host, forward_port} straight from NPM's DB.

    The API needs credentials this check should not depend on; the DB row is the
    same fact and is what the edge actually generates its config from.
    """
    sql = ("select id, domain_names, forward_scheme, forward_host, forward_port "
           "from proxy_host where enabled=1")
    result = docker("exec", "-i", NPM_DB, "sh", "-c",
                    'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" npm -N -B', stdin=sql)
    rows: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pid, names, scheme, host, port = parts[:5]
        try:
            domains = json.loads(names)
        except json.JSONDecodeError:
            continue
        for domain in domains:
            rows[domain.lower()] = {
                "id": int(pid), "scheme": scheme, "host": host, "port": port,
            }
    if not rows:
        detail = result.stderr.strip()[:200]
        raise CouldNotRun(f"could not read NPM proxy hosts from {NPM_DB}: {detail}")
    return rows


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def fetch(url: str, follow: bool = False, timeout: int = 15) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
            return None

    handlers = [] if follow else [NoRedirect]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, {}, str(error).encode()


def authorize_verdict(client_id: str, redirect_uri: str, issuer: str | None = None,
                      timeout: int = 15) -> tuple[str, str]:
    """Ask Authentik to accept a redirect_uri. Returns (verdict, detail).

    `UNREACHABLE` after one retry is deliberately *not* `FAIL`: a TLS reset on a busy
    edge says nothing about whether the URI is registered, and treating it as a
    registration bug invites a patch to a correct provider.
    """
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": "audit",
    })
    base = (issuer or AUTH).rstrip("/")
    text = ""
    for attempt in (1, 2):
        status, _headers, body = fetch(f"{base}/application/o/authorize/?{query}", timeout=timeout)
        text = body.decode(errors="replace")
        if status:
            break
    if status == 0:
        return "UNREACHABLE", f"authorize unreachable twice ({text[:60]}) — ask again, not a registration fault"
    if status == 400 or "Redirect URI Error" in text:
        return "FAIL", "Authentik rejects this redirect_uri (Redirect URI Error)"
    return "PASS", f"authorize answered {status}"


def discover_app(domain: str, timeout: int = 10) -> dict | None:
    """What this hostname actually asks Authentik for, if it starts an OIDC flow."""
    for path in SSO_ENTRY_PATHS:
        status, headers, _ = fetch(f"https://{domain}{path}", timeout=timeout)
        location = headers.get("Location", "")
        if status not in (301, 302, 303, 307, 308) or "client_id=" not in location:
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        client = (query.get("client_id") or [""])[0]
        redirect = (query.get("redirect_uri") or [""])[0]
        if client and redirect:
            return {"path": path, "client_id": client, "redirect_uri": redirect,
                    "issuer": "{}://{}".format(*urllib.parse.urlparse(location)[:2])}
    return None


def main() -> int:
    global AUTH, API, DOCKER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="", help="write the findings to this file")
    parser.add_argument("--no-probe-apps", action="store_true",
                        help="only check registered URIs, do not probe every hostname")
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--docker", default="", metavar="CMD",
                        help="how to reach the Docker CLI, e.g. 'incus exec proxy -- docker' "
                             "(default: detected — `docker` if present, else through `incus "
                             "exec proxy`)")
    parser.add_argument("--auth", default=AUTH, metavar="URL",
                        help=f"provider base URL (default: {AUTH}); use the container "
                             "address so the edge proxy is not in the path")
    parser.add_argument("--auth-from", default="", metavar="CONTAINER",
                        help="read the provider at this container's own address "
                             "(http://<ip>:9000) instead of through the edge, which answers "
                             "504 under load and makes every registration look unreadable")
    parser.add_argument("--timeout", type=int, default=15, help="seconds per request")
    parser.add_argument("--deadline", type=int, default=0,
                        help="seconds for the hostname sweep; unprobed names are reported "
                             "as not probed rather than as passing (0 = no limit)")
    args = parser.parse_args()

    DOCKER = shlex.split(args.docker) if args.docker else []
    AUTH = args.auth.rstrip("/")
    API = AUTH + "/api/v3"
    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran_ok=ran)
        except OSError as error:
            print(f"check-sso-redirects: could not write {args.prom}: {error}", file=sys.stderr)

    if args.auth_from:
        address = container_address(args.auth_from)
        if address is None:
            print(f"check-sso-redirects: no container named {args.auth_from} is visible to "
                  "any Docker CLI here — the audit cannot be made", file=sys.stderr)
            emit(None, ran=False)
            return 2
        AUTH = f"http://{address}:9000"
        API = AUTH + "/api/v3"

    try:
        token = authentik_token()
        providers = api(token, "/providers/oauth2/?page_size=200")["results"]
        hosts = npm_hosts()
    except (CouldNotRun, urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"check-sso-redirects: {error} — the audit cannot be made", file=sys.stderr)
        emit(None, ran=False)
        return 2
    print(f"providers: {len(providers)}   NPM hostnames: {len(hosts)}")

    findings: list[dict] = []

    def record(verdict: str, subject: str, detail: str) -> None:
        findings.append({"verdict": verdict, "subject": subject, "detail": detail})
        print(f"  {verdict:<4} {subject}\n         {detail}")

    registered: dict[str, set[str]] = {}
    print("\n== every registered redirect URI")
    for provider in providers:
        uris = provider.get("redirect_uris") or []
        if not uris:
            continue
        client = provider.get("client_id") or provider.get("name")
        registered.setdefault(client, set())
        print(f"-- {provider.get('name')} ({client}), {len(uris)} URI(s)")
        for entry in uris:
            uri = entry.get("url", "")
            registered[client].add(uri)
            host = urllib.parse.urlparse(uri).hostname or ""
            if entry.get("redirect_uri_type") != "authorization":
                record("WARN", uri, f"registered for {entry.get('redirect_uri_type')}, not an authorization redirect")
                continue
            if not host:
                record("FAIL", uri, "not a usable URI")
                continue
            if host not in hosts:
                record("FAIL", uri, f"{host} has no enabled NPM proxy host — the registration outlived the name")
                continue
            if not resolves(host):
                record("FAIL", uri, f"{host} does not resolve")
                continue
            verdict, detail = authorize_verdict(client, uri, timeout=args.timeout)
            record(verdict, uri, f"{detail}; served by NPM row {hosts[host]['id']}")

    if args.no_probe_apps:
        pass
    else:
        print("\n== what each hostname actually asks for")
        domains = sorted(hosts)
        discovered: dict[str, dict | None] = {}
        unprobed: list[str] = []
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = {pool.submit(discover_app, domain, args.timeout): domain for domain in domains}
            done, pending = wait(futures, timeout=args.deadline or None)
            for future in done:
                try:
                    discovered[futures[future]] = future.result()
                except Exception:  # noqa: BLE001 — a probe that raised is simply unprobed
                    unprobed.append(futures[future])
            unprobed.extend(sorted(futures[future] for future in pending))
            pool.shutdown(wait=False, cancel_futures=True)
        if unprobed:
            record("NOT-PROBED", f"{len(unprobed)} hostname(s)",
                   f"the sweep hit its {args.deadline}s deadline before these were asked: "
                   + ", ".join(unprobed[:8]) + (" …" if len(unprobed) > 8 else ""))
        found = 0
        for domain, app in sorted(discovered.items()):
            if not app:
                continue
            found += 1
            client, redirect = app["client_id"], app["redirect_uri"]
            known = registered.get(client)
            if known is None:
                record("FAIL", f"{domain} -> {client}",
                       f"unknown client_id; it asks for {redirect}")
            elif redirect in known:
                record("PASS", f"{domain} -> {client}", f"{app['path']} names {redirect}")
            else:
                record("FAIL", f"{domain} -> {client}",
                       f"{app['path']} sends redirect_uri {redirect}, which is NOT registered")
        print(f"  (probed {len(domains)} hostnames, {found} start an OIDC sign-in)")

    # A verdict becomes a finding level here, once: PASS is not a finding, FAIL is the
    # only level that fails, and UNREACHABLE/NOT-PROBED are things to ask again about.
    levels = {"FAIL": "fail", "WARN": "warn", "UNREACHABLE": "warn", "NOT-PROBED": "note"}
    result = Audit()
    for finding in findings:
        level = levels.get(finding["verdict"])
        if level:
            result.findings.append(Finding(finding["verdict"].lower().replace("-", "_"),
                                            level, f"{finding['subject']}: {finding['detail']}"))
    emit(result, ran=True)

    failures = result.failures
    warnings = result.warnings
    print(f"\n{sum(1 for f in findings if f['verdict'] == 'PASS')} pass, "
          f"{len(warnings)} warn, {len(failures)} fail")
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(findings, handle, indent=2)
        print(f"findings written to {args.json}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
