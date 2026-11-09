#!/usr/bin/env python3
"""Prove mail.innotel.us can still sign a person in, end to end.

WHY THIS EXISTS
---------------
The webmail's SSO login completed — Authentik authenticated the person and the webmail
exchanged the code for a token — and then every request 500d, so the page said
"Authentication Failed" after a *correct* sign-in. The cause was Stalwart, not the
webmail: its only directory is the Authentik OIDC one, it initialises that directory once,
and a failed discovery fetch is cached for the life of the process. From then on
`GET /jmap/session` answers **500 for every bearer token** while anonymous requests still
answer 200 — a signature nothing in the estate watched, and one that recurs on its own
(recorded 2026-11-02 and again 2026-11-06).

So this check drives the three legs a person actually needs, each observable from outside:

  1. the webmail can start a sign-in (`POST /api/auth/sso/start` -> 200 with an authorize
     URL naming this deployment's own callback);
  2. Authentik accepts that exact callback (302 into the login flow, not the
     "Redirect URI Error" page) — the failure that hides when a hostname is renamed;
  3. the mail server's authentication backend is initialised: an impossible bearer token
     must be **refused with 401**, while 500 means the directory failed to initialise and
     cached it.

THE RULES
---------
1. A leg that answers wrongly is a **failure**, naming which and what it means.
2. An unreachable webmail or mail server is **exit 2**: a check that cannot read its target
   is never a quiet pass.
3. The finding for a cached directory start names the recovery, because the fix is a
   restart — the `stalwart-directory-watchdog.timer` on the mail container does it within
   five minutes — and not a configuration change.

READ-ONLY
---------
No cookie is kept and nothing is written: `sso/start` mints a pending cookie the check
discards, and the bearer probe is a value no issued token can equal.

Usage:
    ./scripts/check-mail-sso.py
    ./scripts/check-mail-sso.py --webmail https://mail.innotel.us \\
        --base http://192.168.1.15:8080 --prom /path/to/textfile

`systemd/estate-checks.service` passes `--base` at the mail container's own address
(`192.168.1.15`, the address table's `mail`), the address every other estate client dials
and the one that travels with the container.

Exit codes: 0 = a person can sign in, 1 = a leg is broken, 2 = a target could not be
reached.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, write_prom  # noqa: E402

CHECK = "mail_sso"

#: The public webmail, the identity provider, and the mail server as the estate reaches
#: it (its own address, not the host it happens to run on).
WEBMAIL = "https://mail.innotel.us"
IDP = "https://auth.cerulean.innotel.us"
STALWART = os.environ.get("STALWART_URL", "http://192.168.1.15:8080")

#: A bearer token Stalwart can never have issued. A healthy server rejects it (401);
#: a server whose directory failed to initialise answers 500 for *any* bearer.
IMPOSSIBLE_BEARER = "estate-check-mail-sso-probe"

USER_AGENT = "innotel-estate-check/mail-sso"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Read the first answer, because the redirect *is* the fact being checked."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def fetch(url: str, method: str = "GET", body=None, headers: dict | None = None,
          timeout: int = 20) -> tuple[int, dict[str, str], str]:
    """(status, headers, body); status 0 means the request never got an answer."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(
        NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _lower(response.headers), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, _lower(error.headers), (error.read() or b"").decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, {}, str(error)


def _lower(headers) -> dict[str, str]:
    return {str(key).lower(): value for key, value in (headers or {}).items()}


def observe(webmail: str, idp: str, base: str, locale: str) -> dict:
    """Every fact the rules need, gathered without deciding anything."""
    callback = f"{webmail.rstrip('/')}/{locale}/auth/callback"
    origin = urllib.parse.urlparse(webmail.rstrip("/")).scheme + "://" + urllib.parse.urlparse(webmail).netloc

    facts: dict = {"callback": callback}

    status, headers, body = fetch(
        f"{webmail.rstrip('/')}/api/auth/sso/start",
        "POST",
        {"redirect_uri": callback, "locale": locale},
        {"Origin": origin, "Referer": f"{webmail.rstrip('/')}/{locale}/login",
         "User-Agent": USER_AGENT},
        timeout=25,
    )
    facts["sso_start"] = {"status": status, "body": body[:300]}
    authorize_url = ""
    if status == 200:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        authorize_url = str(payload.get("authorize_url") or "")
        facts["sso_start"]["authorize_url"] = authorize_url

    if authorize_url:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query)
        facts["authorize_request"] = {
            "client_id": (query.get("client_id") or [""])[0],
            "redirect_uri": (query.get("redirect_uri") or [""])[0],
        }
        status, headers, body = fetch(authorize_url, headers={"User-Agent": USER_AGENT}, timeout=25)
        facts["authorize"] = {
            "status": status,
            "location": headers.get("location", ""),
            "body": body[:400],
        }

    status, _headers, _body = fetch(
        f"{idp.rstrip('/')}/application/o/stalwart-webui/.well-known/openid-configuration",
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    facts["idp_discovery"] = {"status": status}

    base = base.rstrip("/")
    status, _headers, body = fetch(f"{base}/jmap/session", headers={"User-Agent": USER_AGENT}, timeout=20)
    facts["jmap_anonymous"] = {"status": status, "body": body[:200]}

    status, _headers, body = fetch(
        f"{base}/jmap/session",
        headers={"Authorization": "Bearer " + IMPOSSIBLE_BEARER, "User-Agent": USER_AGENT},
        timeout=20,
    )
    facts["jmap_bearer"] = {"status": status, "body": body[:300]}

    return facts


def audit(facts: dict) -> Audit:
    """The three legs, over observations the caller already has. Pure, so the rules are
    the thing under test."""
    out = Audit()
    callback = facts.get("callback", "")

    start = facts.get("sso_start") or {}
    if start.get("status") != 200:
        detail = (start.get("body") or "").replace("\n", " ")[:160]
        out.findings.append(Finding(
            "sso_start_failed",
            "fail",
            f"the webmail's /api/auth/sso/start answered HTTP {start.get('status')} — nobody "
            f"can begin a sign-in at {facts.get('webmail', 'the webmail')} ({detail})",
        ))
    else:
        request = facts.get("authorize_request") or {}
        if not request.get("client_id"):
            out.findings.append(Finding(
                "sso_start_incomplete", "fail",
                "the authorize URL names no client_id, so Authentik cannot identify the app",
            ))
        if request.get("redirect_uri") and request.get("redirect_uri") != callback:
            out.findings.append(Finding(
                "callback_mismatch", "fail",
                f"the webmail asks Authentik to return to {request.get('redirect_uri')}, not "
                f"its own callback {callback} — a renamed host leaves the app sending a URI the "
                f"provider has never heard of",
            ))

    authorize = facts.get("authorize")
    if authorize is not None:
        status = authorize.get("status")
        location = authorize.get("location", "")
        body = authorize.get("body", "")
        if status == 0:
            out.findings.append(Finding(
                "idp_unreachable", "fail",
                "Authentik's authorize endpoint did not answer — no SSO sign-in can complete",
            ))
        elif status in (301, 302, 303, 307, 308) and "/if/flow/" in location:
            pass  # accepted; the browser would be at the login stage
        elif status == 400 or "Redirect URI Error" in body:
            out.findings.append(Finding(
                "authorize_rejected", "fail",
                "Authentik rejects the callback the webmail sends (Redirect URI Error) — the "
                "provider's redirect list and the app's request disagree",
            ))
        else:
            out.findings.append(Finding(
                "authorize_unexpected", "fail",
                f"authorize answered HTTP {status} instead of sending the person to the login "
                f"flow ({body[:120]})",
            ))

    discovery = facts.get("idp_discovery") or {}
    if discovery.get("status") != 200:
        out.findings.append(Finding(
            "idp_discovery_failed", "fail",
            f"the provider's discovery document answered HTTP {discovery.get('status')} — "
            f"Stalwart's directory and the webmail both read it to validate tokens",
        ))

    anonymous = facts.get("jmap_anonymous") or {}
    if anonymous.get("status") != 200:
        out.findings.append(Finding(
            "stalwart_unhealthy", "fail",
            f"the mail server's JMAP session answered HTTP {anonymous.get('status')} — it is not "
            f"serving ({(anonymous.get('body') or '')[:120]})",
        ))

    bearer = facts.get("jmap_bearer") or {}
    status = bearer.get("status")
    if status == 401:
        pass  # the backend is initialised and refused a token it never issued
    elif status == 500:
        out.findings.append(Finding(
            "directory_unavailable", "fail",
            "Stalwart's Authentication directory failed to initialise and the failure is cached "
            "for the process: every bearer token answers 500, so a correct sign-in ends in "
            "'Authentication Failed'. Restart stalwart — stalwart-directory-watchdog.timer on "
            "the mail container does it within five minutes",
        ))
    elif status == 0:
        out.findings.append(Finding(
            "stalwart_unreachable", "fail",
            "the mail server did not answer its JMAP session probe",
        ))
    else:
        out.findings.append(Finding(
            "auth_backend_unexpected", "fail",
            f"an impossible bearer token was answered HTTP {status} — expected 401 (refused); "
            f"anything else means the authentication backend is not deciding",
        ))

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--webmail", default=WEBMAIL, help=f"public webmail origin (default: {WEBMAIL})")
    parser.add_argument("--idp", default=IDP, help=f"Authentik base URL (default: {IDP})")
    parser.add_argument("--base", default=STALWART, help=f"Stalwart base URL (default: {STALWART})")
    parser.add_argument("--locale", default="en", help="locale segment of the callback (default: en)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    facts = observe(args.webmail, args.idp, args.base, args.locale)
    facts["webmail"] = args.webmail
    result = audit(facts)

    unreachable = [
        f"{name} ({url})"
        for name, url, status in (
            ("webmail", args.webmail, (facts.get("sso_start") or {}).get("status")),
            ("mail server", args.base, (facts.get("jmap_anonymous") or {}).get("status")),
        )
        if status == 0
    ]

    if args.prom:
        write_prom(args.prom, CHECK, result, ran_ok=not unreachable)

    print(f"check-mail-sso: {args.webmail} -> {args.idp} -> {args.base}")
    if unreachable:
        print(f"could not reach: {', '.join(unreachable)}", file=sys.stderr)
        return 2
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.code}: {finding.message}")
    print(f"check-mail-sso: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
