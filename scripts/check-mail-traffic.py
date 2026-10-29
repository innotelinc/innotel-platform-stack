#!/usr/bin/env python3
"""Turn the mail server's own log into the numbers a mail dashboard is made of.

WHY THIS EXISTS
---------------
The estate can say whether the mail *chain* is wired (`check-mail-relay`: the
smarthost route, an active DKIM key, a draining queue) and whether its DNS records
resolve (`check-mail-auth`), but nothing measured **traffic**. So the questions
anyone actually asks of a mail server — how much went out, did any of it bounce,
who is authenticating, and is anybody hammering the front door — had no answer
here; the mail panel was a 24-hour-old memory of a textfile built by hand. Traffic
is what a dashboard is made of, and it has to come from somewhere scheduled.

Stalwart v0.16 on this server exposes no Prometheus endpoint, and its
`x:Metrics/get` answers for account storage only, so the log **is** the
measurement. It is structured and line-oriented —

    … INFO Delivery completed (delivery.completed) queueName = "local", from = …, to = […]

— and every number below is a count of those events over a window. Each run
recomputes the window from the files, so the values are gauges rather than
monotonic counters: a Prometheus restart, a textfile deleted between runs, or the
container moving host cannot silently reset or drift them.

WHAT IT READS
-------------
`/var/log/stalwart/stalwart.*` **inside the mail container**, over the same key-only
ssh every other check uses. The container is found by asking each host what it runs
(`incus list`) rather than by naming a host, because the mail server has moved four
times and a check that names one host is a check that goes red the day it moves.

Read-only: one `cat` of the newest log files, filtered on the host, and counted
here.

THE RULES
---------
1. Delivery attempts with nothing delivered is a **failure** — mail accepted and
   never delivered is the quiet failure this check exists for. A genuinely quiet
   estate makes no attempts at all and reads as a warning, not as a broken queue.
2. A permanent failure (a bounce) is a **warning**: a real message that did not
   arrive, and one that no other check can see.
3. A host or container that cannot be read is **exit 2**, never a quiet pass.

Exit codes: 0 = the window is clean, 1 = at least one finding, 2 = the mail
container or its log could not be read.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import (  # noqa: E402
    Audit,
    Finding,
    HostUnreadable,
    read_host,
    ssh,
    write_prom,
)

CHECK = "mail_traffic"

#: Where each estate host is reachable — the same table the other checks read.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: The container that runs Stalwart. Named rather than discovered by port, so a
#: second listener elsewhere on the estate cannot be mistaken for the mail server.
CONTAINER = "mail"

#: How far back a run measures. A day is the window the dashboard states out loud
#: ("delivered, last 24 h") and it survives one quiet night without reading as an
#: outage.
WINDOW_HOURS = 24

#: How many log files to read. Two covers a window that straddles midnight; the
#: timestamp filter is what narrows the window, so a third file is needed only
#: when the window is widened.
LOG_FILES = 2

#: Event families worth reading. Filtering on the host keeps the transfer small:
#: `task-manager`, `telemetry` and `store.blob-store-purged` are most of a day's
#: volume and none of the traffic.
EVENTS = r"\((delivery|queue|message-ingest|smtp|auth|security|iprev|mail-auth|dmarc)\.[a-z0-9.-]+\)"

#: Stalwart writes its log with ANSI colour (it is meant to be read in a terminal),
#: so every line is stripped of escape sequences before it is parsed. Without this
#: the timestamp does not start at offset 0 and *nothing* matches — measured
#: 2026-10-29, when a 2 826-line sample parsed as zero events.
ANSI = re.compile("\x1b\\[[0-9;]*[A-Za-z]")

#: `… INFO Delivery completed (delivery.completed) queueName = "local", …`
LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(?P<level>[A-Z]+)\s+.*?"
    r"\((?P<event>[a-z0-9][a-z0-9.-]*)\)\s*(?P<fields>.*)$"
)

#: How each delivery failure is named in the log, and how it is reported. A
#: permanent failure is a bounce; the rest are retries that may still succeed,
#: which is why they are separate series and only the first is a warning.
FAILURES = {
    "delivery.dsn-perm-fail": "permanent",
    "delivery.dsn-temp-fail": "temporary",
    "delivery.connect-error": "connect",
    "delivery.start-tls-unavailable": "tls",
    "delivery.null-mx": "null-mx",
}

#: How a message entered the queue. Stalwart does not label a queue event with the
#: queue it will be delivered on (that is decided later, per recipient), so the
#: listener is what can be said about it here: authenticated submission, or mail
#: that arrived from outside.
QUEUE_ENTRY = {
    "queue.authenticated-message-queued": "submission",
    "queue.message-queued": "inbound",
}

#: How many domains a sender/recipient panel ranks before the tail is summed.
TOP_DOMAINS = 10


def _field(fields: str, key: str) -> str:
    """One `key = "value"` out of a log line's tail, or "" when it is absent."""
    match = re.search(rf'(?<![\w.]){re.escape(key)} = "([^"]*)"', fields)
    return match.group(1) if match else ""


def _first_recipient(fields: str) -> str:
    """The first address of a `to = [...]` field (`to = "…"` for a single one)."""
    match = (re.search(r'(?<![\w.])to = \["([^"]*)"', fields)
             or re.search(r'(?<![\w.])to = "([^"]*)"', fields))
    return match.group(1) if match else ""


def _domain(address: str) -> str:
    """The domain of an address; a bounce sender (`<>`) has none."""
    _, _, domain = address.rpartition("@")
    return domain.lower() if "@" in address else ""


@dataclass
class Traffic:
    """What the log said in the window. Every field is a count, not a rate."""

    lines: int = 0
    delivered: Counter = field(default_factory=Counter)      # queue
    failed: Counter = field(default_factory=Counter)         # reason
    queued: Counter = field(default_factory=Counter)         # queue
    ingested: Counter = field(default_factory=Counter)       # ham | spam | other
    auth: Counter = field(default_factory=Counter)           # (listener, success|failure)
    senders: Counter = field(default_factory=Counter)        # domain
    recipients: Counter = field(default_factory=Counter)     # domain
    blocked_ips: int = 0
    scan_bans: int = 0
    mailbox_missing: int = 0
    relay_refused: int = 0
    rate_limited: int = 0
    last_delivery: float | None = None
    last_auth_failure: float | None = None

    @property
    def attempts(self) -> int:
        """Deliveries that completed or failed — the denominator for "did mail land"."""
        return sum(self.delivered.values()) + sum(self.failed.values())

    @property
    def auth_failures(self) -> int:
        return sum(n for (_, result), n in self.auth.items() if result == "failure")


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def measure(lines: list[str]) -> Traffic:
    """Count the window's events. Pure: the same lines always give the same numbers."""
    out = Traffic()
    for raw in lines:
        match = LINE.match(ANSI.sub("", raw).strip())
        if not match:
            continue
        event = match.group("event")
        fields = match.group("fields")
        out.lines += 1
        when = _epoch(match.group("ts"))

        if event == "delivery.completed":
            out.delivered[_field(fields, "queueName") or "unknown"] += 1
            out.last_delivery = max(out.last_delivery or 0, when)
        elif event in FAILURES:
            out.failed[FAILURES[event]] += 1
        elif event in QUEUE_ENTRY:
            out.queued[QUEUE_ENTRY[event]] += 1
            sender = _domain(_field(fields, "from"))
            if sender:
                out.senders[sender] += 1
            recipient = _domain(_first_recipient(fields))
            if recipient:
                out.recipients[recipient] += 1
        elif event.startswith("message-ingest."):
            kind = event.split(".", 1)[1]
            out.ingested["spam" if kind == "spam" else
                         "ham" if kind == "ham" else "other"] += 1
            sender = _domain(_field(fields, "from"))
            if sender:
                out.senders[sender] += 1
            recipient = _domain(_first_recipient(fields))
            if recipient:
                out.recipients[recipient] += 1
        elif event in ("auth.success", "auth.error"):
            listener = _field(fields, "listenerId") or "unknown"
            result = "success" if event == "auth.success" else "failure"
            out.auth[(listener, result)] += 1
            if result == "failure":
                out.last_auth_failure = max(out.last_auth_failure or 0, when)
        elif event == "security.ip-blocked":
            out.blocked_ips += 1
        elif event == "security.scan-ban":
            out.scan_bans += 1
        elif event == "smtp.mailbox-does-not-exist":
            out.mailbox_missing += 1
        elif event == "smtp.relay-not-allowed":
            out.relay_refused += 1
        elif event == "smtp.rate-limit-exceeded":
            out.rate_limited += 1
    return out


def _labelled(metric: str, pairs: list[tuple[str, str]], value: int) -> str:
    labels = ",".join(f'{key}="{val}"' for key, val in pairs)
    return f"{metric}{{{labels}}} {value}"


def _top(counter: Counter) -> list[tuple[str, int]]:
    """The ranked domains a panel shows, with the tail summed into `other`."""
    ranked = counter.most_common(TOP_DOMAINS)
    tail = sum(counter.values()) - sum(n for _, n in ranked)
    if tail:
        ranked.append(("other", tail))
    return ranked


def series(out: Traffic, window_hours: int = WINDOW_HOURS) -> str:
    """The check's own Prometheus series, appended to the shared textfile.

    One `# HELP`/`# TYPE` pair per family, omitted entirely when the window has no
    sample for it: an empty family written bare is invalid text and would take the
    whole file — including the rules' `last_status` — down with it.
    """
    body: list[str] = []

    def family(name: str, help_text: str, samples: list[str]) -> None:
        if not samples:
            return
        body.extend([f"# HELP {name} {help_text}", f"# TYPE {name} gauge", *samples])

    family("innotel_estate_check_mail_window_hours",
           "How far back this run measured the mail log",
           [f"innotel_estate_check_mail_window_hours {window_hours}"])
    family("innotel_estate_check_mail_lines",
           "Mail log lines the window contained (a silent server reads as 0)",
           [f"innotel_estate_check_mail_lines {out.lines}"])
    family("innotel_estate_check_mail_delivered",
           "Messages delivered, by queue (local = a mailbox here, remote = off-estate)",
           [_labelled("innotel_estate_check_mail_delivered", [("queue", q)], n)
            for q, n in sorted(out.delivered.items())])
    family("innotel_estate_check_mail_delivery_failed",
           "Delivery failures in the window, by kind (permanent = a bounce)",
           [_labelled("innotel_estate_check_mail_delivery_failed", [("reason", r)], n)
            for r, n in sorted(out.failed.items())])
    family("innotel_estate_check_mail_queued",
           "Messages accepted for delivery, by how they arrived",
           [_labelled("innotel_estate_check_mail_queued", [("via", v)], n)
            for v, n in sorted(out.queued.items())])
    family("innotel_estate_check_mail_ingested",
           "Messages put into a mailbox, by classification",
           [_labelled("innotel_estate_check_mail_ingested", [("result", r)], n)
            for r, n in sorted(out.ingested.items())])
    family("innotel_estate_check_mail_auth",
           "Authentication attempts, by listener and result",
           [_labelled("innotel_estate_check_mail_auth",
                      [("listener", listener), ("result", result)], n)
            for (listener, result), n in sorted(out.auth.items())])
    family("innotel_estate_check_mail_sender_domain",
           f"Volume by sender domain, top {TOP_DOMAINS} and `other`",
           [_labelled("innotel_estate_check_mail_sender_domain", [("domain", d)], n)
            for d, n in _top(out.senders)])
    family("innotel_estate_check_mail_recipient_domain",
           f"Volume by recipient domain, top {TOP_DOMAINS} and `other`",
           [_labelled("innotel_estate_check_mail_recipient_domain", [("domain", d)], n)
            for d, n in _top(out.recipients)])
    for name, value, help_text in (
        ("innotel_estate_check_mail_blocked_ips",
         out.blocked_ips, "Connections refused because the source IP is blocked"),
        ("innotel_estate_check_mail_scan_bans",
         out.scan_bans, "Addresses newly banned for scanning"),
        ("innotel_estate_check_mail_mailbox_missing",
         out.mailbox_missing, "Recipients refused because no mailbox exists"),
        ("innotel_estate_check_mail_relay_refused",
         out.relay_refused, "Relay attempts refused (unauthenticated off-estate sender)"),
        ("innotel_estate_check_mail_rate_limited",
         out.rate_limited, "Sessions rate-limited"),
        ("innotel_estate_check_mail_last_delivery_timestamp",
         int(out.last_delivery or 0), "When the last message was delivered"),
        ("innotel_estate_check_mail_last_auth_failure_timestamp",
         int(out.last_auth_failure or 0), "When authentication last failed"),
    ):
        family(name, help_text, [f"{name} {value}"])
    return "\n".join(body) + "\n"


def find_mail_host(runner=ssh) -> tuple[str, str]:
    """Which estate host runs the mail container right now.

    `read_host` is what raises `HostUnreadable`, but an unreachable host is not a
    finding on its own — the container is on exactly one of them — so a host that
    cannot be read is skipped here and only the *absence* of the container
    anywhere is exit 2.
    """
    for host, target in HOSTS.items():
        try:
            listing = read_host(host, target, "incus list --format csv -c n,s", runner)
        except HostUnreadable:  # not the host that runs it, or not up
            continue
        for line in listing.splitlines():
            name, _, state = line.partition(",")
            if name.strip() == CONTAINER and state.strip() == "RUNNING":
                return host, target
    raise HostUnreadable(CONTAINER, "estate hosts",
                         f"no estate host in {', '.join(sorted(HOSTS))} reports "
                         f"{CONTAINER} running")


def read_log(target: str, cutoff: str, runner=ssh, host: str = "") -> list[str]:
    """The window's log lines, read from inside the container.

    The narrowing runs on the host: `ls -1t | head` takes the newest files and the
    `grep` drops the families this check does not count, so what crosses the wire is
    a fraction of a day's log. The window itself is applied to the timestamps here —
    Stalwart writes RFC 3339 UTC, which compares correctly as a string — and the ANSI
    colour is stripped first, because `\x1b[37m2026-…` sorts before every timestamp
    and would drop the whole window.
    """
    command = (
        f"incus exec {CONTAINER} -- sh -c '"
        f"for f in $(ls -1t /var/log/stalwart/stalwart.* 2>/dev/null | head -{LOG_FILES}); "
        f'do cat "$f"; done | grep -aE "{EVENTS}"\''
    )
    text = ANSI.sub("", read_host(host, target, command, runner))
    return [line for line in text.splitlines() if line[:20] >= cutoff]


def cutoff_for(now: float, window_hours: int) -> str:
    """The ISO instant a `window_hours` window starts at, in the log's own clock."""
    return (datetime.fromtimestamp(now, timezone.utc)
            - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(now: float | None = None, window_hours: int = WINDOW_HOURS,
          runner=ssh) -> tuple[Audit, Traffic]:
    """Read the window and say what it means. The counts travel with the audit, so
    the metrics the dashboard reads and the verdict the rules read can never come
    from two different reads of the log."""
    now = time.time() if now is None else now
    host, target = find_mail_host(runner)
    out = measure(read_log(target, cutoff_for(now, window_hours), runner, host))

    result = Audit()
    # Rule 1: accepted and never delivered is the quiet failure this exists for. A
    # genuinely quiet estate sees no mail at all, which is a warning rather than a
    # failure — a stuck queue and a quiet Sunday must not read the same.
    accepted = sum(out.queued.values())
    if not accepted and out.attempts == 0:
        result.findings.append(Finding(
            "no-traffic", "warn",
            f"no mail activity in the last {window_hours}h — nothing was queued, sent "
            "or delivered (confirm the server is up before reading this as quiet)"))
    elif not out.delivered:
        result.findings.append(Finding(
            "nothing-delivered", "fail",
            f"{accepted or out.attempts} message(s) accepted in the last {window_hours}h "
            "and not one delivery completed — mail is being taken and never sent"))
    # Rule 2: a bounce is a real message that did not arrive.
    permanent = out.failed.get("permanent", 0)
    if permanent:
        result.findings.append(Finding(
            "bounced", "warn",
            f"{permanent} message(s) bounced permanently in the last {window_hours}h"))

    result.findings.append(Finding(
        "window", "note",
        f"{sum(out.delivered.values())} delivered, {sum(out.queued.values())} queued, "
        f"{sum(out.ingested.values())} ingested, {out.auth_failures} auth failure(s), "
        f"{out.blocked_ips} blocked IP event(s) in the last {window_hours}h"))
    return result, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--window", type=int, default=WINDOW_HOURS,
                        help=f"hours to measure (default {WINDOW_HOURS})")
    args = parser.parse_args(argv)

    now = time.time()
    try:
        result, out = audit(now, args.window)
    except HostUnreadable as error:
        print(f"check-mail-traffic: {error}", file=sys.stderr)
        if args.prom:
            write_prom(args.prom, CHECK, None, ran_ok=False)
        return 2

    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(f"\ncheck-mail-traffic: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s)")
    if args.prom:
        write_prom(args.prom, CHECK, result, ran_ok=True, now=now,
                   extra=series(out, args.window))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
