#!/usr/bin/env python3
"""Prove the estate's Alertmanager actually *delivers* mail, not only that it is configured.

WHY THIS EXISTS
---------------
The estate has exactly one Alertmanager (`signara-alertmanager-1` on `i3`), and it spent
its whole life configured-but-silent. First the receiver's `SMTP_HOST` and `ALERT_EMAIL_TO`
were empty, so it delivered nowhere; then, once pointed at the estate's own Stalwart, it
was refused for saying `EHLO localhost` and then for a self-signed certificate that names
no address. Every one of those states passed `amtool check-config` with `SUCCESS` and left
the UI looking healthy — the failure had no symptom except that nobody was ever told
anything. That is the worst shape an alerting component can fail in, and no check watched
it: `docs/container-placement.md` §Open items carried it as a note.

So this does what the note could not: it makes the receiver *talk*. It injects one probe
alert through Alertmanager's own v2 API (`amtool alert add`), then reads the estate's mail
server for a delivery from the receiver's address to its configured recipient. A receiver
that is silenced by config, refused by the relay, or unable to verify a certificate
produces no such delivery, and this fails.

THE RULES
---------
1. **No delivery within the window is a failure**, naming Alertmanager's own last notify
   error when it has one — that is the line that says *why* it is silent.
2. **A host that cannot be read is exit 2**, never silence, the same rule every check here
   follows. `i3` holds Alertmanager; `i1` holds Stalwart.
3. The probe alert is short-lived (`--end` a few minutes out) so it resolves itself and
   does not accumulate in the estate's one Alertmanager.

Usage:
    ./scripts/check-alert-delivery.py
    ./scripts/check-alert-delivery.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-alert-delivery.py

Exit codes: 0 = a probe alert was delivered, 1 = it was not, 2 = a host could not be read.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, read_host, ssh, write_prom  # noqa: E402

CHECK = "alert_delivery"

#: This check needs exactly two hosts — the one running Alertmanager and the one running
#: the mail server — so it carries its own two-entry table rather than the four-host one
#: the per-host checks share. It is deliberately not part of `test_check_host_coverage`
#: (that guard is about the checks that read *every* host).
HOSTS: dict[str, str] = {
    "i3": "root@192.168.1.53",  # signara — Alertmanager
    "i1": "root@192.168.1.51",  # mail — Stalwart
}

#: The container and the addresses the receiver is configured with. Kept here rather than
#: read from the container: what this check proves is that the *configured* path works, and
#: a value read back from the same config that might be wrong would prove nothing.
AM_CONTAINER = "signara-alertmanager-1"
AM_URL = "http://127.0.0.1:9093"
MAIL_FROM = "alertmanager@innotel.us"
MAIL_TO = "admin@innotel.us"
STALWART_LOG = "/var/log/stalwart"

#: group_wait is 30s and the mail hop takes a moment, so the probe needs to outwait the
#: first flush. Poll rather than sleep once, so a fast delivery is reported fast.
DEFAULT_WAIT_SECONDS = 90
POLL_SECONDS = 5

#: A Stalwart delivery log line begins with an RFC3339 UTC stamp:
#:   2026-10-02T21:48:17Z INFO Delivery completed (delivery.completed) queueId = …, from = "alertmanager@innotel.us", to = ["admin@innotel.us"], …
#: but the line usually leads with an ANSI colour escape (`\x1b[37m…\x1b[0m`) before the
#: stamp, so the escape is stripped before matching rather than anchored around.
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def probe_name(nonce: str) -> str:
    """A uniquely named alert, so a delivery in the window can be attributed to this run."""
    return f"EstateDeliveryProbe{nonce}"


def _amtool_time(moment: datetime) -> str:
    """RFC3339 as amtool parses it (`2006-01-02T15:04:05Z`).

    Seconds only, on purpose: a fractional-second stamp (what `datetime.isoformat()`
    produces) is accepted but misread — the probe's `startsAt` came out equal to its
    `--end`, five minutes in the future, so the alert never fired and the check saw a
    silent receiver that was really a malformed probe. Measured 2026-10-02.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def inject_command(name: str, start: datetime, end: datetime) -> str:
    """The shell command that adds one short-lived alert through Alertmanager's own API."""
    annotation = shlex.quote(f"Alertmanager delivery probe {name}")
    return (
        "incus exec signara -- docker exec "
        f"{AM_CONTAINER} amtool alert add {name} severity=warning service=estate-check "
        f"--alertmanager.url={AM_URL} --no-version-check "
        f"--start={_amtool_time(start)} --end={_amtool_time(end)} "
        f"--annotation=summary={annotation} </dev/null"
    )


def last_error_command() -> str:
    """Alertmanager's own last notify error, for the finding that says why it is silent.

    `|| true` because a receiver that has never failed has no such line, and "no error to
    show" must read as an empty answer, not as an ssh failure.
    """
    inner = (
        f"docker logs --since 10m {AM_CONTAINER} 2>&1 "
        "| grep -a 'Notify attempt failed' | tail -n 1 || true"
    )
    return "incus exec signara -- sh -c " + shlex.quote(inner) + " </dev/null"


def mail_log_command() -> str:
    """The successful deliveries from the receiver to its recipient, in today's log.

    `-a` because a log line can carry a control byte, and `|| true` so a day with no mail
    yet is an empty answer rather than an ssh failure (an empty answer is a *finding*: the
    receiver did not deliver).
    """
    inner = (
        f'f="{STALWART_LOG}/stalwart.$(date -u +%F)"; '
        f'grep -aE "delivery\\.(completed|dsn-success)" "$f" 2>/dev/null '
        f'| grep -aF "{MAIL_FROM}" | grep -aF "{MAIL_TO}" | tail -n 40 || true'
    )
    return "incus exec mail -- sh -c " + shlex.quote(inner) + " </dev/null"


def delivery_times(log_text: str) -> list[datetime]:
    """Every delivery stamp in `log_text`, newest last. Unparseable lines are skipped."""
    times: list[datetime] = []
    for line in log_text.splitlines():
        match = _STAMP.match(_ANSI.sub("", line))
        if not match:
            continue
        try:
            times.append(datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return times


def audit(delivered: bool, evidence: str) -> Audit:
    """Read the probe as findings. Pure, so what counts as delivered is the thing under test."""
    out = Audit()
    if delivered:
        out.findings.append(Finding("delivered", "note", "the probe alert was delivered to the mail server"))
    else:
        detail = f" — Alertmanager says: {evidence.strip()}" if evidence.strip() else ""
        out.findings.append(
            Finding(
                "silent",
                "fail",
                f"no delivery of the probe alert to {MAIL_TO} appeared within the window{detail}",
            )
        )
    return out


def delivery_series(delivered: bool, waited_seconds: float | None) -> str:
    lines = [
        f"# HELP {METRIC}_alert_delivered 1 when the Alertmanager probe was delivered",
        f"# TYPE {METRIC}_alert_delivered gauge",
        f'{METRIC}_alert_delivered{{check="{CHECK}"}} {1 if delivered else 0}',
    ]
    if waited_seconds is not None:
        lines += [
            f"# HELP {METRIC}_alert_delivery_seconds Seconds the probe waited for a delivery",
            f"# TYPE {METRIC}_alert_delivery_seconds gauge",
            f'{METRIC}_alert_delivery_seconds{{check="{CHECK}"}} {waited_seconds:.3f}',
        ]
    return "\n".join(lines)


def run_probe(
    *,
    am_host: str,
    am_target: str,
    mail_host: str,
    mail_target: str,
    wait: float,
    poll: float = POLL_SECONDS,
    runner=ssh,
    sleeper=time.sleep,
    clock=time.time,
    nonce: str | None = None,
) -> tuple[bool, str, float]:
    """Inject one probe alert and wait for its delivery. Returns (delivered, evidence, waited).

    Any ssh failure propagates as `HostUnreadable` through `read_host`, so `main` can turn
    it into exit 2 with the host named, rather than reporting a silent receiver because a
    *host* was down.
    """
    nonce = nonce or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    name = probe_name(nonce)
    started = clock()

    now = datetime.now(timezone.utc)
    read_host(am_host, am_target, inject_command(name, now, now + timedelta(minutes=5)), runner)

    deadline = started + wait
    while True:
        text = read_host(mail_host, mail_target, mail_log_command(), runner)
        times = delivery_times(text)
        if any(stamp.timestamp() >= started for stamp in times):
            return True, "", clock() - started
        if clock() >= deadline:
            break
        sleeper(min(poll, max(0.0, deadline - clock())))

    error = ""
    try:
        error = read_host(am_host, am_target, last_error_command(), runner)
    except Exception:  # noqa: BLE001 — evidence is best-effort; the failure stands without it
        error = ""
    return False, error, clock() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: i3, i1)")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS, help="seconds to wait for a delivery (0 = one pass)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    am_target = hosts.get("i3", HOSTS["i3"])
    mail_target = hosts.get("i1", HOSTS["i1"])

    def emit(result: Audit | None, ran: bool, delivered: bool = False, waited: float | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=delivery_series(delivered, waited))
            except OSError as error:
                print(f"check-alert-delivery: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-alert-delivery: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        delivered, evidence, waited = run_probe(
            am_host="i3",
            am_target=am_target,
            mail_host="i1",
            mail_target=mail_target,
            wait=args.wait,
        )
    except Exception as error:
        print(f"check-alert-delivery: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-alert-delivery: reads hosts with key-based `ssh`; where the estate uses "
                "passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(delivered, evidence)
    emit(result, ran=True, delivered=delivered, waited=waited)
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(f"\ncheck-alert-delivery: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
