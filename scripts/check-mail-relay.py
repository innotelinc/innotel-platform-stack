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

  1. the outbound strategy routes non-local recipients at the smarthost;
  2. the smarthost route exists and is reachable on the network;
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
1. A missing route, an unreachable smarthost, no active DKIM key, or a non-draining
   queue is a **failure**, naming which.
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


def audit() -> Audit:
    out = Audit()

    # 1. outbound strategy -> smarthost for non-local recipients
    st = jmap("x:MtaOutboundStrategy/get", {"ids": ["singleton"]})["list"]
    route = st[0].get("route", {}) if st else {}
    else_branch = str(route.get("else", ""))
    if "smarthost" in else_branch:
        out.findings.append(Finding("route-ok", "note",
                                    f"non-local recipients route to the smarthost ({else_branch})"))
    else:
        out.findings.append(Finding("route-wrong", "fail",
                                    f"non-local recipients do not route to a smarthost (else = {else_branch!r})"))

    # 2. the smarthost route exists and is reachable
    routes = {r["name"]: r for r in jmap("x:MtaRoute/get", {})["list"]}
    sm = routes.get("smarthost")
    if not sm:
        out.findings.append(Finding("smarthost-missing", "fail",
                                    "no 'smarthost' route exists; outbound mail cannot leave"))
    else:
        host, port = sm.get("address"), sm.get("port")
        out.findings.append(Finding("smarthost-present", "note",
                                    f"smarthost route is {host}:{port}"))
        if not host or not port:
            out.findings.append(Finding("smarthost-incomplete", "fail",
                                        "smarthost route has no address/port"))
        elif port_open(str(host), int(port)):
            out.findings.append(Finding("smarthost-reachable", "note",
                                        f"{host}:{port} accepts a connection"))
        else:
            out.findings.append(Finding("smarthost-unreachable", "fail",
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
