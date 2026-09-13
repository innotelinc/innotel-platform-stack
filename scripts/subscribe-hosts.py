#!/usr/bin/env python3
"""subscribe-hosts.py — own the NPM side of the shared subscribe portal.

Every Innotel service gets its own subscription landing page, served by one
small nginx (web/subscribe/) that picks the page by Host header:

    subscribe.<service>.innotel.us  ->  pages/<service>.html
    subscribe.innotel.us (apex)     ->  pages/index.html  (the directory)

The pages are generated (scripts/sync-subscribe-pages.py); this script keeps the
*edge NPM* side of the same contract honest so those pages are reachable and
stay reachable:

  * every subscribe host forwards to this portal's host:port;
  * TLS is on and each host reuses whichever certificate already covers the
    name (per-service wildcards like *.monarch.innotel.us where they exist),
    otherwise one is requested via HTTP-01. HTTP-01 is deliberate: the RFC2136
    DNS-01 path times out from the edge NPM (the dynamic update never lands),
    while every subscribe host already resolves at the edge;
  * no Authentik forward-auth and no websocket upgrade — prices and checkout are
    public by design, so a subscribe page must never sit behind the SSO gate.
    A gate found here is reported as drift and removed on the next run.

subscribe.zeus.innotel.us is deliberately NOT managed here: Zeus serves its own
subscription page from its own app, and zeus-pbx-platform already maps that host
to :3001. Its page in web/subscribe/pages/zeus.html stays as the portal-side
fallback.

Usage
    python3 scripts/subscribe-hosts.py --check    # read-only drift check
    python3 scripts/subscribe-hosts.py            # create/update

Environment (real env wins; defaults suit the .46 portal host)
    NPM_API_URL             edge NPM                (http://192.168.1.71:81)
    NPM_ADMIN_EMAIL         edge NPM login email
    NPM_ADMIN_PASSWORD      edge NPM login password
    NPM_API_TOKEN           persistent NPM token    (skips the login)
    SUBSCRIBE_PORTAL_HOST   host running the portal (192.168.1.46)
    SUBSCRIBE_PORTAL_PORT   portal host port        (3040)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "web" / "subscribe" / "pages"

DEFAULT_API_URL = "http://192.168.1.71:81"
DEFAULT_PORTAL_HOST = "192.168.1.46"
DEFAULT_PORTAL_PORT = 3040

# The apex directory plus every service page, minus the services that serve
# their own subscription page elsewhere.
NOT_MANAGED = {
    "zeus": "zeus-pbx-platform maps subscribe.zeus.innotel.us to its own app (:3001)",
}

# What this script owns on an existing host. Everything else a host carries
# (HSTS, caching, body-size limits, …) is left exactly as found — turning a
# security header off silently would be worse than the drift it "fixes".
MANAGED_KEYS = (
    "domain_names", "forward_scheme", "forward_host", "forward_port",
    "certificate_id", "ssl_forced", "enabled", "allow_websocket_upgrade",
    "access_list_id", "meta",
)

# An advanced_config containing any of these is an Authentik forward-auth gate.
# Subscribe pages are public by design, so that IS drift.
GATE_MARKERS = ("auth_request", "authentik", "forward_auth")

# Fields NPM's PUT /api/nginx/proxy-hosts accepts — the GET response carries
# read-only extras (id, created_on, owner_user_id) that v2.x rejects.
PUT_KEYS = (
    "domain_names", "forward_scheme", "forward_host", "forward_port",
    "certificate_id", "ssl_forced", "hide_headers", "http2_support",
    "hsts_enabled", "hsts_subdomains", "block_exploits", "caching_enabled",
    "allow_websocket_upgrade", "access_list_id", "advanced_config",
    "locations", "meta", "enabled",
)


class NpmError(RuntimeError):
    pass


class Npm:
    def __init__(self, api_url: str, token: str | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: int = 300):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.api_url + path, data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            raise NpmError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise NpmError(f"{method} {path} -> {exc}") from None

    def login(self, email: str, password: str) -> str:
        status, body = self.request("POST", "/api/tokens",
                                    {"identity": email, "secret": password},
                                    timeout=30)
        if status != 200 or not isinstance(body, dict) or not body.get("token"):
            raise NpmError("edge NPM rejected the admin login")
        self.token = body["token"]
        return self.token

    def proxy_hosts(self) -> list[dict]:
        _, hosts = self.request("GET", "/api/nginx/proxy-hosts", timeout=60)
        return hosts or []

    def certificates(self) -> list[dict]:
        _, certs = self.request("GET", "/api/nginx/certificates", timeout=60)
        return certs or []


def cert_index(certs: list[dict]) -> dict[str, int]:
    """Every name a certificate covers -> certificate id (lowercased)."""
    index: dict[str, int] = {}
    for cert in certs:
        for name in cert.get("domain_names") or []:
            index.setdefault(name.lower(), cert["id"])
    return index


def covering_cert(index: dict[str, int], name: str) -> int | None:
    """The cert id covering `name`, exact or via *.<parent> (one label only).

    A wildcard matches exactly one label, so subscribe.atlas.innotel.us is
    covered by *.atlas.innotel.us but never by *.innotel.us.
    """
    name = name.lower()
    if name in index:
        return index[name]
    labels = name.split(".")
    if len(labels) > 2:
        return index.get("*." + ".".join(labels[1:]))
    return None


def request_cert(npm: Npm, name: str) -> int | None:
    """Issue a Let's Encrypt cert for `name` via HTTP-01 (see module docstring)."""
    _, cert = npm.request("POST", "/api/nginx/certificates", {
        "provider": "letsencrypt",
        "nice_name": name,
        "domain_names": [name],
        "meta": {"dns_challenge": False, "key_type": "ecdsa"},
    })
    if isinstance(cert, dict) and cert.get("id"):
        print(f"  requested a certificate for {name} (id {cert['id']})")
        return cert["id"]
    print(f"  FAIL could not request a certificate for {name}", file=sys.stderr)
    return None


def desired(name: str, portal_host: str, portal_port: int, cert_id: int | None) -> dict:
    """The field values this script owns for one subscribe host."""
    return {
        "domain_names": [name],
        "forward_scheme": "http",
        "forward_host": portal_host,
        "forward_port": portal_port,
        # public by design: no Authentik gate, so no advanced_config snippet.
        "advanced_config": "",
        "allow_websocket_upgrade": False,
        "ssl_forced": True,
        "certificate_id": cert_id,
        "http2_support": True,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "block_exploits": True,
        "caching_enabled": False,
        "access_list_id": "0",
        "locations": [],
        "enabled": True,
        "meta": {"letsencrypt_agree": False, "dns_challenge": False},
    }


def has_gate(existing: dict) -> bool:
    """True when the host carries an Authentik forward-auth snippet."""
    config = (existing.get("advanced_config") or "").lower()
    return any(marker in config for marker in GATE_MARKERS)


def differences(existing: dict, want: dict) -> list[str]:
    """Which managed fields differ between the live host and the desired state."""
    diffs: list[str] = []
    for key in MANAGED_KEYS:
        current, value = existing.get(key), want[key]
        if key == "certificate_id":
            current, value = int(current or 0), int(value or 0)
        elif key == "domain_names":
            current, value = sorted(current or []), sorted(value)
        elif key == "access_list_id":  # NPM returns 0, we send "0"
            current, value = str(current or "0"), str(value or "0")
        elif key == "meta":  # NPM keeps read-only keys in here too
            current = {k: (existing.get("meta") or {}).get(k) for k in want["meta"]}
            value = dict(want["meta"])
        if current != value:
            diffs.append(key)
    if has_gate(existing):  # the page must never sit behind the SSO gate
        diffs.append("advanced_config(gate)")
    return diffs


def update_payload(existing: dict, want: dict) -> dict:
    """A full PUT body: the host as it is, plus the fields we own."""
    payload = {k: existing.get(k) for k in PUT_KEYS if k in existing}
    for key in MANAGED_KEYS:
        payload[key] = want[key]
    meta = dict(existing.get("meta") or {})
    meta.update(want.get("meta") or {})
    payload["meta"] = meta
    payload.setdefault("locations", [])
    if has_gate(existing):  # dropping the gate drops the whole snippet
        payload["advanced_config"] = ""
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="report drift without writing; exit 1 if out of sync")
    parser.add_argument("--api-url", default=None)
    args = parser.parse_args()

    api_url = args.api_url or os.environ.get("NPM_API_URL") or DEFAULT_API_URL
    portal_host = os.environ.get("SUBSCRIBE_PORTAL_HOST") or DEFAULT_PORTAL_HOST
    portal_port = int(os.environ.get("SUBSCRIBE_PORTAL_PORT") or DEFAULT_PORTAL_PORT)
    email = os.environ.get("NPM_ADMIN_EMAIL", "")
    password = os.environ.get("NPM_ADMIN_PASSWORD", "")

    services = sorted(p.stem for p in PAGES.glob("*.html") if p.stem != "index")
    if not services:
        print(f"FAIL no subscribe pages in {PAGES} — run sync-subscribe-pages.py",
              file=sys.stderr)
        return 1
    managed = [s for s in services if s not in NOT_MANAGED]
    names = ["subscribe.innotel.us"] + [f"subscribe.{s}.innotel.us" for s in managed]

    print(f"portal: http://{portal_host}:{portal_port}  ·  edge NPM: {api_url}")
    for service, why in sorted(NOT_MANAGED.items()):
        print(f"SKIP subscribe.{service}.innotel.us — {why}")

    npm = Npm(api_url, os.environ.get("NPM_API_TOKEN") or None)
    try:
        if not npm.token:
            if not (email and password):
                print("FAIL set NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD (or NPM_API_TOKEN)",
                      file=sys.stderr)
                return 1
            npm.login(email, password)
            print("PASS authenticated with Nginx Proxy Manager")
        index = cert_index(npm.certificates())
        live = {d.lower(): h for h in npm.proxy_hosts()
                for d in (h.get("domain_names") or [])}
    except NpmError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    failed: list[str] = []
    for name in names:
        label = "subscribe.innotel.us (apex directory)" if name == "subscribe.innotel.us" \
            else name.replace(".innotel.us", "")
        cert_id = covering_cert(index, name)
        if cert_id is None:
            if args.check:
                print(f"FAIL {label} — no certificate covers {name}")
                failed.append(name)
                continue
            cert_id = request_cert(npm, name)
            if cert_id is None:
                failed.append(name)
                continue
            index[name.lower()] = cert_id

        want = desired(name, portal_host, portal_port, cert_id)
        existing = live.get(name.lower())
        if existing is None:
            if args.check:
                print(f"FAIL {label} — proxy host missing")
                failed.append(name)
                continue
            try:
                npm.request("POST", "/api/nginx/proxy-hosts", want, timeout=60)
                print(f"PASS {label} — created → http://{portal_host}:{portal_port}")
            except NpmError as exc:
                print(f"FAIL {label} — could not create: {exc}", file=sys.stderr)
                failed.append(name)
            continue

        diffs = differences(existing, want)
        if not diffs:
            print(f"PASS {label} — already correct")
            continue
        if args.check:
            print(f"FAIL {label} — drift in {', '.join(diffs)}")
            failed.append(name)
            continue
        payload = update_payload(existing, want)
        try:
            npm.request("PUT", f"/api/nginx/proxy-hosts/{existing['id']}", payload,
                        timeout=60)
            print(f"PASS {label} — updated ({', '.join(diffs)})")
        except NpmError as exc:
            print(f"FAIL {label} — could not update: {exc}", file=sys.stderr)
            failed.append(name)

    if failed:
        print(f"\nFAIL {len(failed)} of {len(names)} subscribe host(s) out of sync",
              file=sys.stderr)
        return 1
    print(f"\nPASS all {len(names)} subscribe host(s) in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
