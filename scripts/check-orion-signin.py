#!/usr/bin/env python3
"""Say which hop of the Orion NAS sign-in is broken, without a password.

WHY THIS EXISTS
---------------
On 2026-11-14 the door reported Authentik's own "There was an error submitting the form.
Response returned an error code." Everything reachable from outside looked right: the
name redirected through the authorize endpoint to the login flow, `/ping` answered, the
API answered with its own 403 JSON on both brands. The one failing path, `/oauth2/callback`,
answers 500 with a Request ID and no message — and it answers that for a *bogus* code too,
so probing it distinguishes nothing. The defect was in the one place no probe pointed at:
the proxy trusted an issuer named by one brand while the estate serves the same authentik
under two, and each brand reports **itself** as `issuer`. Login completed at the provider
and died at the callback, which is the failure this file exists to name.

WHAT IT READS
-------------
Three statements of the same fact, and the check is whether they agree:

1. **What the proxy trusts** — `OAUTH2_PROXY_OIDC_ISSUER_URL` and
   `OAUTH2_PROXY_CLIENT_ID` in `orion-sso.env`, read from the edge host that runs the
   container (`--env` / `--sso-host`). This is the only readable record of the setting;
   oauth2-proxy exposes no endpoint that reports its own configuration.
2. **What that issuer says it is** — the discovery document's `issuer` field. `iss` is
   compared to it on the token exchange, so a document that disagrees with the configured
   URL refuses every token the provider issues, whatever the login page did.
3. **What the door sends people to** — the `Location` of `/oauth2/start`, un-followed.
   Its host must be the issuer's host and its `client_id` must be the configured client.
   This is the assertion that would have caught the drift as soon as it existed, and the
   only one that does not need the proxy to be restarted to be true.

THE RULES
---------
1. **Env unreadable, or either key missing** → exit 2. Nothing can be judged.
2. **Discovery unreachable, or not a 200** → failure: the proxy cannot verify a token
   against a document it cannot fetch.
3. **Discovery `issuer` ≠ configured issuer** → failure, quoting both. This is the
   500-at-the-callback class, and it is exact — trailing slash included.
4. **`/oauth2/start` unreachable** → exit 2, like every check here: an unreachable door is
   not a healthy one.
5. **Redirect host ≠ issuer host** → failure. The proxy sends people to a name it will not
   accept tokens from, which reads to them as a login that silently does nothing.
6. **Redirect `client_id` ≠ configured client** → failure: that is a different provider.
7. **Authorize endpoint not serving a login page** → failure, with the status.
8. **A brand that is not the canonical one** → **warning**, not a failure. The estate's
   canonical identity host is `auth.innotel.us` (`docs/identity-hosts.md`); a door on the
   other brand is self-consistent and works, it is just not where the estate is going.

Usage:
    ./scripts/check-orion-signin.py
    ./scripts/check-orion-signin.py --prom /path/to/textfile
    ./scripts/check-orion-signin.py --env /root/orion-sso/orion-sso.env
    ./scripts/check-orion-signin.py --sso-host root@192.168.1.71   # read it over ssh

Exit codes: 0 = the door, the issuer and the login page agree, 1 = one of them does not,
2 = could not run (an unreadable env file or an unreachable door is never a pass).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, read_host, ssh, write_prom  # noqa: E402

CHECK = "orion_signin"

#: The door and the hop that starts a sign-in on it. `/oauth2/start` is the right thing to
#: read: it is a 302 built from the proxy's own configuration, so it reports the issuer it
#: is about to trust without needing a session or a credential.
DOOR = "https://nas.innotel.us"
START_PATH = "/oauth2/start"

#: The estate's canonical identity host (`docs/identity-hosts.md`). Not a correctness
#: requirement — a door on the other brand works — which is why it is a warning.
CANONICAL_HOST = "auth.innotel.us"

#: Where the proxy's env file lives on the edge, and the edge itself. The unit that starts
#: this check runs on that host, so the file is usually local; `--sso-host` covers the case
#: where this check is run from somewhere else.
ENV_PATH = "/root/orion-sso/orion-sso.env"
SSO_HOST = "root@192.168.1.71"

ISSUER_KEY = "OAUTH2_PROXY_OIDC_ISSUER_URL"
CLIENT_KEY = "OAUTH2_PROXY_CLIENT_ID"

USER_AGENT = "innotel-estate-check/1.0 (+orion signin)"
TIMEOUT = 20.0
DISCOVERY_SUFFIX = ".well-known/openid-configuration"

#: The 3xx codes whose Location can carry a person on to the login page.
REDIRECT = (301, 302, 303, 307, 308)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a 3xx: on this door the redirect *is* the measurement."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, timeout: float = TIMEOUT) -> tuple[int | None, dict[str, str], bytes]:
    """GET one URL without following redirects; any transport failure is `(None, {}, b"")`."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(16_384)
    except urllib.error.HTTPError as error:  # a status is an answer, not a failure to ask
        return error.code, dict(error.headers or {}), error.read(4_096) if error.fp else b""
    except Exception:  # noqa: BLE001 — DNS, TLS, timeout: all "no answer" here
        return None, {}, b""


def parse_env(text: str) -> dict[str, str]:
    """The keys of an env file, as compose and systemd read it. Pure, for the tests."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # A value may be quoted; an inline `#` after an unquoted value is not a comment in
        # either consumer, so nothing is stripped here beyond the quotes themselves.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass
class Measured:
    """The three statements, before any judgement about them."""

    issuer: str = ""
    client_id: str = ""
    discovery_status: int | None = None
    discovery_issuer: str = ""
    start_status: int | None = None
    start_location: str = ""
    authorize_status: int | None = None
    authorize_location: str = ""
    login_status: int | None = None
    login_location: str = ""


def gather(issuer: str, client_id: str, get=None) -> Measured:
    """Ask the issuer and the door; no judgement, so the rules below stay testable.

    `get` defaults to this module's `fetch` **at call time**, not at definition time, so a
    test can replace `fetch` and have the real path exercise it.
    """
    get = get or fetch
    measured = Measured(issuer=issuer.strip(), client_id=client_id.strip())
    if not measured.issuer:
        return measured

    base = measured.issuer.rstrip("/")
    status, _, body = get(f"{base}/{DISCOVERY_SUFFIX}")
    measured.discovery_status = status
    if status == 200:
        try:
            document = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            document = {}
        if isinstance(document, dict):
            measured.discovery_issuer = str(document.get("issuer") or "")

    status, headers, _ = get(f"{DOOR}{START_PATH}")
    measured.start_status = status
    measured.start_location = headers.get("Location") or headers.get("location") or ""

    if measured.start_location:
        measured.authorize_status, headers, _ = get(measured.start_location)
        measured.authorize_location = headers.get("Location") or headers.get("location") or ""
        # authentik hands the authorize request to its flow executor, which serves the form.
        # Follow that one hop so the judgement is about the page a person lands on.
        if measured.authorize_status in REDIRECT and measured.authorize_location:
            measured.login_location = urljoin(measured.start_location, measured.authorize_location)
            measured.login_status = get(measured.login_location)[0]
    return measured


def reaches_login_page(measured: Measured, issuer_host: str) -> bool:
    """Whether the authorize request lands a person on a login page.

    A 200 is the provider serving the form itself. A redirect is the provider handing the
    request to its flow executor — authentik does that for every sign-in — so what counts
    is the page that redirect points at: a 200 on the issuer's own host. Anything else
    (a dead flow, a bounce to another name) leaves the person without a way in.
    """
    if measured.authorize_status == 200:
        return True
    if measured.authorize_status not in REDIRECT or not measured.login_location:
        return False
    return (measured.login_status == 200
            and urlsplit(measured.login_location).netloc == issuer_host)


def audit(measured: Measured) -> Audit:
    """Judge the three statements against each other. Pure, for the tests."""
    out = Audit()

    if not measured.issuer:
        out.findings.append(
            Finding(
                "issuer_unset",
                "fail",
                f"{ISSUER_KEY} is not in the proxy's env file: oauth2-proxy will not start "
                "at all, so the door answers nothing",
            )
        )
        return out

    issuer = measured.issuer
    issuer_host = urlsplit(issuer).netloc

    if measured.discovery_status is None:
        out.findings.append(
            Finding(
                "discovery_unreachable",
                "fail",
                f"no answer from {issuer}{DISCOVERY_SUFFIX} — the proxy cannot verify any "
                "token against a document it cannot fetch",
            )
        )
    elif measured.discovery_status != 200:
        out.findings.append(
            Finding(
                "discovery_status",
                "fail",
                f"{issuer}{DISCOVERY_SUFFIX} answered {measured.discovery_status}; the "
                "discovery document has to be a 200 for a login to be verified",
            )
        )
    elif measured.discovery_issuer != issuer:
        # The one that fails as a 500 at the callback with nothing else to see: `iss` is
        # compared to this string, so a document naming a different brand (or the same
        # brand with the trailing slash missing) refuses every token.
        out.findings.append(
            Finding(
                "issuer_mismatch",
                "fail",
                f"the proxy trusts {issuer!r} and that issuer's discovery says "
                f"{measured.discovery_issuer!r}; every login completes at the provider and "
                "then dies on the callback with a 500",
            )
        )

    if issuer_host and issuer_host != CANONICAL_HOST:
        out.findings.append(
            Finding(
                "brand_not_canonical",
                "warn",
                f"the proxy trusts {issuer_host}, not the canonical {CANONICAL_HOST} "
                "(docs/identity-hosts.md): it works, and it is not where the estate is going",
            )
        )

    if measured.start_status is None:
        out.findings.append(
            Finding(
                "door_unreachable",
                "fail",
                f"no answer from {DOOR}{START_PATH} — the door cannot start a sign-in, so "
                "nobody gets in",
            )
        )
        return out
    if measured.start_status not in (301, 302, 303, 307):
        out.findings.append(
            Finding(
                "start_status",
                "fail",
                f"{DOOR}{START_PATH} answered {measured.start_status}; a sign-in starts "
                "with a redirect to the provider",
            )
        )
        return out

    if not measured.start_location:
        out.findings.append(
            Finding(
                "no_location",
                "fail",
                f"{DOOR}{START_PATH} redirected without a Location",
            )
        )
        return out

    parts = urlsplit(measured.start_location)
    if parts.netloc and parts.netloc != issuer_host:
        out.findings.append(
            Finding(
                "redirect_wrong_brand",
                "fail",
                f"the door sends people to {parts.netloc} but trusts "
                f"{issuer_host}: the sign-in happens on one name and the token is refused "
                "by the other, which reads as a login that silently does nothing",
            )
        )

    if measured.client_id:
        sent = (parse_qs(parts.query).get("client_id") or [""])[0]
        if sent and sent != measured.client_id:
            out.findings.append(
                Finding(
                    "client_mismatch",
                    "fail",
                    f"the door asks {parts.netloc} for client {sent!r} and its env names "
                    f"{measured.client_id!r} — a different provider is in front of the door",
                )
            )

    if measured.authorize_status is None:
        out.findings.append(
            Finding(
                "authorize_unreachable",
                "fail",
                f"no answer from the authorize endpoint the door redirects to "
                f"({measured.start_location})",
            )
        )
    elif not reaches_login_page(measured, issuer_host):
        landed = (f"and the page it handed on to answered {measured.login_status}"
                  if measured.authorize_location else "and it sent them nowhere")
        out.findings.append(
            Finding(
                "authorize_status",
                "fail",
                f"{parts.netloc} answered {measured.authorize_status} for the door's own "
                f"authorize request (client {measured.client_id or 'unknown'}) {landed}; "
                "the person never reaches a login page",
            )
        )

    if not out.findings:
        out.findings.append(
            Finding(
                "sign_in_agrees",
                "note",
                f"the door redirects to {issuer_host} for client "
                f"{measured.client_id or 'unknown'}, that issuer reports itself as {issuer}, "
                "and it serves a login page",
            )
        )
    elif not out.failures:
        out.findings.append(
            Finding(
                "sign_in_works",
                "note",
                f"the door and {issuer} agree and serve a login page",
            )
        )
    return out


def read_env_text(path: str, sso_host: str) -> tuple[str | None, str]:
    """The env file's contents, local first and then over the estate's ssh path.

    Returns `(text, why_not)`: the edge runs this check and holds the file, so the local
    read is the normal case; `--sso-host` is for running it from anywhere else.
    """
    if Path(path).is_file():
        try:
            return Path(path).read_text(encoding="utf-8"), ""
        except OSError as error:
            return None, f"{path}: {error}"
    if not sso_host:
        return None, f"{path} does not exist on this host and no --sso-host was given"
    try:
        return read_host(sso_host, sso_host, f"cat {path}", ssh), ""
    except Exception as error:  # noqa: BLE001 — an unreadable env is one verdict
        return None, str(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--env", metavar="PATH", default=ENV_PATH, help=f"the proxy's env file (default: {ENV_PATH})")
    parser.add_argument("--sso-host", metavar="TARGET", default=SSO_HOST,
                        help=f"ssh target holding the env file when it is not local (default: {SSO_HOST})")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-orion-signin: could not write {args.prom}: {error}", file=sys.stderr)

    text, why_not = read_env_text(args.env, args.sso_host)
    if text is None:
        print(f"check-orion-signin: could not read the proxy's env: {why_not}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    values = parse_env(text)
    issuer, client_id = values.get(ISSUER_KEY, ""), values.get(CLIENT_KEY, "")
    if not issuer or not client_id:
        missing = ", ".join(k for k in (ISSUER_KEY, CLIENT_KEY) if not values.get(k))
        print(f"check-orion-signin: {args.env} has no {missing}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    measured = gather(issuer, client_id)
    result = audit(measured)
    if any(f.code == "door_unreachable" for f in result.findings):
        # Nothing about a door that answers nothing is a verdict about its configuration.
        emit(None, ran=False)
        for finding in result.findings:
            print(f"FAIL {finding.message}", file=sys.stderr)
        return 2

    emit(result, ran=True)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(f"\ncheck-orion-signin: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
