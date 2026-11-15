#!/usr/bin/env python3
"""Is the classroom still behind a sign-in — on both names, and for the right client?

WHY THIS EXISTS
---------------
`class.atheniq.courses` is where the courseware sends a learner: every classroom link in
`atheniq-work` is `${CLASSROOM_HOST}/classroom/<id>`, 302 to the estate's authentik with
`client_id=classroom` and `redirect_uri=https://class.atheniq.courses/oauth2/callback`.
Three things make that door work and none of them is visible from inside the app:

  * **the gate is on the name, not the landing page.** A proxy host whose gate covers `/`
    but not `/classroom/…` hands an anonymous learner a 404 from a system that still looks
    gated from the front; the deep links are the only URLs the courseware uses.
  * **the client and the callback agree with the provider.** `client_id=classroom` is
    registered against two callbacks, and a door that asks for a callback the provider does
    not hold fails *at the callback*, after the person has signed in — the same generic
    error as a wrong issuer, and the same shape as the 2026-11-14 Orion door.
  * **the name the door asks of is the one the estate publishes.** The estate serves one
    authentik under two names and each reports itself, so a door consistently on
    `auth.cerulean.innotel.us` works and is nevertheless drift — a warning here, per
    `docs/identity-hosts.md`, never a failure.

`classroom.atheniq.courses` is the superseded name (`docs/atheniq-classroom-name.md`): it
still answers with the same app and the same client, and it is in the *learn* certificate's
SAN rather than one of its own. Either state is correct while the retirement is finished —
gated like the classroom, or a redirect to it — so this check accepts both and fails on
everything else, including a third name appearing under the same client.

WHAT IT ASSERTS
---------------
  1. `https://class.atheniq.courses/` answers **3xx to authentik** with
     `client_id=classroom` and the door's own `/oauth2/callback`. A 200 is the gate having
     been removed (the one thing this check exists to notice); a 5xx is the edge or the app
     down behind a name that still resolves.
  2. `https://class.atheniq.courses/classroom/` is gated too — the path the links use.
  3. `https://classroom.atheniq.courses/` is gated the same way, or 3xx to the classroom
     (a note: that is the retirement happening).
  4. The certificate covers the name it answers on — a TLS failure is reported as a
     certificate finding rather than as "unreachable", because the name did answer.

Run it from the edge, where the names resolve and the certificates are the ones a person
gets:

    ./scripts/check-atheniq-doors.py
    ./scripts/check-atheniq-doors.py --prom ${TEXTFILE_DIR}/estate-check-atheniq-doors.prom
    ./scripts/check-atheniq-doors.py --json

Exit codes: 0 = both doors behaved, 1 = one did not, 2 = the classroom itself did not
answer at all (no route from here, which is not an answer about the door).
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from estate_check import Audit, Finding, write_prom  # noqa: E402

#: The `check` label this check publishes under (`estate-checks.yml` alerts on it).
CHECK = "atheniq_doors"

#: The client every classroom sign-in names. One provider, one client, both doors.
CLIENT_ID = "classroom"

#: The canonical identity host (`docs/identity-hosts.md`). The other name is a warning: a
#: door consistently on it authenticates people perfectly, and the brand is the direction.
CANONICAL_AUTH = "auth.innotel.us"
AUTH_HOSTS = (CANONICAL_AUTH, "auth.cerulean.innotel.us")

#: The path the courseware's links are under. `/` alone proves nothing about the gate: a
#: proxy host can carry a redirect on the landing page and pass everything under it.
DEEP_PATH = "/classroom/"

USER_AGENT = "innotel-estate-check/1.0 (+atheniq classroom doors)"


@dataclass(frozen=True)
class Door:
    """One public name in front of the classroom, and what it is allowed to do."""

    host: str
    role: str  # "classroom" (must be gated) | "alias" (gated, or moved to the classroom)
    canonical: str = ""  # for an alias: the name it may redirect to instead

    @property
    def url(self) -> str:
        return f"https://{self.host}/"

    @property
    def deep_url(self) -> str:
        return f"https://{self.host}{DEEP_PATH}"

    @property
    def callback(self) -> str:
        """The callback this door must ask the provider for — its own name, not another's.

        Derived rather than written beside the host, because the failure this catches is
        exactly a door asking for a callback that names a different door (the 2026-11-06
        `classroom.` request that was not registered until it was added by hand).
        """
        return f"https://{self.host}/oauth2/callback"


DOORS = (
    Door("class.atheniq.courses", "classroom"),
    Door("classroom.atheniq.courses", "alias", "https://class.atheniq.courses"),
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a 3xx, so the gate's own answer is what comes back.

    `urlopen` follows redirects by default, which turned the gate's 302 into authentik's
    200 and made a gated door read as an open one.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, timeout: float = 20.0) -> tuple[int | None, dict[str, str], bytes, str | None]:
    """GET one URL without following redirects.

    Returns `(status, headers, body, error)` where `error` is `None`, `"tls"` (the name
    answered but its certificate does not cover it) or `"transport"` (DNS, connect,
    timeout). The distinction is the difference between a finding about the door and a
    reason this host cannot judge it.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(64_000), None
    except urllib.error.HTTPError as error:  # a status is an answer, not a failure to ask
        headers = dict(error.headers or {})
        return error.code, headers, error.read(4_096) if error.fp else b"", None
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return None, {}, b"", "tls"
        return None, {}, b"", "transport"
    except ssl.SSLError:
        return None, {}, b"", "tls"
    except Exception:  # noqa: BLE001 — anything else is "no answer" for this check
        return None, {}, b"", "transport"


def _location(headers: dict[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "location":
            return value
    return ""


def _moves_to(door: Door, location: str) -> bool:
    """Whether this 3xx is the alias being retired rather than a sign-in being offered."""
    if door.role != "alias" or not door.canonical:
        return False
    host = urlsplit(location).hostname or ""
    return host == (urlsplit(door.canonical).hostname or "")


def _gate(door: Door, location: str, *, deep: bool = False) -> list[Finding]:
    """The statements the redirect makes, each checked against the provider's own facts."""
    parts = urlsplit(location)
    query = parse_qs(parts.query)
    findings: list[Finding] = []

    if parts.scheme != "https" or (parts.hostname or "") not in AUTH_HOSTS:
        return [Finding(
            "issuer_unrecognised", "fail",
            f"{door.url} sends a person to {location or 'nowhere'}, which is not either of "
            f"the estate's identity hosts ({', '.join(AUTH_HOSTS)}) — whatever is answering "
            "here is not the gate this name is supposed to have")]
    if "authorize" not in parts.path:
        findings.append(Finding(
            "issuer_unrecognised", "fail",
            f"{door.url} sends a person to {location}, which is not an authorize endpoint"))
        return findings

    client = (query.get("client_id") or [""])[0]
    if client != CLIENT_ID:
        findings.append(Finding(
            "wrong_client", "fail",
            f"{door.url} asks the provider for client `{client or '(none)'}`, not "
            f"`{CLIENT_ID}` — a session minted for another application is not the "
            "classroom's, and the procedure will not match the account's own rules"))

    callback = (query.get("redirect_uri") or [""])[0]
    if callback != door.callback:
        findings.append(Finding(
            "wrong_callback", "fail",
            f"{door.url} asks the provider to come back to {callback or '(none)'}, but this "
            f"door's callback is {door.callback} — the registered one is not what it is "
            "asking for, which fails *after* a correct sign-in"))

    # A note rather than a warning, and said once per door rather than once per path. The
    # estate's doors are consistently on the cerulean name, which is what
    # `docs/identity-hosts.md` actually requires (the two sides of one door must agree); the
    # brand is a direction, not a correctness property. And `EstateCheckNotPinned` pages on
    # any warning older than an hour, so a warning here would be a page that cannot end
    # until a provider-side change happens — the kind people learn to close.
    if not deep and (parts.hostname or "") != CANONICAL_AUTH:
        findings.append(Finding(
            "issuer_alias", "note",
            f"{door.url} asks {parts.hostname} rather than {CANONICAL_AUTH} — it works, "
            "because both names are the same instance; a note rather than a warning "
            "because only a provider-side change can end it (docs/identity-hosts.md)"))
    return findings


def judge(door: Door, url: str, answer, *, deep: bool = False) -> list[Finding]:
    """One request, as findings. Pure, so the rules are the thing under test."""
    status, headers, _body, error = answer
    if error == "tls":
        return [Finding(
            "tls_not_covering", "fail",
            f"{url} answered, but the certificate does not cover `{door.host}` — a person "
            "gets an interstitial before the gate, and some clients refuse outright")]
    if error is not None:
        if door.role == "alias":
            # The superseded name is expendable by design: gone is a retirement, and
            # `EstateCheckAbsent` would still notice the check itself disappearing.
            return [Finding("alias_unreachable", "note",
                            f"{url} did not answer — the superseded name is gone rather "
                            "than moved, which is allowed, but only if that was intended")]
        return [Finding("unreachable", "fail",
                        f"{url} did not answer — DNS, the edge or the app is down")]

    if status == 200:
        return [Finding(
            "deep_path_ungated" if deep else "door_open", "fail",
            f"{url}{'' if not deep else ' (the path the courseware links use)'} answered 200 "
            "with no gate — a learner link now opens the app anonymously")]
    if status is not None and 300 <= status < 400:
        location = _location(headers)
        if _moves_to(door, location):
            return [Finding("alias_moved", "note",
                            f"{url} redirects to {location}: the superseded name is being "
                            "retired to the classroom, which is the documented end state")]
        return _gate(door, location, deep=deep)
    return [Finding(
        "deep_path_ungated" if deep else "door_status", "fail",
        f"{url} answered {status} — that is the gate or the app behind it failing, not a "
        "sign-in being offered")]
    # (unreachable above; a 4xx here is the app answering, which means no gate)


def audit(probe=fetch) -> tuple[Audit, bool]:
    """Every door, as findings. Returns `(findings, the classroom answered at all)`."""
    result = Audit()
    classroom_answered = False
    for door in DOORS:
        answer = probe(door.url)
        root = judge(door, door.url, answer)
        result.findings += root
        if door.role == "classroom" and answer[3] != "transport":
            classroom_answered = True
        # The deep path matters for a gated door and not for one that has been moved: a
        # redirection host answers every path on the name the same way, so probing it again
        # would state the same note twice.
        moved = any(f.code == "alias_moved" for f in root)
        if not moved:
            result.findings += judge(door, door.deep_url, probe(door.deep_url), deep=True)
    return result, classroom_answered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    probe = lambda url: fetch(url, timeout=args.timeout)  # noqa: E731
    result, answered = audit(probe=probe)

    def emit(audit_result: Audit | None, ran_ok: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, audit_result, ran_ok=ran_ok)
        except OSError as error:
            print(f"check-atheniq-doors: could not write {args.prom}: {error}", file=sys.stderr)

    if not answered:
        emit(None, ran_ok=False)
        print("check-atheniq-doors: the classroom did not answer — this host cannot see "
              f"{DOORS[0].host}", file=sys.stderr)
        return 2

    emit(result, ran_ok=True)
    if args.json:
        print(json.dumps({
            "doors": [{"host": d.host, "role": d.role, "callback": d.callback} for d in DOORS],
            "findings": [{"code": f.code, "level": f.level, "message": f.message}
                         for f in result.findings],
            "failures": len(result.failures), "warnings": len(result.warnings),
            "notes": len(result.notes),
        }, indent=2, sort_keys=True))
        return 0 if result.ok else 1

    for door in DOORS:
        print(f"  {door.url:<38} {door.role:<10} callback {door.callback}")
    for finding in result.findings:
        # The code is printed, not only the sentence: it is the label an alert and a textfile
        # key on, and it is what makes "which of the four statements" greppable in a log.
        print(f"{finding.level.upper():4} {finding.code:<22} {finding.message}")
    print(f"check-atheniq-doors: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s), {len(result.notes)} note(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
