#!/usr/bin/env python3
"""Fail when a sign-in gate's callback cannot get through the proxy in front of it.

WHY THIS EXISTS
---------------
On 2026-10-25 a person signed in to Onyx and got **502 Bad Gateway** — after
Authentik had authenticated them and handed the code back. The app was fine and
the gate was fine; the proxy was not. oauth2-proxy's session cookie carries the
whole ID token and the estate's group list, so the callback's *response headers*
are several kilobytes. nginx-proxy-manager's stock `proxy_buffer_size` is 4k, so
the proxy read a partial header block and answered 502 — a sign-in that fails
after it succeeded, with nothing in the app's log because the app was never
reached.

Nothing watched that. The gate answered `/oauth2/sign_in` perfectly (its headers
are small), `/` redirected correctly, and every check here was green while a real
person could not get in. That is what this check is for.

THE RULES
---------
1. **A gateway error on the callback path is a failure.** 502/503/504 means the
   proxy in front could not pass the gate's answer. A 500 is not: that is
   oauth2-proxy rejecting the deliberately bogus code this check sends, which is
   the correct answer to a request that is not a real sign-in.
2. **A gate whose response headers do not fit the proxy's buffer is a failure**
   before anyone hits it. The generated nginx server block must raise
   `proxy_buffer_size` (and `large_client_header_buffers`, which the same cookie
   needs on the request side) past the stock 4k. This is the rule that catches
   the *next* gated host added without them.
3. **A gate whose proxy configuration cannot be read is a failure to run**, never
   a pass — silence is exactly what 2026-10-25 looked like.

The gates are listed here rather than discovered from a scan: the estate has ~200
public names and only a handful are sign-in gates, and a table that fails the
build when it is out of step (`test_check_sso_callbacks.py` pins it) is worth
more than 200 probes a run.

    ./scripts/check-sso-callbacks.py
    ./scripts/check-sso-callbacks.py --prom /path/to/textfile
    ./scripts/check-sso-callbacks.py --npm-conf /path/to/proxy_host --json

Exit codes: 0 = every gate answers and fits, 1 = at least one does not, 2 = the
check could not run (no gate table, no proxy configuration).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, write_prom  # noqa: E402

CHECK = "sso_callback"

#: Where the edge keeps NPM's generated server blocks (one file per proxy host).
NPM_CONF = "/usr/src/proxy/data/nginx/proxy_host"

#: The stock nginx response-header buffer is 4k. A gate whose session cookie is
#: larger than this needs the block raised; 16k is the smallest value with room
#: for the ID token, the groups claim and the proxy's own headers.
MIN_BUFFER = 16 * 1024

#: Gateway errors — the proxy could not pass the gate's answer. 500 is the gate
#: itself rejecting the bogus code, which is the right answer to this probe.
GATEWAY_ERRORS = (502, 503, 504)

#: Every public name that puts a sign-in gate in front of it. `gate` decides
#: whether rule 2 applies: an oauth2-proxy session cookie is the large one.
GATES: list[dict] = [
    {"host": "onyx.innotel.us", "callback": "/oauth2/callback", "gate": "oauth2-proxy"},
    {"host": "re.genie.innotel.us", "callback": "/oauth2/callback", "gate": "oauth2-proxy"},
    {"host": "uc.genie.innotel.us", "callback": "/oauth2/callback", "gate": "oauth2-proxy"},
    {"host": "nas.innotel.us", "callback": "/oauth2/callback", "gate": "oauth2-proxy"},
    {"host": "proxy.innotel.us", "callback": "/oauth2/callback", "gate": "oauth2-proxy"},
    # The app's own SSO: its session cookie is an id, not an ID token, so rule 2
    # does not apply, but the callback still has to get through the proxy.
    {"host": "tix.ontrak.technology", "callback": "/api/sso/callback", "gate": "app"},
]


def _size(value: str) -> int | None:
    """`32k` / `4k` / `16384` as bytes; None when it is not a size at all."""
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(\d+)\s*([kmg]?)", text)
    if not match:
        return None
    number = int(match.group(1))
    return number * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match.group(2)]


def server_blocks(directory: str) -> dict[str, str]:
    """`server_name` -> the server block that answers it, from NPM's files."""
    blocks: dict[str, str] = {}
    root = Path(directory)
    if not root.is_dir():
        return blocks
    for path in sorted(root.glob("*.conf")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip().startswith("server_name "):
                for name in line.split(None, 1)[1].rstrip(";").split():
                    blocks.setdefault(name.lower(), text)
    return blocks


def buffer_of(block: str, directive: str) -> int | None:
    """The byte size a directive is set to in one server block."""
    if directive == "proxy_buffer_size":
        found = re.search(r"^\s*proxy_buffer_size\s+(\S+);", block, re.MULTILINE)
        return _size(found.group(1)) if found else None
    # `large_client_header_buffers 4 32k;` — the count, then the size.
    found = re.search(r"^\s*large_client_header_buffers\s+\d+\s+(\S+);", block,
                      re.MULTILINE)
    return _size(found.group(1)) if found else None


def audit(blocks: dict[str, str], probes: dict[str, int], gates: list[dict],
          minimum: int = MIN_BUFFER) -> Audit:
    """The two rules, over data the caller already has. Pure: the rules are the test."""
    result = Audit()
    for gate in gates:
        host = gate["host"]
        callback = gate["callback"]
        status = probes.get(host)
        if status is None:
            result.findings.append(Finding(
                "callback_unreachable", "fail",
                f"{host} did not answer at all — the gate is down, not merely unhappy"))
        elif status in GATEWAY_ERRORS:
            result.findings.append(Finding(
                "callback_gateway_error", "fail",
                f"{host}{callback} answered {status}: the proxy could not pass the gate's "
                "response. A session cookie larger than proxy_buffer_size is the usual "
                "cause — raise it on this host's proxy block"))
        elif status >= 500 and status not in (500,):
            result.findings.append(Finding(
                "callback_server_error", "warn",
                f"{host}{callback} answered {status}; expected 3xx/4xx for a bogus code"))

        if gate.get("gate") != "oauth2-proxy":
            continue
        block = blocks.get(host.lower())
        if block is None:
            result.findings.append(Finding(
                "no_proxy_block", "fail",
                f"{host} is a gated name with no proxy block here — the buffer rule "
                "cannot be checked, which is how 2026-10-25 stayed invisible"))
            continue
        for directive, why in (("proxy_buffer_size", "the gate's response headers"),
                               ("large_client_header_buffers", "the request side of the same cookie")):
            size = buffer_of(block, directive)
            if size is None:
                result.findings.append(Finding(
                    "stock_buffer", "fail",
                    f"{host}: {directive} is not set, so nginx uses its 4k default — "
                    f"{why} will not fit and the callback answers 502"))
            elif size < minimum:
                result.findings.append(Finding(
                    "small_buffer", "fail",
                    f"{host}: {directive} is {size} bytes, below the {minimum} a gated "
                    f"callback needs — {why} will not fit"))
    return result


def probe(host: str, callback: str, timeout: int = 15) -> int | None:
    """The callback's status for a request that is deliberately not a sign-in.

    No redirects are followed: the answer itself is the finding, and following it
    would land on the IdP and report *that* status instead.
    """
    url = f"https://{host}{callback}?code=atheniq-estate-check&state=atheniq-estate-check"
    request = urllib.request.Request(url, headers={"User-Agent": "atheniq-estate-check/1"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--npm-conf", default=NPM_CONF,
                        help=f"NPM's generated proxy_host directory (default: {NPM_CONF})")
    parser.add_argument("--host", action="append",
                        help="limit to one gate host (repeatable)")
    parser.add_argument("--min-buffer", type=int, default=MIN_BUFFER,
                        help=f"smallest acceptable header buffer in bytes (default: {MIN_BUFFER})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    gates = [gate for gate in GATES if not args.host or gate["host"] in args.host]

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran)
        except OSError as error:
            print(f"check-sso-callbacks: could not write {args.prom}: {error}", file=sys.stderr)

    blocks = server_blocks(args.npm_conf)
    if not blocks:
        print(f"check-sso-callbacks: no proxy configuration under {args.npm_conf} — "
              "cannot tell whether a gated callback fits its proxy", file=sys.stderr)
        emit(None, ran=False)
        return 2
    if not gates:
        print("check-sso-callbacks: no gate by that name", file=sys.stderr)
        emit(None, ran=False)
        return 2

    probes = {gate["host"]: probe(gate["host"], gate["callback"]) for gate in gates}
    result = audit(blocks, probes, gates, args.min_buffer)
    emit(result, ran=True)

    if args.json:
        print(json.dumps({
            "gates": [{"host": gate["host"], "callback": gate["callback"],
                       "status": probes.get(gate["host"])} for gate in gates],
            "findings": [{"code": f.code, "level": f.level, "message": f.message}
                         for f in result.findings],
            "failures": len(result.failures), "warnings": len(result.warnings),
        }, indent=2, sort_keys=True))
        return 0 if result.ok else 1

    for gate in gates:
        status = probes.get(gate["host"])
        print(f"  {gate['host']:<24} {gate['callback']:<20} "
              f"{'no answer' if status is None else status}")
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(f"check-sso-callbacks: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
