#!/usr/bin/env python3
"""Will the edge hand a *learner* a session — and refuse everyone it should?

WHY THIS EXISTS
---------------
`docs/sign-in-posture.md` says identity lives in Cerulean's Authentik and the NPM edge is
a door, not a second user directory. The edge's half of that is one endpoint,
`POST /api/tokens/sso`: it believes the gateway's `X-Forwarded-Email/User/Groups` headers
only when the request arrives over the edge's own loopback *and* the edge's vhost has
stamped `X-NPM-Edge: yes` on it. Three ways that goes wrong, all of which look like the
same 403 in a browser:

  * the vhost's peer map stopped matching the gateway (`$remote_addr` rewritten by the
    realip module from `X-Real-IP` — 2026-10-29, every sign-in refused);
  * the identity is not in `AUTH_SSO_REQUIRED_GROUP`, so a perfectly good Authentik
    sign-in ends in a 403 the user reads as "bad password";
  * the request never passed the gateway, which must be refused even when it carries
    convincing headers — that is the whole control.

A real browser round trip cannot cover the first two cheaply (it needs a password, an
Authentik flow and a cookie), and it cannot cover the third at all. This drives the
endpoint directly from inside the edge container, where the gateway's hop looks like
loopback, and asserts all three verdicts. It is the edge's half of the flow; the
Authentik half is oauth2-proxy's `--allowed-group`, which the gateway logs when it
refuses.

WHAT IT ASSERTS
---------------
  1. a learner identity **in** the required group gets a token (200, non-empty);
  2. the same identity **without** the group is refused (403);
  3. headers forged from a non-edge peer are refused (403) — the gateway cannot be
     impersonated by anything that can reach the published admin port.

Run it where docker can reach the edge container (the `proxy` container on i2):

    ./scripts/check-edge-sso-door.py --container cerulean-npm
    ./scripts/check-edge-sso-door.py --required-group cerulean-platform \
        --learner-email learner@example.org --learner-groups "AthenIQ LMS,learners"
    ./scripts/check-edge-sso-door.py --prom ${TEXTFILE_DIR}/estate-check-edge-sso-door.prom

Exit codes: 0 = every case behaved, 1 = one did not, 2 = nothing to test (no curl in the
container, or the endpoint does not exist).
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # the estate's shared telemetry contract, beside this file when it is installed
    from estate_check import Audit, Finding, write_prom  # noqa: E402
    HAVE_TELEMETRY = True
except ImportError:  # a bare copy still has to run: --prom is then simply unavailable
    Audit = Finding = write_prom = None  # type: ignore[assignment]
    HAVE_TELEMETRY = False

#: The `check` label this check publishes under (`estate-checks.yml` alerts on it).
CHECK = "edge_sso_door"

LOOPBACK_URL = "http://127.0.0.1:81/api/tokens/sso"
#: The edge's own address on `npm-gate` — a peer the vhost must NOT vouch for.
EDGE_PEER_URL = "http://172.31.42.20:81/api/tokens/sso"


@dataclass(frozen=True)
class Case:
    """One request and the status it has to produce."""

    name: str
    url: str
    headers: tuple[str, ...]
    expect: int
    note: str


def cases(url: str, email: str, groups: str, required_group: str) -> list[Case]:
    """The three verdicts, as requests. `groups` is the learner's Authentik group list."""
    identity = [f"X-Forwarded-Email: {email}", "X-Forwarded-User: learner"]
    return [
        Case(
            name="learner in the required group",
            url=url,
            headers=(*identity, f"X-Forwarded-Groups: {groups}"),
            expect=200,
            note="a learner's sign-in has to end somewhere other than a 403",
        ),
        Case(
            name="learner outside the required group",
            url=url,
            headers=(*identity, "X-Forwarded-Groups: learners"),
            expect=403,
            note=f"only `{required_group}` may hold the admin UI",
        ),
        Case(
            name="forged identity from the admin port",
            url=EDGE_PEER_URL,
            headers=("X-Forwarded-Email: attacker@example.org",
                     "X-Forwarded-Groups: cerulean-platform"),
            expect=403,
            note="no gateway, no session — the published port must not mint one",
        ),
    ]


def run_case(case: Case, container: str, docker: list[str] | None = None) -> tuple[int, str]:
    """Send one case with curl *inside* the edge container; return (status, body)."""
    command = [*(docker or ["docker"]), "exec", container, "curl", "-s", "-o", "-",
               "-w", "\n%{http_code}"]
    command += ["-m", "8", "-X", "POST", "-H", "Content-Type: application/json"]
    for header in case.headers:
        command += ["-H", header]
    command += ["-d", "{}", case.url]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return 0, (done.stderr or done.stdout).strip()
    body, _, status = done.stdout.rpartition("\n")
    try:
        return int(status.strip() or 0), body.strip()
    except ValueError:
        return 0, done.stdout.strip()


def verdict(case: Case, status: int, body: str) -> bool:
    """A 200 has to carry a token; anything else has to be the endpoint's own refusal."""
    if status != case.expect:
        return False
    if case.expect == 200:
        try:
            return bool(json.loads(body).get("token"))
        except ValueError:
            return False
    return "Permission Denied" in body or body == ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive the edge's half of the SSO flow.")
    parser.add_argument("--container", default="cerulean-npm")
    parser.add_argument("--docker", default="docker",
                        help="command prefix that reaches the docker CLI, e.g. "
                             "'incus exec proxy -- docker' when the edge is reached through Incus")
    parser.add_argument("--url", default=LOOPBACK_URL,
                        help="endpoint as the gateway's hop sees it (loopback by default)")
    parser.add_argument("--learner-email", default="learner@innotel.us")
    parser.add_argument("--learner-groups", default="cerulean-platform,AthenIQ LMS",
                        help="the groups claim that identity carries in Authentik")
    parser.add_argument("--required-group", default="cerulean-platform")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    prefix = shlex.split(args.docker)
    failed = 0
    audit = Audit() if HAVE_TELEMETRY else None
    for case in cases(args.url, args.learner_email, args.learner_groups, args.required_group):
        status, body = run_case(case, args.container, prefix)
        ok = verdict(case, status, body)
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL':4}  {case.name:38} "
              f"HTTP {status or '---'} (wanted {case.expect})")
        if not ok:
            print(f"      {case.note}")
            print(f"      body: {body[:200]}")
            if audit is not None:
                audit.findings.append(Finding(
                    code="door_verdict", level="fail",
                    message=f"{case.name}: HTTP {status or '---'}, wanted {case.expect}"))
    print()
    print("all three verdicts hold" if failed == 0 else f"{failed} case(s) failed")
    if args.prom:
        if not HAVE_TELEMETRY:
            print("check-edge-sso-door: --prom needs estate_check.py beside this file",
                  file=sys.stderr)
        else:
            write_prom(args.prom, CHECK, audit, audit is not None)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
