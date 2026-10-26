#!/usr/bin/env python3
"""Prove the mail server's *outbound* path is actually wired, not merely configured.

WHY THIS EXISTS
---------------
The estate's mail left through a chain that was easy to configure wrongly and had no
symptom until someone noticed mail was not arriving: a `relay` route to a GCP VM whose
egress port 25 was blocked, and a `smarthost` route that had never been created, so
messages queued against direct-to-MTA delivery to `Network is unreachable` — which looks
exactly like a working server with a quiet queue. Nothing watched the chain.

So this reads the live server and asserts the four things that make outbound delivery
work, each of which has failed here at least once:

  1. the outbound strategy routes non-local recipients at an authenticated relay
     (whichever route it names — the estate sends through Mailjet, and SPF publishes
     `include:spf.mailjet.com`);
  2. that relay, and the retry route when the strategy names one (the estate retries
     through the same Mailjet route, so it does not), exist and are reachable on the
     network;
  3. each sending domain has an **active** DKIM signature (a rotation stuck in
     `pending`/`retiring` sends mail unsigned — DMARC then rejects it at the far end);
  4. the queue is not backing up.

WHAT IT READS
-------------
The JMAP management API on the mail server (`--base`, `STALWART_URL`, or `127.0.0.1`)
as the recovery admin account, because DKIM and route configuration lives in RocksDB and
is not readable from the filesystem. The credential comes **only** from the environment
(`STALWART_USER` / `STALWART_PASSWORD`, set in `/etc/innotel/estate-checks.env` on the
edge) and is never defaulted in this file: until 2026-10-09 this check carried a literal
password in a mode-0755 file on the edge, and it was not in this repo at all, so nothing
could ever notice. The value now lives in exactly one place — that 0640 env file — and
the check is here.

THE RULES
---------
1. A route that is missing, points at no relay, or does not answer, no active DKIM key,
   or a non-draining queue is a **failure**, naming which.
2. A server that cannot be read at all is **exit 2**, never a quiet pass.

Usage:
    ./scripts/check-mail-relay.py
    ./scripts/check-mail-relay.py --base http://192.168.1.15:8080 --prom /path/to/textfile

`systemd/estate-checks.service` passes `--base` at the mail container's own address —
`mail` in the address table, `192.168.1.15` — which is the address every other client in
the estate dials and which travels with the container rather than with whichever host it
happens to be sitting on.

Exit codes: 0 = the chain is intact, 1 = at least one part is broken, 2 = the server
could not be read.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, write_prom  # noqa: E402

CHECK = "mail_relay"

BASE = os.environ.get("STALWART_URL", "http://127.0.0.1:8080")
USER = os.environ.get("STALWART_USER", "admin")
PASSWORD = os.environ.get("STALWART_PASSWORD", "")

#: How many queued recipients is "backing up". The estate sends a handful a day; a queue
#: deeper than this means delivery is failing and retrying rather than flowing.
QUEUE_FAIL = 10
QUEUE_WARN = 1


def jmap(method: str, params: dict | None = None) -> dict:
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    body = {
        "using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"],
        "methodCalls": [[method, params or {}, "c1"]],
    }
    req = urllib.request.Request(
        BASE + "/jmap",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        name, resp, _ = json.load(r)["methodResponses"][0]
    if name == "error":
        raise RuntimeError(f"{method}: {json.dumps(resp)}")
    return resp


def port_open(host: str, port: int, timeout: int = 8) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def route_name(expression: str) -> str:
    """The route a strategy expression names: `'mailjet'` -> `mailjet`.

    Stalwart stores each branch as an expression string with its quotes kept, and the
    checks here have to turn one into a key in `x:MtaRoute/get`. Anything that is not a
    bare route name (`'local'`, `false`) simply does not match a route, which is the
    finding, not a crash.
    """
    return expression.strip().strip("'\"")


def audit() -> Audit:
    out = Audit()

    routes = {r["name"]: r for r in jmap("x:MtaRoute/get", {})["list"]}

    # 1. the outbound strategy's primary branch -> an authenticated relay.
    #
    # It does not have to be the route called `smarthost`: the estate sends through
    # Mailjet (SPF publishes `include:spf.mailjet.com`) and retries through the same
    # route. What matters is that non-local recipients reach a Relay with an address,
    # through the branch the strategy picks first — a branch that resolves to `mx` sends
    # straight to the recipient's server, and port 25 does not leave here.
    st = jmap("x:MtaOutboundStrategy/get", {"ids": ["singleton"]})["list"]
    route = st[0].get("route", {}) if st else {}
    primary = route_name(str(route.get("else", "")))
    chosen = routes.get(primary)
    if not chosen:
        out.findings.append(Finding("route-wrong", "fail",
                                    f"non-local recipients route to {primary!r}, which is not a configured route"))
    else:
        host, port = chosen.get("address"), chosen.get("port")
        if not host or not port:
            out.findings.append(Finding("route-direct", "fail",
                                        f"non-local recipients route through {primary!r}, which has no smarthost "
                                        "address — mail would go straight to the recipient's MX, and port 25 "
                                        "does not leave this estate"))
        else:
            out.findings.append(Finding("route-ok", "note",
                                        f"non-local recipients route through {primary} ({host}:{port})"))
            if port_open(str(host), int(port)):
                out.findings.append(Finding("route-reachable", "note",
                                            f"{host}:{port} accepts a connection"))
            else:
                out.findings.append(Finding("route-unreachable", "fail",
                                            f"{host}:{port} refused or timed out"))

    # 2. the retry branch (`retry_num > 0` in the strategy), when there is one, has a
    # route that answers. Skipped when it names the same route as the primary one — the
    # estate's shape, where a retry is retried through Mailjet — because that host was
    # just dialled.
    branches = list((route.get("match") or {}).values())
    retry = next((b for b in branches if "retry_num" in str(b.get("if", ""))), None)
    fallback = route_name(str(retry.get("then", ""))) if retry else ""
    sm = routes.get(fallback) if fallback else None
    if not fallback or fallback == primary:
        pass
    elif not sm:
        out.findings.append(Finding("retry-missing", "fail",
                                    f"no {fallback!r} route exists; a retry has nowhere to go"))
    else:
        host, port = sm.get("address"), sm.get("port")
        out.findings.append(Finding("retry-present", "note",
                                    f"retry route {fallback} is {host}:{port}"))
        if not host or not port:
            out.findings.append(Finding("retry-incomplete", "fail",
                                        f"retry route {fallback!r} has no address/port"))
        elif port_open(str(host), int(port)):
            out.findings.append(Finding("retry-reachable", "note",
                                        f"{host}:{port} accepts a connection"))
        else:
            out.findings.append(Finding("retry-unreachable", "fail",
                                        f"{host}:{port} refused or timed out"))

    # 3. every sending domain has an ACTIVE DKIM signature
    domains = {d["id"]: d["name"] for d in jmap("x:Domain/get", {})["list"]}
    active: dict[str, int] = {}
    for sig in jmap("x:DkimSignature/get", {})["list"]:
        if sig.get("stage") == "active":
            active[sig["domainId"]] = active.get(sig["domainId"], 0) + 1
    for did, name in domains.items():
        n = active.get(did, 0)
        if n:
            out.findings.append(Finding("dkim-active", "note",
                                        f"{name} has {n} active DKIM signature(s)"))
        else:
            out.findings.append(Finding("dkim-inactive", "fail",
                                        f"{name} has no ACTIVE DKIM signature; mail is sent unsigned and DMARC will reject it"))

    # 4. the queue is draining
    queued = jmap("x:QueuedMessage/get", {})["list"]
    n = sum(len(m.get("recipients") or {}) for m in queued)
    if n >= QUEUE_FAIL:
        out.findings.append(Finding("queue-backlog", "fail",
                                    f"{n} queued recipient(s) — outbound delivery is failing"))
    elif n > QUEUE_WARN:
        out.findings.append(Finding("queue-some", "warn", f"{n} queued recipient(s)"))
    else:
        out.findings.append(Finding("queue-empty", "note", "queue is empty"))

    return out


def main(argv: list[str] | None = None) -> int:
    global BASE
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--base", help=f"Stalwart JMAP base URL (default {BASE})")
    args = parser.parse_args(argv)
    if args.base:
        BASE = args.base

    try:
        result = audit()
    except Exception as error:  # noqa: BLE001 — an unreadable server is never a pass
        print(f"check-mail-relay: server could not be read: {error}", file=sys.stderr)
        if args.prom:
            write_prom(args.prom, CHECK, None, ran_ok=False)
        return 2

    # Failures first, like every other check here: the thing that broke is what a reader
    # at 3am needs at the top of the output.
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(f"\ncheck-mail-relay: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    if args.prom:
        write_prom(args.prom, CHECK, result, ran_ok=True)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
