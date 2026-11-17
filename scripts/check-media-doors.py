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
     hop to that (`302 web/`). A redirect to either identity host is a failure: the
     OIDC gate is not allowed in front of this name. 5xx is a failure, no answer is a
     failure, and a 200 that is not Jellyfin is a failure.
  2. WATCH — `iptv.innotel.us/web/` answers 200 and its body is Jellyfin's web client (the
     page names jellyfin), not a proxy error page that happens to be 200.
  3. BRANDING — `iptv.innotel.us/Branding/Splashscreen` answers 200 `image/png` of at least
     `--splash-min-bytes` (default 50 000). The Gizmo splash is an upload into Jellyfin's
     config, so it is the first thing a Jellyfin upgrade or a reinstall drops; it is a
     note rather than a failure, because the library still plays without it.
  4. HOME ROUTE CHUNKS — the front page loads its routes lazily: its index names
     `runtime.bundle.js`, and that runtime maps the `home` chunk to a JavaScript file and
     a stylesheet the browser fetches on the first navigation. Both are fetched here.
     This is the assertion that would have caught 2026-11-16: the index and *every* asset
     in it answered 200 while `/web/home` died with "loading CSS chunk 27962 failed". The
     id is not a filename — `27962` is the runtime's name for `home`, the file is
     `home.193c6fa7a64e52078d35.css`, and searching the image for `27962*` finds nothing.
     So the filenames come from the runtime's map, never from the id.

A gateway status (502/503/504) is reported as *the app being unreachable*, not as a wrong
page: the proxy says it could not reach Jellyfin, which is what the app's start-up window
answers while it initialises. Naming that separately is the difference between a bare 502
that reads like a misrouted vhost and the one repair it actually calls for — wait for the
app, or start it — and it is the shape the 2026-11-16 `iptv` splash warning took.

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
import re
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
#: Both names the estate's authentik answers on (`docs/identity-hosts.md`). The regression
#: is "a gate is back in front of this name", not one particular brand — asserting a single
#: host would let the other one back in silently, which is the failure this assertion
#: exists for, one rename later.
AUTH_HOSTS = ("auth.cerulean.innotel.us", "auth.innotel.us")
SPLASH_PATH = "/Branding/Splashscreen"
#: The SPA's top-level routes that carry their own chunk, as the served bundle declares
#: them (`path:"home"`, `path:"search"`, `path:"movies"`, …). `home` is the one the
#: 2026-11-16 error was on; the rest are the siblings a person clicks next, and they fail
#: the same way one click further in. A route this build does not name is skipped rather
#: than warned — a rename is not an outage — and walking *every* chunk would be a load test
#: of the edge rather than an answer about the app.
ROUTE_CHUNKS = ("home", "search", "favorites", "movies", "livetv", "shows", "music",
                "settings")
RUNTIME_HINT = "runtime.bundle.js"

# What this check is called in the shared family, and why it is a constant here at all:
# it used to publish `check="media-doors"` as an inline literal — the only check in the
# estate with no `CHECK`, and therefore the only one invisible to the hand-typed list that
# ties a script to its `EstateCheckAbsent` branch. A check whose absence nothing can name
# is the failure that rule exists for, and this one guards the watch door that nothing else
# in the estate looks at. The hyphen was the only one in the family too; the series is
# `media_doors` like every other. The textfile keeps its `estate-check-media-doors.prom`
# name, because that is a path in the unit, not a label.
CHECK = "media_doors"
WEB_PATH = "/web/"
DEFAULT_SPLASH_MIN = 50_000
USER_AGENT = "innotel-estate-check/1.0 (+media doors)"
#: What a proxy answers when it cannot reach the app behind it. Jellyfin's own start-up
#: window answers exactly this, so it is a distinct finding: the door is fine and the app
#: is not up yet, which is a different repair from "something else is answering".
GATEWAY_ERRORS = (502, 503, 504)
#: The findings a gateway answer produces, and therefore the ones a second look can clear.
#: A 502 is the app's start-up window as often as it is an outage: on 2026-11-16 a watchtower
#: restart of Jellyfin answered 502 for a couple of minutes and this check failed twice for
#: something that had repaired itself before anyone read the alert. They stay failures — a
#: door that is dark is dark — but only after the app has had the settle window to come back.
GATEWAY_CODES = ("front-gateway", "watch-gateway", "branding-gateway")


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


_RUNTIME_REF = re.compile(r'(?:src|href)="([^"]*' + re.escape(RUNTIME_HINT) + r'[^"]*)"')


def _runtime_url(base: str, page: bytes) -> str | None:
    """The runtime the index actually names, resolved against the page's own directory."""
    match = _RUNTIME_REF.search(page.decode("utf-8", "replace"))
    return urljoin(base, match.group(1)) if match else None


def _chunk_ids(runtime: str, name: str) -> list[str]:
    """The runtime's ids for one chunk name — `27962:"home"` → `27962`."""
    return re.findall(rf'([0-9]+):"{re.escape(name)}"', runtime)


def _chunk_hashes(runtime: str, chunk_id: str) -> list[str]:
    """The content hashes the runtime maps to that id — `27962:"6f5c…"` → `6f5c…`.

    A chunk carries one hash per asset kind, and the maps are separate and in no order the
    page reveals, so both hashes are collected and each extension is matched against all
    of them. The one that answers 200 is that kind's file; a cross pairing simply 404s.
    """
    return re.findall(rf'(?<![0-9]){chunk_id}:"([0-9a-f]{{20}})"', runtime)


def lazy_chunks(base: str, page: bytes, probe=fetch,
                routes: tuple[str, ...] = ROUTE_CHUNKS) -> list[Finding]:
    """Do the route chunks this page loads *after* boot exist, for the routes it has?

    Everything the index names can answer 200 and the first navigation still fail, because
    a route's files are named by the runtime map rather than by the index — the 2026-11-16
    shape. Every top-level route is asked the same way: the runtime's ids for that name
    give its content hashes, and each asset kind's file must answer 200. A runtime that
    cannot be read is a warning (the page may work, and a start-up window answers 502), but
    a runtime the page names and the app does not serve, or a route file that answers
    nothing, is a failure: the page loads and the click breaks.
    """
    findings: list[Finding] = []
    runtime_url = _runtime_url(base, page)
    if runtime_url is None:
        return [Finding(
            "chunk-map-unreadable", "warn",
            f"{base} serves a page that names no {RUNTIME_HINT}, so the routes' chunks "
            "cannot be read from here — the page may still work")]

    status, _, body = probe(runtime_url)
    if status is None or status in GATEWAY_ERRORS:
        return [Finding(
            "chunk-map-unreadable", "warn",
            f"{runtime_url} answered {status or 'nothing'} — the app may still be starting, "
            "so this is not evidence that a route chunk is missing")]
    if status != 200:
        return [Finding(
            "chunk-file-missing", "fail",
            f"{base} names {runtime_url}, which answers {status} — the web client cannot "
            "boot at all")]

    runtime = body.decode("utf-8", "replace")
    asked = 0
    for route in routes:
        hashes = sorted({value for chunk_id in _chunk_ids(runtime, route)
                         for value in _chunk_hashes(runtime, chunk_id)})
        if not hashes:
            continue  # this build has no such route: a rename is not an outage
        asked += 1
        # One hash per asset kind. `activity`, `keys` and `branding` import no stylesheet,
        # so they carry the script's hash only; demanding a `.css` for them would invent a
        # missing file on this build and on every build shaped like it.
        for suffix in (".chunk.js", ".css")[:min(len(hashes), 2)]:
            for value in hashes:
                url = urljoin(base, f"{route}.{value}{suffix}")
                status, _, payload = probe(url)
                if status == 200 and payload:
                    break
            else:
                findings.append(Finding(
                    "chunk-file-missing", "fail",
                    f"{base} names a {route}{suffix} chunk ({', '.join(hashes)}) that the "
                    "app does not serve: the front page loads and the first navigation to "
                    "that route fails with a chunkload error"))
    if not asked:
        return [Finding(
            "chunk-map-unreadable", "warn",
            f"{runtime_url} names none of {', '.join(routes)} with a content hash — the "
            "client's build format has changed, so this assertion asked nothing")]
    return findings


def audit(media: str, watch: str, *, probe=fetch, splash_min: int = DEFAULT_SPLASH_MIN) -> Audit:
    """The four assertions, as findings. Pure enough to test with a fake `probe`."""
    result = Audit()

    #: Where the front door's own page lives, once it is known to be Jellyfin's, and the
    #: page itself — the lazy-route assertion reads its runtime from these. Unset when the
    #: page is wrong, because probing the chunks of a page that is not Jellyfin's would
    #: only add noise to the finding that already says so.
    front_base: str | None = None
    front_page = b""

    status, headers, body = probe(media)
    location = headers.get("Location", "") or headers.get("location", "")
    if status is None:
        result.findings.append(Finding(
            "front-unreachable", "fail", f"{media} did not answer — DNS, TLS or the edge is down"))
    elif 300 <= status < 400 and any(host in location for host in AUTH_HOSTS):
        result.findings.append(Finding(
            "gate-restored", "fail",
            f"{media} redirects to {next(h for h in AUTH_HOSTS if h in location)}: the OIDC "
            "gate is back in front of the "
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
        else:
            front_base = target if target.endswith("/") else target + "/"
            front_page = landed_body
    elif status == 200 and not _names_jellyfin(body):
        result.findings.append(Finding(
            "front-not-jellyfin", "fail",
            f"{media} answered 200 but the page does not name Jellyfin — something else is "
            "answering on the front door"))
    elif status == 200:
        # The door answers with the app's page directly instead of the `302 web/` hop, so
        # the route's files resolve from the web root beside it.
        front_base = urljoin(media, WEB_PATH)
        front_page = body
    elif 300 <= status < 400:
        result.findings.append(Finding(
            "front-wrong-target", "fail",
            f"{media} redirects off the app to {location or 'nowhere'}"))
    elif status in GATEWAY_ERRORS:
        result.findings.append(Finding(
            "front-gateway", "fail",
            f"{media} answered {status} — the edge could not reach Jellyfin, so the app is "
            "down or still starting; that is its start-up window, not another app on the "
            "front door"))
    elif status != 200:
        result.findings.append(Finding(
            "front-status", "fail",
            f"{media} answered {status} — the gateway or the app is down behind the name"))

    status, _, body = probe(watch + WEB_PATH)
    text = body[:64_000].decode("utf-8", "replace").lower()
    if status is None:
        result.findings.append(Finding(
            "watch-unreachable", "fail", f"{watch}{WEB_PATH} did not answer — Jellyfin or the edge is down"))
    elif status in GATEWAY_ERRORS:
        result.findings.append(Finding(
            "watch-gateway", "fail",
            f"{watch}{WEB_PATH} answered {status} — the edge could not reach Jellyfin: the "
            "app is down or in its start-up window, and the door recovers when it does"))
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
    elif status in GATEWAY_ERRORS:
        result.findings.append(Finding(
            "branding-gateway", "warn",
            f"{watch}{SPLASH_PATH} answered {status} — Jellyfin was unreachable when the "
            "splash was asked for (its start-up window answers this), so this is not "
            "evidence that the Gizmo splash is uninstalled; re-check once the app is up"))
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

    if front_base:
        result.findings.extend(lazy_chunks(front_base, front_page, probe))
    return result


def settle(
    media: str,
    watch: str,
    *,
    probe=fetch,
    splash_min: int = DEFAULT_SPLASH_MIN,
    attempts: int = 3,
    delay: float = 15.0,
    sleep=time.sleep,
) -> tuple[Audit, list[Finding]]:
    """Audit, and give a gateway answer the app's start-up window before calling it down.

    Only a result whose failures are *all* gateway answers is re-examined, and only while
    attempts remain: any other failure is a wrong page on a working door, which a second
    look would not change. A later clean pass turns the earlier gateway findings into notes
    that say how long the app took, so "0 failures" still tells the reader what was seen.
    Nothing is retried forever — an app that stays dark past the window is a failure.
    """
    result = audit(media, watch, probe=probe, splash_min=splash_min)
    recovered: list[Finding] = []
    waited = 0.0
    for _ in range(max(0, attempts - 1)):
        if not result.failures or any(code not in GATEWAY_CODES for code in
                                      (finding.code for finding in result.failures)):
            break
        sleep(delay)
        waited += delay
        later = audit(media, watch, probe=probe, splash_min=splash_min)
        if later.ok:
            recovered = [
                Finding(finding.code.replace("-gateway", "-recovered"), "note",
                        f"{finding.message} — it answered again after {waited:.0f}s, so this "
                        "was the app's start-up window and the door is serving")
                for finding in result.findings if finding.code in GATEWAY_CODES
            ]
            result = later
            break
        result = later
    return result, recovered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--media", default=MEDIA_DOOR, help="the gated front page")
    parser.add_argument("--watch", default=WATCH_DOOR, help="the Jellyfin watch door")
    parser.add_argument("--splash-min-bytes", type=int, default=DEFAULT_SPLASH_MIN)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--settle-attempts", type=int, default=3,
                        help="passes over the doors while only gateway answers are failing")
    parser.add_argument("--settle-delay", type=float, default=15.0,
                        help="seconds between settle passes (the app's start-up window)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran_ok=ran)
        except OSError as error:
            print(f"check-media-doors: could not write {args.prom}: {error}", file=sys.stderr)

    probe = lambda url: fetch(url, timeout=args.timeout)  # noqa: E731
    result, recovered = settle(args.media, args.watch, probe=probe,
                               splash_min=args.splash_min_bytes,
                               attempts=args.settle_attempts, delay=args.settle_delay)
    result.findings.extend(recovered)

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
