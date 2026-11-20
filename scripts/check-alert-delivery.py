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
4. **   The probe rides the submission listener, not the mail exchanger.** Stalwart logs the
   probe's hop as `listenerId = "submission", localPort = 587` from the Alertmanager
   container's own address, so the internet-facing `MtaInboundThrottle` on port 25 — the
   per-IP anti-abuse limit a mail burst spends — has no bearing on this check. Measured
   2026-11-19 while chasing a failure that the throttle was blamed for: the throttled
   message was a *different* delivery from the estate's public address, and the probe it
   was said to have starved went out authenticated on 587 in ~1 s. A probe that fails is
   the receiver, the relay, or the wait, never inbound throttling; do not raise the port-25
   limit to "fix" this check.
5. **The delivery has to be one that was not already in the log.** The probe proves itself
   by a delivery newer than anything the log held *before* the probe existed — a delivery
   already in the log could otherwise be read as a pass in a couple of seconds, which the
   published `alert_delivery_seconds` cannot distinguish from a probe that genuinely went
   out at once because its alert joined a group whose `group_wait` had already elapsed.
   Stalwart
   never logs a subject, so the message is identified by its own `queueId`: the queue
   record and the ingest name the same id, and a message queued before this probe began
   cannot be this probe's delivery, however late it arrives. A message ingested inside the
   submission transaction is the exception: it carries no sender, recipient or queueId at
   all (see `INLINE_INGEST_TOLERANCE_SECONDS`), so it is attributed by the submission
   record written within seconds of it, and that record is fetched for this reason.
   Comparing a delivery stamp to this host's start time instead measured two different machines'
   clocks: a mail server a few seconds behind read as silence, and a delivery already in
   the log read as a pass in ~1.3 s — which is what the published `alert_delivery_seconds`
   series showed for most runs (measured 2026-11-19: 27 of 75 runs published 0, and every
   failure stopped at 181.8–182.1 s, the old 180 s window).

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
#: 300s rather than 90s. Measured 2026-11-18: a healthy probe lands in ~35s, but the same
#: probe took longer than 90s twice in one afternoon while `signara-api-1` was restarting
#: and four other alerts were firing — Alertmanager serialises sends per integration and
#: retries a failed SMTP attempt for 15s at a time, so a probe submitted during a storm
#: queues behind them. Measured again 2026-11-19 over three days of the published series:
#: 27 of 75 runs reported 0, and every failing run stopped at 181.8–182.1 s — the 180 s
#: window, not the receiver. The assertion is unchanged — no delivery, no pass — the check
#: was simply giving up inside the receiver's own retry window, which reads as an
#: intermittent failure that teaches people to ignore it.
#: The mail log then showed a failing run's own probe ingested 242 s after it started
#: (16:16:24 start, 16:20:26 ingest): 360 s leaves headroom over the measured queue.
DEFAULT_WAIT_SECONDS = 360

#: How far the mail server's stamp may sit behind this host's clock and still be this run's
#: delivery. The two are different machines, so a stamp a few seconds "early" is normal;
#: the baseline read in `run_probe` is what stops the tolerance reaching a previous run.
SKEW_TOLERANCE_SECONDS = 300
#: The *other* shape Stalwart writes when a message reaches a mailbox here:
#:   2026-11-19T21:01:09Z INFO Message ingested (message-ingest.ham) accountId = 2, documentId = 2216, mailboxId = [0], blobId = "…", changeId = 2221, messageId = "…", size = 10757, elapsed = 0ms
#: No sender, no recipient, no queueId: the ingest happens *inside the submission
#: transaction*, and the queue record for the same message is written beside it. Measured
#: 2026-11-19 on the 16:01 EST run this check failed on — the probe's own ingest was there
#: at the same second as its `queue.authenticated-message-queued`, so the receiver had
#: delivered it and the check called it silent. Attribution for this shape can only come
#: from the submission record, which is therefore fetched too, and only an ingest within a
#: few seconds of it counts: an inline ingest is immediate by construction (`elapsed =
#: 0ms`), so a wider allowance would let a *different* message that happened to arrive
#: during the run's window be read as this probe's delivery.
INLINE_INGEST_TOLERANCE_SECONDS = 5

#: How far before this run a message may have entered the queue and still be ours. The
#: queue record and the ingest are written by the *same* server clock, so this is not a
#: clock-skew allowance: it exists only because the injection and the queue write are
#: milliseconds apart. Kept tight so two checks run minutes apart cannot cross-credit.
QUEUE_TOLERANCE_SECONDS = 60
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

#: The events that say a message reached a mailbox on this server.
_INGEST_EVENTS = ("message-ingest.ham", "message-ingest.spam")

#: Every event that means "a message reached its destination", local or outbound.
_EVENTS = (
    "delivery.completed",
    "delivery.dsn-success",
) + _INGEST_EVENTS
#: The events that say when a message *entered the queue*: the same queueId, earlier in
#: the same log. This is the only per-message handle Stalwart logs — it never logs a
#: subject, so a timestamp cannot tell this run's message from an earlier probe's delivery
#: that sat in the queue and arrived late.
_QUEUED = ("queue.message-queued", "queue.authenticated-message-queued")
_QUEUE_ID = re.compile(r"queueId\s*=\s*([0-9A-Za-z_.:-]+)")


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
    # The queue records are fetched as well: an ingest logged without sender/recipient
    # can only be attributed through the submission written beside it, and the tail is
    # longer because those records now share it.
    inner = (
        f'f="{STALWART_LOG}/stalwart.$(date -u +%F)"; '
        rf'grep -aE "delivery\.(completed|dsn-success)|message-ingest\.(ham|spam)'
        rf'|queue\.(message-queued|authenticated-message-queued)" "$f" 2>/dev/null '
        f'| tail -n 4000 || true'
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
    return [
        stamp
        for stamp, event, _queue in _records(
            log_text, from_address=from_address, to_address=to_address
        )
        if event in _EVENTS
    ]


def _records(
    log_text: str,
    *,
    from_address: str | None = None,
    to_address: str | None = None,
) -> list[tuple[datetime, str, str | None]]:
    """(stamp, event, queueId) for every queue/ingest/delivery line, newest last."""
    found: list[tuple[datetime, str, str | None]] = []
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
        event = next((name for name in _EVENTS + _QUEUED if name in clean), None)
        if event is None:
            continue
        queue = _QUEUE_ID.search(clean)
        try:
            stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        found.append((stamp, event, queue.group(1) if queue else None))
    return found


def queued_times(
    records: list[tuple[datetime, str, str | None]],
) -> dict[str, datetime]:
    """When each message was queued, by queueId — the handle that says whose message it is."""
    return {queue: stamp for stamp, event, queue in records if event in _QUEUED and queue}


def inline_ingest_times(log_text: str) -> list[datetime]:
    """Ingest stamps for messages logged without a sender, a recipient or a queueId.

    See `INLINE_INGEST_TOLERANCE_SECONDS`: this shape carries only `accountId`,
    `documentId` and the blob, so it says nothing about whose message it was and cannot be
    filtered against the probe's addresses. `run_probe` attributes it by the submission
    record written at the same moment; this function only finds the candidates.
    """
    found: list[datetime] = []
    for line in log_text.splitlines():
        clean = _ANSI.sub("", line)
        match = _STAMP.match(clean)
        if not match or not any(event in clean for event in _INGEST_EVENTS):
            continue
        if _FIELDS.search(clean):
            continue
        try:
            found.append(
                datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            )
        except ValueError:
            continue
    return found


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

    # Sample the log *before* the probe exists. Without this the check either took a
    # delivery already sitting in the log as proof of the probe it had just injected
    # (most runs finished in ~1.3 s, inside Alertmanager's 30 s group_wait) or, comparing
    # the stamp to `started`, read a mail server whose clock is a little behind as silent.
    baseline = delivery_times(
        read_host(mail_host, mail_target, mail_log_command(), runner),
        from_address=MAIL_FROM,
        to_address=MAIL_TO,
    )
    newest_before = max(baseline) if baseline else None
    floor = datetime.fromtimestamp(started, tz=timezone.utc) - timedelta(
        seconds=SKEW_TOLERANCE_SECONDS
    )
    queued_floor = datetime.fromtimestamp(started, tz=timezone.utc) - timedelta(
        seconds=QUEUE_TOLERANCE_SECONDS
    )

    def is_fresh(stamp: datetime) -> bool:
        """A delivery this run can claim: recent, and newer than anything already logged."""
        if stamp < floor:
            return False
        return newest_before is None or stamp > newest_before

    def is_this_run(stamp: datetime, queue_id: str | None, queued: dict[str, datetime]) -> bool:
        """Fresh *and*, when the log says when it was queued, queued after this run started.

        A stamp alone cannot separate this run's message from a previous probe's delivery
        that sat in the queue and arrived late: both are newer than the baseline. The queue
        record carries the same queueId as the ingest, so its stamp says when the message
        entered the queue, and anything queued before this run's floor is not ours.
        The fallback is deliberate: the tail read can miss the queue line, and then
        freshness is all the log can prove.
        """
        if not is_fresh(stamp):
            return False
        submitted = queued.get(queue_id) if queue_id else None
        return submitted is None or submitted >= queued_floor

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
        records = _records(text, from_address=MAIL_FROM, to_address=MAIL_TO)
        queued = queued_times(records)
        # The receiver"s own submissions in this log, as stamps: the only handle an ingest
        # logged without a sender can be tied to (see `inline_ingest_times`).
        submissions = [stamp for stamp, event, _queue in records if event in _QUEUED]

        def is_this_run_inline(stamp: datetime) -> bool:
            """Fresh, and beside a submission this run made — the probe's own transaction."""
            if not is_fresh(stamp):
                return False
            return any(
                submitted >= queued_floor
                and abs((stamp - submitted).total_seconds()) <= INLINE_INGEST_TOLERANCE_SECONDS
                for submitted in submissions
            )

        if any(
            is_this_run(stamp, queue_id, queued)
            for stamp, event, queue_id in records
            if event in _EVENTS
        ) or any(is_this_run_inline(stamp) for stamp in inline_ingest_times(text)):
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
