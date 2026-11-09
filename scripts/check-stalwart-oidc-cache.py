#!/usr/bin/env python3
"""Watch the local copy of Stalwart's OIDC discovery document.

WHY THIS EXISTS
---------------
Stalwart's only directory speaks OIDC, and the server reads that directory's discovery
document once, at start-up. A failed fetch is cached for the life of the process, so every
bearer token answers 500 until somebody restarts it — measured 2026-11-02 and again
2026-11-06, and the page after a *correct* Authentik login just says "Authentication
Failed".

Since 2026-11-09 the document is served from a copy inside the mail container
(`stalwart-oidc-discovery-proxy.service`), and Stalwart's fetch is steered to that copy by
a DNAT (`stalwart-oidc-redirect.service`) while the server alone gets a hosts file that
points the issuer's name at loopback. That removed the dependency on the identity provider
being reachable at the moment Stalwart starts. It also moved the failure somewhere nothing
watched: the copy refreshes every fifteen minutes by going *upstream* and keeps its last
good bytes when a refresh fails, so all of these leave a mail server that starts happily
and signs nobody in, with nothing said anywhere:

* the provider has been unreachable for hours, and the copy is ageing past the point where
  its keys are still the ones the provider publishes;
* the proxy died, and the DNAT now points at a closed port;
* the redirect unit is not active, so the next start fetches upstream again — fine today,
  and the 2026-11-06 fault again on the next blip;
* a permission regression hides the trust bundle from the `stalwart` user (which is what
  panicked the server on the first install attempt: `No CA certificates were loaded from
  the system`), or the copy from the proxy user that has to read it.

So the state is published beside every other check, where a failure becomes an alert and a
dashboard row instead of a line in a journal nobody opens.

WHAT IT READS
-------------
The container is on `i2` and runs sshd, but not with the key this check holds; the path
that already works for `mail` is the one `check-alert-delivery.py` takes — ssh to the incus
host, then `incus exec mail`. Inside, it asks four things that are facts rather than
inferences: the two unit states, the cache's mtime and bytes (parsed here, so a truncated
or foreign document is caught), and whether each user can read the file it owns — the
`stalwart` user the trust bundle, the `oidcproxy` user the copy. They are deliberately not
the same file: `/etc/stalwart-oidc/cache` is 0750 to the proxy, because nothing else needs
the copy.

THE RULES
---------
1. A wrong answer is a **failure** naming which fact and what it costs. An ageing copy
   (two missed refreshes) is a **warning**, not a failure: the old document is still the
   right one, and it is still being served.
2. An unreachable host or container is **exit 2** — "could not read it" is never a pass.
3. A clean run states the copy's age as a **note**, so "ok" says how old the answer is.

READ-ONLY
---------
Nothing is restarted, refreshed or written. The refresh itself belongs to the proxy.

Usage:
    ./scripts/check-stalwart-oidc-cache.py
    ./scripts/check-stalwart-oidc-cache.py --prom /path/to/textfile
    ./scripts/check-stalwart-oidc-cache.py --host root@192.168.1.52 --container mail

Exit codes: 0 = the copy is current and servable, 1 = it is not, 2 = could not run.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, ssh_prefix, write_prom  # noqa: E402

CHECK = "stalwart_oidc_cache"

#: The host the mail container runs on (`i2`, per the container address table) and the
#: container itself. The unit file that starts this check is the authority for both, not
#: this default.
HOST = "root@192.168.1.52"
CONTAINER = "mail"

#: Where the setup script puts the three pieces the server depends on.
CACHE_PATH = "/etc/stalwart-oidc/cache/discovery.json"
BUNDLE_PATH = "/etc/stalwart-oidc/ca-bundle.pem"

#: The document must be for this issuer: Stalwart rejects one that names another exactly
#: as it rejects a failed fetch, so a wrong document is not a degraded state.
ISSUER = "https://auth.cerulean.innotel.us/application/o/stalwart-webui/"

#: The proxy refreshes every fifteen minutes. Two missed refreshes are worth a look; four
#: mean the copy is an hour old and the provider's keys may already have rotated.
REFRESH_SECONDS = 900
AGING_SECONDS = 2 * REFRESH_SECONDS
STALE_SECONDS = 4 * REFRESH_SECONDS

#: Two hops and a small file; a minute is generous and a hung ssh must not hold the unit.
TIMEOUT_SECONDS = 120

#: ssh's own failure code. Not a verdict about the copy, so it must read as exit 2.
SSH_FAILURE = 255

#: Runs inside the container. The document is printed between markers rather than parsed
#: on the far side, so the parsing — and the rules about it — live here, where the tests
#: can reach them.
REMOTE = f"""
set -u
printf 'proxy:%s\\n' "$(systemctl is-active stalwart-oidc-proxy 2>/dev/null || echo unknown)"
printf 'redirect:%s\\n' "$(systemctl is-active stalwart-oidc-redirect 2>/dev/null || echo unknown)"
if [ -r {CACHE_PATH} ]; then
  printf 'mtime:%s\\n' "$(stat -c %Y {CACHE_PATH} 2>/dev/null || echo unknown)"
else
  printf 'mtime:missing\\n'
fi
if su -s /bin/sh stalwart -c 'test -r {BUNDLE_PATH}' 2>/dev/null; then
  printf 'bundle_readable:yes\n'
else
  printf 'bundle_readable:no\n'
fi
if su -s /bin/sh oidcproxy -c 'test -r {CACHE_PATH}' 2>/dev/null; then
  printf 'cache_readable:yes\n'
else
  printf 'cache_readable:no\n'
fi
printf 'document_begin\\n'
cat {CACHE_PATH} 2>/dev/null || true
printf '\\ndocument_end\\n'
printf 'now:%s\\n' "$(date +%s)"
"""


@dataclass
class Facts:
    """What the container said, before any judgement about it."""

    proxy: str = "unknown"
    redirect: str = "unknown"
    mtime: int | None = None
    readable: bool | None = None
    cache_readable: bool | None = None
    issuer: str | None = None
    document_ok: bool = False
    document_error: str = ""
    now: int = 0
    detail: str = ""

    @property
    def age_seconds(self) -> int | None:
        """How old the copy is, or None when there is no mtime to subtract from."""
        if self.mtime is None or not self.now:
            return None
        return max(0, self.now - self.mtime)


@dataclass
class Result:
    """The container's exit status and output."""

    code: int
    stdout: str = ""
    stderr: str = ""


def probe(host: str = HOST, container: str = CONTAINER, timeout: int = TIMEOUT_SECONDS) -> Result:
    """Read the container's state over the estate's ssh path, then `incus exec`.

    `check=False` because a non-zero exit is the finding or the "could not run" case, not
    an exception — only a command that will not start or that times out is exceptional.
    """
    remote = f"incus exec {shlex.quote(container)} -- sh -c {shlex.quote(REMOTE)}"
    completed = subprocess.run(
        [*ssh_prefix(), "-o", "ConnectTimeout=25", host, remote],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def parse(output: str) -> Facts:
    """Turn the container's report into facts. Pure, so the rules below are testable."""
    facts = Facts()
    body: list[str] = []
    in_document = False

    for line in output.splitlines():
        if line.strip() == "document_begin":
            in_document = True
            continue
        if line.strip() == "document_end":
            in_document = False
            continue
        if in_document:
            body.append(line)
            continue

        key, _, value = line.partition(":")
        value = value.strip()
        if key == "proxy":
            facts.proxy = value or "unknown"
        elif key == "redirect":
            facts.redirect = value or "unknown"
        elif key == "mtime":
            if value.isdigit():
                facts.mtime = int(value)
            elif value == "missing":
                facts.mtime = None
                facts.detail = "the copy is not there"
        elif key == "bundle_readable":
            facts.readable = value == "yes"
        elif key == "cache_readable":
            facts.cache_readable = value == "yes"
        elif key == "now":
            if value.isdigit():
                facts.now = int(value)

    text = "\n".join(body).strip()
    if text:
        try:
            document = json.loads(text)
        except ValueError as error:
            facts.document_error = str(error)
        else:
            facts.document_ok = isinstance(document, dict)
            if facts.document_ok:
                issuer = document.get("issuer")
                facts.issuer = issuer if isinstance(issuer, str) else None
                if not facts.issuer:
                    facts.document_error = "the document names no issuer"
    return facts


def audit(facts: Facts) -> Audit:
    """Read the facts as findings: what is wrong, and what it costs."""
    out = Audit()

    if facts.proxy != "active":
        out.findings.append(
            Finding(
                "proxy_inactive",
                "fail",
                f"stalwart-oidc-proxy is {facts.proxy}: the copy is not being served or "
                "refreshed, so the next start fetches upstream again",
            )
        )
    if facts.redirect != "active":
        out.findings.append(
            Finding(
                "redirect_inactive",
                "fail",
                "stalwart-oidc-redirect is "
                f"{facts.redirect}: the DNAT that steers Stalwart's fetch is not installed, "
                "so a start with the provider unreachable caches a failure again",
            )
        )

    if facts.mtime is None:
        out.findings.append(
            Finding(
                "cache_missing",
                "fail",
                "there is no local discovery document, so Stalwart's start depends on the "
                "provider answering",
            )
        )
    elif not facts.document_ok:
        detail = facts.document_error or "it is not a JSON object"
        out.findings.append(
            Finding("cache_unreadable", "fail", f"the local discovery document does not parse: {detail}")
        )
    elif facts.issuer != ISSUER:
        named = facts.issuer or "no issuer"
        out.findings.append(
            Finding(
                "issuer_mismatch",
                "fail",
                f"the local document names {named}; Stalwart rejects a document whose issuer "
                f"is not {ISSUER}",
            )
        )

    if facts.readable is False:
        out.findings.append(
            Finding(
                "bundle_unreadable",
                "fail",
                "the stalwart user cannot read the trust bundle — the server panics at start "
                "with 'No CA certificates were loaded from the system'",
            )
        )
    if facts.cache_readable is False:
        out.findings.append(
            Finding(
                "cache_unreadable_by_proxy",
                "fail",
                "the oidcproxy user cannot read the copy it serves, so every discovery fetch "
                "fails even though the units are active",
            )
        )

    age = facts.age_seconds
    if age is not None:
        if age > STALE_SECONDS:
            out.findings.append(
                Finding(
                    "cache_stale",
                    "fail",
                    f"the copy is {age} s old and has not refreshed for "
                    f"{age // REFRESH_SECONDS} cycles: the provider is not answering, and the "
                    "document's keys may already have rotated",
                )
            )
        elif age > AGING_SECONDS:
            out.findings.append(
                Finding(
                    "cache_aging",
                    "warn",
                    f"the copy is {age} s old, past two {REFRESH_SECONDS} s refresh cycles: "
                    "refreshes are failing but the old document is still being served",
                )
            )
        else:
            out.findings.append(
                Finding("cache_current", "note", f"the copy is {age} s old and refreshes every {REFRESH_SECONDS} s")
            )
    return out


def metric_age(facts: Facts) -> str | None:
    """The copy's age as its own series, so a rising number is visible before it fails."""
    age = facts.age_seconds
    if age is None:
        return None
    return (
        "# HELP innotel_estate_check_stalwart_oidc_cache_age_seconds Age of the local "
        "discovery document the mail server reads at start\n"
        "# TYPE innotel_estate_check_stalwart_oidc_cache_age_seconds gauge\n"
        f'innotel_estate_check_stalwart_oidc_cache_age_seconds{{check="{CHECK}"}} {age}\n'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--host", metavar="TARGET", default=HOST, help="ssh target of the host running the container")
    parser.add_argument("--container", metavar="NAME", default=CONTAINER, help="container holding the mail server")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool, facts: Facts | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=metric_age(facts) if facts else None)
            except OSError as error:
                print(f"check-stalwart-oidc-cache: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-stalwart-oidc-cache: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        result = probe(args.host, args.container)
    except Exception as error:  # noqa: BLE001 — any failure here is "could not run"
        print(f"check-stalwart-oidc-cache: could not reach {args.host}: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    if result.code != 0:
        detail = " ".join(result.stderr.split())[-300:] or "no output"
        why = "could not reach" if result.code == SSH_FAILURE else f"the read exited {result.code}"
        print(f"check-stalwart-oidc-cache: {why} {args.host}: {detail}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    facts = parse(result.stdout)
    if not facts.now:
        # No `now` means the remote script did not run to the end, so the rest of the
        # output is not a verdict about the copy.
        print(f"check-stalwart-oidc-cache: {args.container} did not finish reporting", file=sys.stderr)
        emit(None, ran=False)
        return 2

    report = audit(facts)
    emit(report, ran=True, facts=facts)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in report.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(f"\ncheck-stalwart-oidc-cache: {len(report.failures)} failure(s)")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
