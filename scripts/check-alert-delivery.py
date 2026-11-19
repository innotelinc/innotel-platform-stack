#!/usr/bin/env python3
"""Prove the estate's Alertmanager actually *delivers* mail, not only that it is configured.

WHY THIS EXISTS
---------------
The estate has exactly one Alertmanager (`signara-alertmanager-1`, in Signara's stack — on
`i2` since 2026-11-02, having been on `i3` before that host was retired), and it spent
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
   follows. `i2` holds Alertmanager; the host holding Stalwart (the mail server) is read
   from the container address table rather than pinned here, because it moves — `mail` was
   on `i1` when this check was written, on `i4` after the 2026-10-09 pass, on `i3` from
   2026-10-15 and on `i2` since 2026-11-02, and a check
   that reads the mail log on the host the server has left exits 2 instead of reporting
   the silence it exists to catch.
3. The probe alert is short-lived (`--end` a few minutes out) so it resolves itself and
   does not accumulate in the estate's one Alertmanager.
4. **The probe rides the submission listener, not the mail exchanger.** Stalwart logs the
   probe's hop as `listenerId = "submission", localPort = 587` from the Alertmanager
   container's own address, so the internet-facing `MtaInboundThrottle` on port 25 — the
   per-IP anti-abuse limit a mail burst spends — has no bearing on this check. Measured
   2026-11-19 while chasing a failure that the throttle was blamed for: the throttled
   message was a *different* delivery from the estate's public address, and the probe it
   was said to have starved went out authenticated on 587 in ~1 s. A probe that fails is
   the receiver, the relay, or the wait, never inbound throttling; do not raise the port-25
   limit to "fix" this check.

Usage:
    ./scripts/check-alert-delivery.py
    ./scripts/check-alert-delivery.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-alert-delivery.py

Exit codes: 0 = a probe alert was delivered, 1 = it was not, 2 = a host could not be read.
"""
from __future__ import annotations

import argparse
import importlib.util
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

#: The Alertmanager host: it is the estate's one receiver, and it follows the `signara`
#: container rather than a host of its own. That container kept `.44` through both moves —
#: off `i3` to `i2` on 2026-11-02 and back to `i3` on 2026-11-15 — so this target has to
#: follow the container, not stay where it was first written down. It pointed at `i2`
#: after the first move and reported "could not read a host" once `signara` was on the
#: rebuilt `i3`; verified on `i3` before the change: `signara-alertmanager-1` up and
#: `127.0.0.1:9093/-/healthy` → `OK` inside the container.
AM_HOST = "i3"
AM_TARGET = "root@192.168.1.53"  # signara — Alertmanager

#: This check needs exactly two hosts — the one running Alertmanager and the one running
#: the mail server — so it carries its own table rather than the four-host one the per-host
#: checks share. Only the Alertmanager is in it: the mail server's host is derived by
#: `mail_host()` below, because that is the one that moves. It is deliberately not part of
#: `test_check_host_coverage` (that guard is about the checks that read *every* host).
HOSTS: dict[str, str] = {AM_HOST: AM_TARGET}


def mail_host() -> tuple[str, str]:
    """Which estate host carries `mail`, and how to reach it, from the address table.

    Read from `check-container-addresses.py` rather than pinned here, for the same reason
    `check-address-latency.py` reads its targets from it: the address check already knows
    where every container runs, so "the mail server's host" means the same thing in both
    places and a move cannot leave this check reading a log on the host `mail` has left.
    The module is loaded by path (its filename has a hyphen) and has no side effects.

    `--hosts` still overrides the target for either host.
    """
    path = Path(__file__).resolve().parent / "check-container-addresses.py"
    spec = importlib.util.spec_from_file_location("check_container_addresses_for_delivery", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for host, containers in module.EXPECTED.items():
        if "mail" in containers:
            return host, module.HOSTS[host]
    raise SystemExit("check-alert-delivery: no host in the address table carries `mail`")

#: The container and the addresses the receiver is configured with. Kept here rather than
#: read from the container: what this check proves is that the *configured* path works, and
#: a value read back from the same config that might be wrong would prove nothing.
AM_CONTAINER = "signara-alertmanager-1"
AM_URL = "http://127.0.0.1:9093"
MAIL_FROM = "alertmanager@innotel.us"
#: Where the receiver sends the probe: `SMTP_USER` on the relay, the alerts account —
#: not `ALERT_EMAIL_TO`, the human inbox. Alertmanager mails only `critical` there, and
#: a delivery probe that pages somebody every half hour is a probe that teaches them to
#: ignore the mailbox. `alertmanager.yml` routes `EstateDeliveryProbe*` to a `probe`
#: receiver whose only `to` is this address, so the proof costs nobody an inbox.
MAIL_TO = "alertmanager@innotel.us"
STALWART_LOG = "/var/log/stalwart"

#: group_wait is 30s and the mail hop takes a moment, so the probe needs to outwait the
#: first flush. Poll rather than sleep once, so a fast delivery is reported fast.
#:
#: 180s rather than 90s, measured 2026-11-18: a healthy probe lands in ~35s, but the same
#: probe took longer than 90s twice in one afternoon while `signara-api-1` was restarting
#: and four other alerts were firing — Alertmanager serialises sends per integration and
#: retries a failed SMTP attempt for 15s at a time, so a probe submitted during a storm
#: queues behind them. The assertion is unchanged — no delivery, no pass — the check was
#: simply giving up inside the receiver's own retry window, which reads as an intermittent
#: failure that teaches people to ignore it.
DEFAULT_WAIT_SECONDS = 180
POLL_SECONDS = 5

#: A delivery to a mailbox on this server is logged as an *ingest*, not a delivery:
#:   2026-11-15T02:29:57Z INFO Message ingested (message-ingest.ham) queueId = …, queueName = "local", from = "alertmanager@innotel.us", to = ["alertmanager@innotel.us"], …
#: `delivery.completed`/`delivery.dsn-success` is what the server writes when a message
#: leaves for another host; when both ends are local the ingest is the only line, and the
#: probe's recipient is the alerts account on this same server. Measured 2026-11-15: the
#: submit and the mailbox both worked while this check called the receiver silent, because
#: it only knew the outbound event names.
#: The line begins with an RFC3339 UTC stamp:
#:   2026-10-02T21:48:17Z INFO Delivery completed (delivery.completed) queueId = …, from = "alertmanager@innotel.us", to = ["admin@innotel.us"], …
#: but the line usually leads with an ANSI colour escape (`\x1b[37m…\x1b[0m`) before the
#: stamp, so the escape is stripped before matching rather than anchored around.
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
#: The sender and recipient of a delivery line, once the colour escapes are gone:
#: `from = "alertmanager@innotel.us", to = ["alertmanager@innotel.us"]`.
_FIELDS = re.compile(r'from = "([^"]*)", to = \[([^\]]*)\]')


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


def query_command(name: str) -> str:
    """How many alerts named `name` Alertmanager currently holds.

    This exists because the injection can silently do nothing. Measured 2026-11-18: the
    same `amtool alert add` that usually registers a probe returned without registering
    one, and the check then waited out its whole window and reported a *silent receiver* —
    a finding about the mail path for a failure in the probe. Counting afterwards is what
    tells the two apart.
    """
    inner = (
        f"docker exec {AM_CONTAINER} amtool alert query --alertmanager.url={AM_URL} "
        f"--no-version-check 2>&1 | grep -c {shlex.quote(name)} || true"
    )
    return "incus exec signara -- sh -c " + shlex.quote(inner) + " </dev/null"


def _count_of(text: str) -> int | None:
    """The single integer in a `grep -c` answer, or None when the answer is not one."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


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

    A recipient on this server counts too: Stalwart logs that as
    `message-ingest.ham`, with the same `from`/`to` fields, and never as
    `delivery.completed`.

    `-a` because a log line can carry a control byte, and `|| true` so a day with no mail
    yet is an empty answer rather than an ssh failure (an empty answer is a *finding*: the
    receiver did not deliver).
    """
    # Candidates only: every delivery or local ingest in today's log, newest last. The
    # sender/recipient filter is applied in Python, not here, because Stalwart colours its
    # field names — the line reads `\x1b[36mfrom\x1b[0m = "…"`, so `grep -F 'from = "'`
    # matches nothing at all. Measured 2026-10-24, after a probe was delivered and this
    # check still called the receiver silent.
    inner = (
        f'f="{STALWART_LOG}/stalwart.$(date -u +%F)"; '
        rf'grep -aE "delivery\.(completed|dsn-success)|message-ingest\.(ham|spam)" "$f" 2>/dev/null '
        f'| tail -n 400 || true'
    )
    return "incus exec mail -- sh -c " + shlex.quote(inner) + " </dev/null"


def delivery_times(
    log_text: str,
    *,
    from_address: str | None = None,
    to_address: str | None = None,
) -> list[datetime]:
    """Every delivery stamp in `log_text`, newest last. Unparseable lines are skipped.

    With `from_address`/`to_address` given, only deliveries between exactly those two
    addresses count. That matters here because the probe is addressed to the same account
    it is sent from: an unfiltered match would take any delivery from the alerts account —
    including a real page to the human inbox — as proof of the probe's route.
    """
    times: list[datetime] = []
    for line in log_text.splitlines():
        clean = _ANSI.sub("", line)
        match = _STAMP.match(clean)
        if not match:
            continue
        if from_address or to_address:
            fields = _FIELDS.search(clean)
            if not fields:
                continue
            if from_address and fields.group(1) != from_address:
                continue
            if to_address and f'"{to_address}"' not in fields.group(2):
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
    # Start the probe a minute in the *past* on purpose. `--start=now` is measured against
    # this host's clock while Alertmanager decides with its own, so a container running a
    # little behind refuses to fire the alert until the skew passes — which looked like an
    # intermittently silent receiver (measured 2026-11-18, and a manual add with a start
    # ahead of the container clock registered nothing at all).
    injected_at = now - timedelta(minutes=1)
    ends_at = now + timedelta(minutes=5)
    read_host(am_host, am_target, inject_command(name, injected_at, ends_at), runner)

    # Verify the probe landed, and re-inject once if it did not. An alert that was never
    # registered cannot be delivered, and saying so is different from blaming the receiver.
    registered: bool | None = None
    for attempt in range(2):
        registered = _count_of(read_host(am_host, am_target, query_command(name), runner))
        registered = None if registered is None else registered > 0
        if registered is not False:
            break
        if attempt == 0:
            read_host(am_host, am_target, inject_command(name, injected_at, ends_at), runner)
    if registered is False:
        return (
            False,
            f"the probe alert {name} never appeared in Alertmanager, so nothing could be "
            f"delivered: the injection failed, the receiver was never asked",
            clock() - started,
        )

    deadline = started + wait
    while True:
        text = read_host(mail_host, mail_target, mail_log_command(), runner)
        times = delivery_times(text, from_address=MAIL_FROM, to_address=MAIL_TO)
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
    parser.add_argument(
        "--hosts",
        help="name=target pairs, comma separated (default: i2, and whichever host carries `mail`)",
    )
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS, help="seconds to wait for a delivery (0 = one pass)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    mail_name, mail_default = mail_host()
    am_target = hosts.get(AM_HOST, AM_TARGET)
    mail_target = hosts.get(mail_name, mail_default)

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
            am_host=AM_HOST,
            am_target=am_target,
            mail_host=mail_name,
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
