#!/usr/bin/env python3
"""Prove the estate's mail domains are *authenticated in the zone the public reads*.

WHY THIS EXISTS
---------------
The estate sends mail as at least two domains (`innotel.us` and, since 2026-10-03,
`signara.innotel.us`) and every one of those senders depends on three records a
receiver checks before it trusts the mail: an **SPF** policy on the domain, a
**DMARC** policy at `_dmarc.<domain>`, and a **DKIM** public key under
`<selector>._domainkey.<domain>`. Losing any one of them does not stop the mail
leaving — it arrives, or is quietly filed as spam, or is rejected at the far end.
That is the same shape as the Alertmanager that looked healthy while delivering
nowhere, and it went un-watched the same way.

It matters here more than in most estates because the records are **not** published
by the mail server. Stalwart generates the keys but its automatic DNS publishing is
refused (see `docs/container-placement.md` §Open items: its `DnsServer` is a TSIG
update the public zone does not accept), so the records were published by hand and a
key rotation will not publish itself. Nothing told anyone when a record was missing;
this does.

WHAT IT READS
-------------
`dig`, against the **authoritative** server the public sees — Technitium on the edge
(`192.168.1.71`), the box `ns1/ns2.innotel.us` answer for — not the LAN resolver and
not the older BIND copy. The records are read back, not assumed: a query that returns
no answer is a finding, and a query that cannot reach the server at all is exit 2
(never a quiet pass), the same rule every check here follows.

THE RULES
---------
1. **SPF** must resolve on each domain and begin `v=spf1`.
2. **DMARC** must resolve at `_dmarc.<domain>` and begin `v=DMARC1`.
3. **DKIM** must resolve for each selector in `DOMAINS` and carry a `v=DKIM1` tag with
   a non-empty `p=` key.
4. A server that answers `SERVFAIL`/`REFUSED`/nothing is **exit 2**, naming it.

The selector table is deliberate and versioned: a rotation adds a selector *here* as
well as to the zone, and that edit is the record of what the public zone must serve —
which is the only place the estate can catch Stalwart's publish gap until its TSIG
update is accepted.

Usage:
    ./scripts/check-mail-auth.py
    ./scripts/check-mail-auth.py --server 192.168.1.71 --prom /path/to/textfile
    ./scripts/check-mail-auth.py --domains innotel.us,signara.innotel.us

Exit codes: 0 = every record resolved and is well-formed, 1 = at least one did not,
2 = the zone could not be read at all.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, write_prom  # noqa: E402

CHECK = "mail_auth"

#: The server the public actually sees: Technitium on the edge, bound on `i4` and
#: answering for `ns1/ns2.innotel.us`. Explicitly not the resolver (which may serve a
#: cached, older copy) and not the BIND on `www` (a second copy of the zone the LAN
#: reads, authoritative only for the nameservers themselves).
AUTHORITATIVE = "192.168.1.71"

#: What each mail domain must serve. The DKIM selectors are the ones Stalwart generated
#: and that were published by hand; a rotation adds its new selector to the zone **and**
#: to this table, which is the point at which a missing record becomes visible.
DOMAINS: dict[str, dict[str, object]] = {
    "innotel.us": {
        "dkim": ["v1-ed25519-20260901", "v1-rsa-20260901"],
    },
    "signara.innotel.us": {
        "dkim": ["v1-ed25519-20261003", "v1-rsa-20261003"],
    },
}

#: A TXT answer is one or more quoted character-strings that decode to one value; a
#: long RSA key is split across two `"…" "…"` chunks on one line (measured, 2026-10-03),
#: so the chunks are concatenated rather than compared one by one.
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_STATUS = re.compile(r"status:\s*([A-Z]+)")

#: A dig that could not reach its server has no `status:` line and says so in prose.
_UNREACHABLE = ("no servers could be reached", "connection timed out", "communications error")


class DnsUnreadable(RuntimeError):
    """The zone could not be read — the server did not answer, or refused the query."""

    def __init__(self, server: str, detail: str) -> None:
        super().__init__(f"{server} could not be read: {detail.strip() or 'no answer'}")
        self.server = server
        self.detail = detail


def unquote(text: str) -> str:
    """The value of a quoted dig character-string, with dig's own escapes undone."""
    return text.replace('\\"', '"').replace("\\\\", "\\")


def parse_answer(output: str) -> tuple[str, list[str]]:
    """`(status, [record text, …])` from a `dig +noall +answer +comments` run.

    `status` is `"UNREADABLE"` when dig never reached a server, so the caller can tell
    "no such record" (a finding) from "the zone is down" (exit 2).
    """
    for phrase in _UNREACHABLE:
        if phrase in output:
            return "UNREADABLE", []
    match = _STATUS.search(output)
    if match is None:
        return "UNREADABLE", []
    records: list[str] = []
    in_answer = False
    for line in output.splitlines():
        if line.startswith(";; ANSWER SECTION:"):
            in_answer = True
            continue
        if not in_answer:
            continue
        if line.startswith(";;") or not line.strip():
            in_answer = False
            continue
        text = "".join(unquote(chunk.group(1)) for chunk in _QUOTED.finditer(line))
        if text:
            records.append(text)
    return match.group(1), records


def dig_command(name: str, server: str) -> list[str]:
    """A single AXFR-free TXT query against one server, with a short, bounded timeout."""
    return ["dig", "+time=3", "+tries=1", "+noall", "+answer", "+comments", "TXT", name, f"@{server}"]


def query_txt(name: str, server: str, runner=subprocess.run) -> tuple[str, list[str]]:
    """Read `name`'s TXT records from `server`. Raises `DnsUnreadable` on a refused query."""
    proc = runner(dig_command(name, server), capture_output=True, text=True, timeout=15)
    status, records = parse_answer(proc.stdout)
    if status in {"UNREADABLE", "SERVFAIL", "REFUSED", "NOTIMP"}:
        raise DnsUnreadable(server, proc.stdout or proc.stderr)
    return status, records


def _spf(texts: list[str]) -> str | None:
    return next((t for t in texts if t.lower().startswith("v=spf1")), None)


def _dmarc(texts: list[str]) -> str | None:
    return next((t for t in texts if t.lower().startswith("v=dmarc1")), None)


def _dkim(texts: list[str]) -> str | None:
    return next((t for t in texts if "v=dkim1" in t.lower() and "p=" in t.lower()), None)


def evaluate(domain: str, selector: str, lookup) -> Finding:
    """One DKIM selector's finding. `lookup(name)` returns `(status, texts)`."""
    name = f"{selector}._domainkey.{domain}"
    _, texts = lookup(name)
    if _dkim(texts):
        return Finding("dkim-ok", "note", f"{name} serves a DKIM1 key")
    return Finding("dkim-missing", "fail", f"{name} has no DKIM1 record — mail signed with selector {selector} will not verify")


def audit(records: list[tuple[str, str, str | None]]) -> Audit:
    """Read back the queries as findings.

    `records` is `(domain, kind, text)` triples: `kind` is `"spf"` or `"dmarc"` and
    `text` the matching record, or None when nothing matched. Pure, so what counts as
    authenticated is the thing under test.
    """
    out = Audit()
    for domain, kind, text in records:
        if text is None:
            where = domain if kind == "spf" else f"_dmarc.{domain}"
            out.findings.append(Finding(f"{kind}-missing", "fail", f"{domain} has no {kind.upper()} record at {where}"))
        else:
            out.findings.append(Finding(f"{kind}-ok", "note", f"{domain} serves {text[:32]}…"))
    return out


def mail_series(ok_records: int, total_records: int) -> str:
    lines = [
        f"# HELP {METRIC}_mail_auth_records_ok Auth records that resolved and are well-formed",
        f"# TYPE {METRIC}_mail_auth_records_ok gauge",
        f'{METRIC}_mail_auth_records_ok{{check="{CHECK}"}} {ok_records}',
        f"# HELP {METRIC}_mail_auth_records_expected Auth records the zone must serve",
        f"# TYPE {METRIC}_mail_auth_records_expected gauge",
        f'{METRIC}_mail_auth_records_expected{{check="{CHECK}"}} {total_records}',
    ]
    return "\n".join(lines)


def collect(domains: dict[str, dict[str, object]], server: str, lookup=None) -> tuple[Audit, int, int]:
    """Run every query and fold the answers into one `Audit` plus its ok/total counts.

    `lookup` is resolved at call time (not bound as a default) so a test can replace
    `query_txt` without the network.
    """
    lookup = lookup or query_txt
    records: list[tuple[str, str, str | None]] = []
    detail = Audit()
    for domain, spec in domains.items():
        _, spf_texts = lookup(domain, server)
        records.append((domain, "spf", _spf(spf_texts)))
        _, dmarc_texts = lookup(f"_dmarc.{domain}", server)
        records.append((domain, "dmarc", _dmarc(dmarc_texts)))
        for selector in spec.get("dkim", []):  # type: ignore[union-attr]
            detail.findings.append(evaluate(domain, selector, lambda name: lookup(name, server)))

    result = audit(records)
    result.findings.extend(detail.findings)
    ok = len([f for f in result.findings if f.level == "note"])
    return result, ok, len(result.findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default=AUTHORITATIVE, help=f"authoritative server to read (default {AUTHORITATIVE})")
    parser.add_argument("--domains", help="comma-separated domains to read (default: the estate's mail domains)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    domains = dict(DOMAINS)
    if args.domains:
        wanted = {d.strip() for d in args.domains.split(",") if d.strip()}
        domains = {d: DOMAINS.get(d, {"dkim": []}) for d in wanted}

    def emit(result: Audit | None, ran: bool, ok: int = 0, total: int = 0) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=mail_series(ok, total))
            except OSError as error:
                print(f"check-mail-auth: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("dig") is None:
        print("check-mail-auth: no dig on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        result, ok, total = collect(domains, args.server)
    except DnsUnreadable as error:
        print(f"check-mail-auth: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2
    except subprocess.SubprocessError as error:
        print(f"check-mail-auth: dig failed: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    emit(result, ran=True, ok=ok, total=total)
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(f"\ncheck-mail-auth: {len(result.failures)} failure(s), {ok}/{total} records resolved")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
