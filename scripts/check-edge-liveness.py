#!/usr/bin/env python3
"""Notice when the edge stops answering — from a host that is not the edge.

WHY THIS EXISTS
---------------
Every other estate check runs **on the Cerulean edge** (`systemd/estate-checks.timer` on
`proxy`), and so does the Prometheus that evaluates their rules. That is a single point of
failure with a specific shape: if the edge host is down, the checks are down, the timer is
down, Prometheus is down, and **every alert that would say so is down with it**.
`docs/container-placement.md` §Open items called this "the honest limit of a check that
runs on a host it also monitors". This closes it, because the one thing a host cannot do is
tell you it is gone.

So this runs somewhere else — a normal estate host, not the edge — and does the smallest
possible thing: it connects to the edge **as the estate dials it**. If the edge's ingress
stops answering, this is the process that still knows, and `--notify` is how it says so.

**Where it runs is the invariant.** "Somewhere else" is `i4` since 2026-10-09
(`systemd/edge-liveness.{service,timer}`): the watcher and `proxy` must never share a host.
That was briefly not true — the unit had been installed on `i1` *because* the edge was not
there, and the 2026-10-09 placement change moved the edge back onto `i1`, so a host failure
took out the watcher and the edge together. The same change gave `i4` a wired uplink and
left it carrying `atheniq`, `mail`, `vault` and `vpn`, which makes it a normal estate host
and the watcher's home. Placement changes have to check the pair: whichever host the
watcher is installed on must not carry `proxy`. See `docs/container-placement.md`
§Changes applied 2026-10-09.

WHAT IT PROBES
--------------
TCP connect, not ICMP. A ping proves the host's network stack answered; a connect to
`:443` proves the edge's *ingress* did, which is what every A record and every consumer
actually depends on. The default endpoints are the edge's own web doors
(`192.168.1.71:80` and `:443`); `--endpoints` adds more.

Each endpoint is tried `--attempts` times (default 2) and counts as up if **any** attempt
answers. That is deliberate: a watcher that pages on one dropped packet on a WiFi uplink
would be turned off the first week, which is worse than not having it.

HOW IT REPORTS
--------------
Three ways, because this must work with no external account and no credential:

  * the exit code (0 = every endpoint answered, 1 = one did not) — what systemd records;
  * `--prom DIR/file.prom`, a textfile a node-exporter on **this** host can collect, so a
    Prometheus that does not live on the edge can still alert;
  * `--mail-to ADDRESS`, which emails the failure through `--smtp-host` (the estate's own
    mail server by default). This is why it does not lean on the estate's Alertmanager:
    that one lives on the edge's side of the estate and its delivery config is empty
    (see §Open items), and a watcher exists precisely for when that side is gone;
  * `--notify COMMAND`, run with the failure text on stdin. Empty (the default) means it
    only logs. Any other channel works — a webhook, an Alertmanager's `/api/v2/alerts` —
    because the command is configuration, not code.

Install (on a non-edge host):
    install -D -m 0755 scripts/check-edge-liveness.py /opt/innotel/edge-liveness/check-edge-liveness.py
    install -m 0644 systemd/edge-liveness.service systemd/edge-liveness.timer /etc/systemd/system/
    printf 'EDGE_LIVENESS_MAIL_TO=%s\n' admin@innotel.us > /etc/innotel/edge-liveness.env
    systemctl daemon-reload && systemctl enable --now edge-liveness.timer

Exit codes: 0 = the edge answered on every endpoint, 1 = it did not, 2 = nothing to probe.

Usage:
    ./scripts/check-edge-liveness.py
    ./scripts/check-edge-liveness.py --prom /var/lib/node_exporter/textfile/edge-liveness.prom
    ./scripts/check-edge-liveness.py --mail-to admin@innotel.us
    ./scripts/check-edge-liveness.py --endpoints 192.168.1.71:443,192.168.1.73:443
    ./scripts/check-edge-liveness.py --notify 'curl -s -XPOST http://host:9093/api/v2/alerts -d @-'
"""
from __future__ import annotations

import argparse
import os
import smtplib
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

#: The metric family this watcher publishes. Deliberately its own, not
#: `innotel_estate_check`: those series live on the edge, and this file exists precisely for
#: when they do not. A Prometheus that scrapes this host is the one that reads it.
METRIC = "innotel_edge_liveness"

#: The edge's own doors, by the addresses every A record names. A connect here succeeding is
#: what "the estate is reachable" means.
DEFAULT_ENDPOINTS = ["192.168.1.71:80", "192.168.1.71:443"]

#: Where mail goes when `--mail-to` is set: the estate's own mail server (`mail` on `i4`
#: since 2026-10-09; on `i1` when this was written). Deliberately the estate's, not an
#: external relay: this must work when the edge is gone, so it cannot depend on anything
#: hosted on the edge — which is why the watcher's host and `mail`'s host both matter — or
#: on a credential nobody has checked. The recipient has no default — an alert sent to an
#: address nobody chose is the failure mode that left the edge's own Alertmanager delivering
#: nowhere (see docs/container-placement.md §Open items).
SMTP_HOST = "192.168.1.15"
SMTP_PORT = 25
MAIL_FROM = "edge-watch@innotel.us"


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int

    @property
    def spec(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class Result:
    """One endpoint and how long it took to answer — or None if it did not."""

    endpoint: Endpoint
    ms: float | None


def parse_endpoint(text: str) -> Endpoint:
    """`host:port` to an Endpoint. Raises ValueError on anything else."""
    host, _, port = text.strip().rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"not a host:port endpoint: {text!r}")
    return Endpoint(host=host.strip("[]"), port=int(port))


def tcp_probe(
    endpoint: Endpoint,
    timeout: float = 5.0,
    connect: Callable[..., socket.socket] = socket.create_connection,
    clock: Callable[[], float] = time.monotonic,
) -> float | None:
    """Connect to `endpoint`, returning milliseconds or None. `connect`/`clock` injectable."""
    start = clock()
    try:
        with connect((endpoint.host, endpoint.port), timeout=timeout):
            return (clock() - start) * 1000.0
    except OSError:
        return None


def probe(
    endpoints: list[Endpoint],
    attempts: int = 2,
    timeout: float = 5.0,
    connect: Callable[..., socket.socket] = socket.create_connection,
    clock: Callable[[], float] = time.monotonic,
) -> list[Result]:
    """Try each endpoint up to `attempts` times; it is up if any attempt answers."""
    results: list[Result] = []
    for endpoint in endpoints:
        ms = None
        for _ in range(max(1, attempts)):
            ms = tcp_probe(endpoint, timeout=timeout, connect=connect, clock=clock)
            if ms is not None:
                break
        results.append(Result(endpoint=endpoint, ms=ms))
    return results


def all_up(results: list[Result]) -> bool:
    return all(result.ms is not None for result in results)


def report(results: list[Result], edge: str = "the edge") -> str:
    """A line per endpoint, so a journal entry names what stopped answering."""
    lines = []
    for result in results:
        state = f"{result.ms:.0f} ms" if result.ms is not None else "NO ANSWER"
        lines.append(f"  {result.endpoint.spec:24} {state}")
    dark = [r.endpoint.spec for r in results if r.ms is None]
    summary = f"{edge} answered on all {len(results)} endpoint(s)" if not dark else f"{edge} is dark on: {', '.join(dark)}"
    return "\n".join(lines + [summary])


def textfile(results: list[Result], now: float, edge: str = "the edge") -> str:
    """The textfile body: one reachability gauge, per-endpoint latency, and last success.

    `last_success_timestamp` is only advanced on a healthy probe, so a Prometheus that
    scrapes this host can alert on staleness even though the edge — and the edge's own
    clock and thresholds — may be gone.
    """
    up = all_up(results)
    lines = [
        f"# HELP {METRIC}_reachable 1 when {edge} answered on every probed endpoint",
        f"# TYPE {METRIC}_reachable gauge",
        f"{METRIC}_reachable {1 if up else 0}",
        f"# HELP {METRIC}_last_run_timestamp When this watcher last ran, whatever it found",
        f"# TYPE {METRIC}_last_run_timestamp gauge",
        f"{METRIC}_last_run_timestamp {now:.0f}",
    ]
    if up:
        lines += [
            f"# HELP {METRIC}_last_success_timestamp When {edge} last answered everywhere",
            f"# TYPE {METRIC}_last_success_timestamp gauge",
            f"{METRIC}_last_success_timestamp {now:.0f}",
        ]
    lines += [
        f"# HELP {METRIC}_endpoint_ms Connect time to an edge endpoint in milliseconds",
        f"# TYPE {METRIC}_endpoint_ms gauge",
    ]
    for result in results:
        if result.ms is not None:
            lines.append(f'{METRIC}_endpoint_ms{{endpoint="{result.endpoint.spec}"}} {result.ms:.3f}')
    return "\n".join(lines) + "\n"


def write_textfile(path: str, body: str) -> None:
    """Write atomically and 0644 — the collector reads it as an unprivileged user."""
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False, prefix=".edge-liveness-")
    try:
        handle.write(body)
        handle.close()
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise


def send_mail(
    host: str,
    port: int,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    timeout: float = 20.0,
    smtp: Callable[..., smtplib.SMTP] = smtplib.SMTP,
) -> None:
    """Send one plain-text message. `smtp` is injectable so the tests never open a socket."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtp(host, port, timeout=timeout) as client:
        client.send_message(message)


def notify(command: str, message: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    """Run `command` with `message` on stdin. Errors here are reported, never fatal.

    A watcher whose own delivery channel throws must still record that the edge was dark,
    so a failed notify is printed and swallowed rather than raised.
    """
    try:
        runner(command, shell=True, input=message, text=True, timeout=60, check=False)
    except Exception as error:  # noqa: BLE001 — the finding matters more than the channel
        print(f"check-edge-liveness: notify command failed: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoints", default=",".join(DEFAULT_ENDPOINTS), help="comma-separated host:port (default: the edge's 80/443)")
    parser.add_argument("--attempts", type=int, default=2, help="connects per endpoint; up if any answers (default 2)")
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds per connect (default 5)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for a Prometheus off the edge")
    parser.add_argument("--notify", default="", metavar="COMMAND", help="shell command to run with the failure text on stdin")
    parser.add_argument("--mail-to", default="", metavar="ADDRESS", help="email the failure here (via --smtp-host)")
    parser.add_argument("--smtp-host", default=SMTP_HOST, help=f"the mail server to send through (default {SMTP_HOST}, the estate's)")
    parser.add_argument("--smtp-port", type=int, default=SMTP_PORT, help=f"the mail server's SMTP port (default {SMTP_PORT})")
    parser.add_argument("--mail-from", default=MAIL_FROM, help=f"the alert's From address (default {MAIL_FROM})")
    parser.add_argument("--edge", default="the edge", help="what to call the thing being watched, in prose")
    args = parser.parse_args(argv)

    try:
        endpoints = [parse_endpoint(part) for part in args.endpoints.split(",") if part.strip()]
    except ValueError as error:
        print(f"check-edge-liveness: {error}", file=sys.stderr)
        return 2
    if not endpoints:
        print("check-edge-liveness: no endpoints to probe", file=sys.stderr)
        return 2

    now = time.time()
    results = probe(endpoints, attempts=args.attempts, timeout=args.timeout)
    ok = all_up(results)
    text = report(results, edge=args.edge)
    print(text)

    if args.prom:
        try:
            write_textfile(args.prom, textfile(results, now, edge=args.edge))
        except OSError as error:
            print(f"check-edge-liveness: could not write {args.prom}: {error}", file=sys.stderr)

    if not ok and args.mail_to.strip():
        subject = f"[critical] {args.edge} did not answer"
        try:
            send_mail(args.smtp_host, args.smtp_port, args.mail_from, args.mail_to, subject, text + "\n")
            print(f"check-edge-liveness: mailed {args.mail_to} via {args.smtp_host}:{args.smtp_port}")
        except Exception as error:  # noqa: BLE001 — the finding matters more than the channel
            print(f"check-edge-liveness: could not send mail to {args.mail_to}: {error}", file=sys.stderr)

    if not ok and args.notify.strip():
        notify(args.notify, text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
