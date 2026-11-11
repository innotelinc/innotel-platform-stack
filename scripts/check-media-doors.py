#!/usr/bin/env python3
"""Do Gizmo's two public doors still open the way they are supposed to?

WHY THIS EXISTS
---------------
The media estate collapsed eight hostnames onto one product (docs in the `monarch` repo,
`docs/unified.md`). Two doors are all that is left, and both fail quietly in a way nobody
notices until somebody wants to watch something:

  * `media.innotel.us` is the Jellyfin front page, and since 2026-09-18 it is
    **deliberately not OIDC-gated**: its gateway carries `OAUTH2_PROXY_SKIP_AUTH_ROUTES:
    "^/.*$"` and Jellyfin serves its own sign-in page (a Cerulean Authentik button plus an
    LDAP binding for native clients). Gating the page was tried and reverted, because a TV
    client that opens `…/web/#/login` in a webview was sent to Authentik — a flow it cannot
    complete — and Quick Connect had no page to enter its code on. So the working answer
    here is Jellyfin's own page, reached through one app-relative hop (`/` answers `302
    web/`). A **redirect to the IdP** is now the regression: it is the gate having been put
    back in front of a product whose clients cannot complete it. A 5xx is the gateway or
    the app being down behind a name that still resolves.
  * `iptv.innotel.us` is the watch door, and it is deliberately **not** a redirect: Jellyfin
    keeps its own accounts because TV and native clients cannot complete a browser OIDC
    flow. Nothing in the estate checks it, so a stopped Jellyfin, a bad proxy pass or a
    lost branding upload all read as "the site is up" from every other check.
    (It was `tv.innotel.us` until 2026-11-11, when the owner pointed that name at Sonarr.
    The assertion is about Jellyfin's direct publish on 14016, so it follows the name that
    still forwards there; `media.innotel.us` serves the same app through its gateway.)

WHAT IT ASSERTS
---------------
  1. FRONT PAGE — `media.innotel.us/` answers 200 naming Jellyfin, or one app-relative
     hop to that (`302 web/`). A redirect to `auth.cerulean.innotel.us` is a failure: the
     OIDC gate is not allowed in front of this name. 5xx is a failure, no answer is a
     failure, and a 200 that is not Jellyfin is a failure.
  2. WATCH — `iptv.innotel.us/web/` answers 200 and its body is Jellyfin's web client (the
     page names jellyfin), not a proxy error page that happens to be 200.
  3. BRANDING — `iptv.innotel.us/Branding/Splashscreen` answers 200 `image/png` of at least
     `--splash-min-bytes` (default 50 000). The Gizmo splash is an upload into Jellyfin's
     config, so it is the first thing a Jellyfin upgrade or a reinstall drops; it is a
     note rather than a failure, because the library still plays without it.

Run it from a host that can reach the names the way a person does (the edge, or any estate
host with DNS):

    ./scripts/check-media-doors.py
    ./scripts/check-media-doors.py --prom ${TEXTFILE_DIR}/estate-check-media-doors.prom
    ./scripts/check-media-doors.py --json

Exit codes: 0 = both doors behaved, 1 = one did not, 2 = nothing could be probed at all
(the names did not resolve from here, which is not an answer about the doors).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from estate_check import Audit, Finding, write_prom  # noqa: E402

MEDIA_DOOR = "https://media.innotel.us/"
WATCH_DOOR = "https://iptv.innotel.us"
AUTH_HOST = "auth.cerulean.innotel.us"
SPLASH_PATH = "/Branding/Splashscreen"
WEB_PATH = "/web/"
DEFAULT_SPLASH_MIN = 50_000
USER_AGENT = "innotel-estate-check/1.0 (+media doors)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a 3xx, so the redirect itself is what comes back.

    `urlopen` follows redirects by default, which turned the gate's 302 into the identity
    provider's 200 and made a working gate read as an open door.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, timeout: float = 20.0) -> tuple[int | None, dict[str, str], bytes]:
    """GET one URL without following redirects.

    A redirect is the answer the gate check is about, so following it would hide it. Any
    transport failure is `(None, {}, b"")` — the caller decides whether that is a finding.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(64_000)
    except urllib.error.HTTPError as error:  # a status is an answer, not a failure to ask
        headers = dict(error.headers or {})
        return error.code, headers, error.read(4_096) if error.fp else b""
    except Exception:  # noqa: BLE001 — DNS, TLS, timeout: all "no answer" here
        return None, {}, b""


def _names_jellyfin(body: bytes) -> bool:
    """Whether a 200 body is Jellyfin's own page rather than a proxy error page."""
    return "jellyfin" in body[:64_000].decode("utf-8", "replace").lower()


def audit(media: str, watch: str, *, probe=fetch, splash_min: int = DEFAULT_SPLASH_MIN) -> Audit:
    """The three assertions, as findings. Pure enough to test with a fake `probe`."""
    result = Audit()

    status, headers, body = probe(media)
    location = headers.get("Location", "") or headers.get("location", "")
    if status is None:
        result.findings.append(Finding(
            "front-unreachable", "fail", f"{media} did not answer — DNS, TLS or the edge is down"))
    elif 300 <= status < 400 and AUTH_HOST in location:
        result.findings.append(Finding(
            "gate-restored", "fail",
            f"{media} redirects to {AUTH_HOST}: the OIDC gate is back in front of the "
            "front page, and a TV client in a webview cannot complete that flow (Jellyfin "
            "serves its own sign-in; see monarch docs/operations.md §Jellyfin and Seerr)"))
    elif 300 <= status < 400 and location and not location.startswith("http"):
        # Jellyfin's own front page answers `/` with `302 web/` — the app's page, one
        # relative hop away. Follow exactly that hop and judge the page it lands on.
        target = urljoin(media, location)
        landed, _, landed_body = probe(target)
        if landed is None:
            result.findings.append(Finding(
                "front-unreachable", "fail", f"{media} redirected to {target}, which did not answer"))
        elif landed != 200:
            result.findings.append(Finding(
                "front-status", "fail",
                f"{media} redirected to {target}, which answered {landed} — the app is down behind the name"))
        elif not _names_jellyfin(landed_body):
            result.findings.append(Finding(
                "front-not-jellyfin", "fail",
                f"{media} redirected to {target}, which answered 200 but does not name "
                "Jellyfin — something else is answering on the front door"))
    elif status == 200 and not _names_jellyfin(body):
        result.findings.append(Finding(
            "front-not-jellyfin", "fail",
            f"{media} answered 200 but the page does not name Jellyfin — something else is "
            "answering on the front door"))
    elif 300 <= status < 400:
        result.findings.append(Finding(
            "front-wrong-target", "fail",
            f"{media} redirects off the app to {location or 'nowhere'}"))
    elif status != 200:
        result.findings.append(Finding(
            "front-status", "fail",
            f"{media} answered {status} — the gateway or the app is down behind the name"))

    status, _, body = probe(watch + WEB_PATH)
    text = body[:64_000].decode("utf-8", "replace").lower()
    if status is None:
        result.findings.append(Finding(
            "watch-unreachable", "fail", f"{watch}{WEB_PATH} did not answer — Jellyfin or the edge is down"))
    elif status != 200:
        result.findings.append(Finding(
            "watch-status", "fail", f"{watch}{WEB_PATH} answered {status} instead of 200"))
    elif "jellyfin" not in text:
        result.findings.append(Finding(
            "watch-not-jellyfin", "fail",
            f"{watch}{WEB_PATH} answered 200 but the page does not name Jellyfin — "
            "something else is answering on the watch door"))

    status, headers, body = probe(watch + SPLASH_PATH)
    kind = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if status is None:
        result.findings.append(Finding(
            "branding-unreachable", "warn", f"{watch}{SPLASH_PATH} did not answer"))
    elif status != 200:
        result.findings.append(Finding(
            "branding-missing", "warn",
            f"{watch}{SPLASH_PATH} answered {status} — the Gizmo splash is not installed"))
    elif "image/png" not in kind:
        result.findings.append(Finding(
            "branding-type", "warn", f"{watch}{SPLASH_PATH} answered {kind or 'no content-type'}"))
    elif len(body) < splash_min:
        result.findings.append(Finding(
            "branding-small", "warn",
            f"{watch}{SPLASH_PATH} answered {len(body)} bytes, under {splash_min} — "
            "a placeholder rather than the Gizmo splash"))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--media", default=MEDIA_DOOR, help="the gated front page")
    parser.add_argument("--watch", default=WATCH_DOOR, help="the Jellyfin watch door")
    parser.add_argument("--splash-min-bytes", type=int, default=DEFAULT_SPLASH_MIN)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, "media-doors", result, ran_ok=ran)
        except OSError as error:
            print(f"check-media-doors: could not write {args.prom}: {error}", file=sys.stderr)

    probe = lambda url: fetch(url, timeout=args.timeout)  # noqa: E731
    result = audit(args.media, args.watch, probe=probe, splash_min=args.splash_min_bytes)

    unreachable = all(f.code.endswith("unreachable") and f.level == "fail"
                      for f in result.failures) and len(result.failures) >= 2
    if unreachable:
        emit(None, ran=False)
        print("check-media-doors: neither door answered — this host cannot see the names",
              file=sys.stderr)
        return 2

    emit(result, ran=True)
    if args.json:
        print(json.dumps({
            "findings": [{"code": f.code, "level": f.level, "message": f.message}
                         for f in result.findings],
            "failures": len(result.failures), "warnings": len(result.warnings),
        }, indent=2, sort_keys=True))
        return 0 if result.ok else 1

    print(f"  {args.media:<40} Jellyfin front page (not OIDC-gated by design)")
    print(f"  {args.watch}{WEB_PATH:<27} watch door")
    print(f"  {args.watch}{SPLASH_PATH:<20} Gizmo splash")
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(f"check-media-doors: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
