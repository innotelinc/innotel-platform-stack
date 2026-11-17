#!/usr/bin/env python3
"""Name the estate checks that flap, from the series they already publish.

WHY THIS EXISTS
---------------
Every check here writes `innotel_estate_check_last_status` on each run, and the alerts read
the *current* value. That is right for paging — a door that is dark is dark — and blind to
the other failure: a check that goes red and green every few runs. Flapping costs the same
attention as a real outage in notifications, and it hides real outages behind "it is always
like that". On 2026-11-16 two checks (media_doors, sso_redirects) each produced one red run
that repaired itself, and only a hand comparison of runs found it.

So this reads the history rather than the moment: Prometheus's range API for
`innotel_estate_check_last_status` over a window, one series per check, and reports the ones
whose value changed more than the tolerance allows.

WHAT IT REPORTS
---------------
* A **warning** per check whose status changed more than once in the window, naming how many
  times and how many samples were red. Once is a blip; more than once is a pattern.
* A **note** per check that was red at all but changed at most once, so a single repair is
  visible without being called flapping.
* One **note** stating the coverage: how many series, how long the window really was, and
  how old the oldest sample is. Prometheus retention is finite and a series can restart, so
  "no flaps" without a coverage line is a claim about a window that may not exist.

A Prometheus that cannot be queried is **exit 2** — a flap report that cannot read history is
not a clean bill of health.

Usage:
    ./scripts/check-estate-flaps.py
    ./scripts/check-estate-flaps.py --days 7 --prom /var/lib/node_exporter/textfile/flaps.prom

Exit codes: 0 = no check flapped, 1 = at least one did, 2 = could not run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from estate_check import Audit, Finding, write_prom  # noqa: E402

CHECK = "estate_flaps"
METRIC = "innotel_estate_check_last_status"
DEFAULT_PROMETHEUS = "http://127.0.0.1:9090"
DEFAULT_DAYS = 7.0
#: One red episode is a blip (a restart window, a transient resolver failure). A second
#: episode inside the window is the pattern this check is for.
DEFAULT_MAX_EPISODES = 1


class PrometheusUnreadable(RuntimeError):
    """The range query could not be answered — exit 2, never "no flaps"."""


def query_range(base: str, query: str, start: float, end: float, step: float,
                timeout: float = 15.0) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "start": f"{start:.0f}",
                                     "end": f"{end:.0f}", "step": f"{step:.0f}"})
    url = f"{base.rstrip('/')}/api/v1/query_range?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read(64_000_000))
    except Exception as error:  # noqa: BLE001 — transport, TLS, timeout, bad JSON
        raise PrometheusUnreadable(str(error)) from error
    if body.get("status") != "success":
        raise PrometheusUnreadable(body.get("error") or "the query did not succeed")
    return body["data"]["result"]


def values_of(series: dict) -> list[tuple[float, float]]:
    """`[(timestamp, value)]`, dropping samples Prometheus reports as NaN."""
    out: list[tuple[float, float]] = []
    for stamp, raw in series.get("values") or []:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        out.append((float(stamp), value))
    return out


def count_episodes(values: list[float]) -> int:
    """How many separate runs of not-green the samples contain.

    Counting *transitions* calls a single repaired blip a flap: green → red → green is two
    changes, and the blip that started this check would read the same as a check cycling
    every run. What costs attention is the number of episodes — a single red run between
    good ones is one, and going red again later is the pattern.
    """
    episodes = 0
    previous = 1.0
    for value in values:
        if value != 1 and previous == 1:
            episodes += 1
        previous = value
    return episodes


def audit(series: list[dict], *, now: float | None = None,
          max_episodes: int = DEFAULT_MAX_EPISODES) -> Audit:
    """Per-check flapping findings from Prometheus range results."""
    result = Audit()
    now = time.time() if now is None else now
    rows: list[tuple[str, int, int, float]] = []
    oldest: float | None = None

    for item in series:
        name = (item.get("metric") or {}).get("check")
        if not name:
            continue
        samples = values_of(item)
        if not samples:
            continue
        values = [value for _, value in samples]
        first = samples[0][0]
        oldest = first if oldest is None else min(oldest, first)
        rows.append((name, count_episodes(values), len(values) - sum(1 for v in values if v == 1),
                     first))

    if not rows:
        result.findings.append(Finding(
            "no-history", "warn",
            "no check-series came back for the window — the estate is not reporting, which "
            "is the outage this report cannot see either"))
        return result

    for name, episodes, red, _ in sorted(rows, key=lambda row: (-row[1], row[0])):
        if episodes > max_episodes:
            result.findings.append(Finding(
                "flapping", "warn",
                f'check "{name}" went red {episodes} separate times in the window '
                f"({red} of its samples not green) — it flaps, and each episode costs the "
                "same attention as an outage; read `journalctl -u estate-checks.service` for "
                "what it said on the red runs"))
        elif episodes == 1:
            result.findings.append(Finding(
                "single-episode", "note",
                f'check "{name}" went red once ({red} not green) — a single repair, '
                "below the flapping threshold"))

    covered = (now - oldest) / 3600 if oldest else 0.0
    result.findings.append(Finding(
        "coverage", "note",
        f"{len(rows)} check series over the window; oldest sample is {covered:.1f}h old — "
        "the window is only what Prometheus still holds"))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prometheus", default=DEFAULT_PROMETHEUS)
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS)
    parser.add_argument("--step", type=float, default=1800.0,
                        help="seconds between samples (default: the estate's half-hour cadence)")
    parser.add_argument("--max-episodes", type=int, default=DEFAULT_MAX_EPISODES,
                        help="red episodes allowed in the window before a check is flapping")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran_ok=ran)
        except OSError as error:
            print(f"check-estate-flaps: could not write {args.prom}: {error}", file=sys.stderr)

    end = time.time()
    try:
        series = query_range(args.prometheus, f'count by (check) ({METRIC})', end - args.days * 86_400,
                             end, args.step, timeout=args.timeout)
    except PrometheusUnreadable as error:
        emit(None, ran=False)
        print(f"check-estate-flaps: {args.prometheus} could not be queried: {error}",
              file=sys.stderr)
        return 2

    result = audit(series, now=end, max_episodes=args.max_episodes)
    emit(result, ran=True)

    if args.json:
        print(json.dumps({
            "findings": [{"code": f.code, "level": f.level, "message": f.message}
                         for f in result.findings],
            "failures": len(result.failures), "warnings": len(result.warnings),
            "window_days": args.days,
        }, indent=2, sort_keys=True))
        return 0 if result.ok else 1

    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(f"check-estate-flaps: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s) over {args.days:.0f} day(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
