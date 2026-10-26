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
Run where the Docker CLI reaches the edge and Authentik — the `proxy` container
on i4:

    incus exec proxy -- python3 /root/check-sso-redirects.py
    incus exec proxy -- python3 /root/check-sso-redirects.py --json report.json
    incus exec proxy -- python3 /root/check-sso-redirects.py --no-probe-apps

Exit status: 0 when every check passes, 1 when anything fails.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

AUTH = "https://auth.cerulean.innotel.us"
API = AUTH + "/api/v3"
NPM_DB = "cerulean-npm-db"
NPM_CONTAINER = "cerulean-npm"

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
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True)


def authentik_token() -> str:
    out = docker("exec", "cerulean", "printenv", "AUTHENTIK_BOOTSTRAP_TOKEN")
    token = out.stdout.strip()
    if not token:
        raise SystemExit("no AUTHENTIK_BOOTSTRAP_TOKEN in the cerulean container")
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
        raise SystemExit(f"could not read NPM proxy hosts from {NPM_DB}: {detail}")
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


def authorize_verdict(client_id: str, redirect_uri: str, issuer: str | None = None) -> tuple[str, str]:
    """Ask Authentik to accept a redirect_uri. Returns (verdict, detail)."""
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": "audit",
    })
    base = (issuer or AUTH).rstrip("/")
    status, headers, body = fetch(f"{base}/application/o/authorize/?{query}")
    text = body.decode(errors="replace")
    if status == 0:
        return "FAIL", f"authorize unreachable: {text[:80]}"
    if status == 400 or "Redirect URI Error" in text:
        return "FAIL", "Authentik rejects this redirect_uri (Redirect URI Error)"
    return "PASS", f"authorize answered {status}"


def discover_app(domain: str) -> dict | None:
    """What this hostname actually asks Authentik for, if it starts an OIDC flow."""
    for path in SSO_ENTRY_PATHS:
        status, headers, _ = fetch(f"https://{domain}{path}", timeout=10)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="", help="write the findings to this file")
    parser.add_argument("--no-probe-apps", action="store_true",
                        help="only check registered URIs, do not probe every hostname")
    parser.add_argument("--threads", type=int, default=12)
    args = parser.parse_args()

    token = authentik_token()
    providers = api(token, "/providers/oauth2/?page_size=200")["results"]
    hosts = npm_hosts()
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
            verdict, detail = authorize_verdict(client, uri)
            record(verdict, uri, f"{detail}; served by NPM row {hosts[host]['id']}")

    if args.no_probe_apps:
        pass
    else:
        print("\n== what each hostname actually asks for")
        domains = sorted(hosts)
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            discovered = dict(zip(domains, pool.map(discover_app, domains)))
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

    failures = [f for f in findings if f["verdict"] == "FAIL"]
    warnings = [f for f in findings if f["verdict"] == "WARN"]
    print(f"\n{len(findings) - len(failures) - len(warnings)} pass, "
          f"{len(warnings)} warn, {len(failures)} fail")
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(findings, handle, indent=2)
        print(f"findings written to {args.json}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
