#!/usr/bin/env python3
"""Run the estate checks side by side instead of one after another.

WHY THIS EXISTS
---------------
`systemd/estate-checks.service` is a `Type=oneshot` unit with one `ExecStart=` per check, and
systemd runs those strictly in order. Almost all of that time is *waiting*, not work: each
check asks a host, a container runtime, a provider or a DNS resolver something over the
network, so the unit sat at 17.7 s of CPU for 254 s of wall (measured 2026-11-18 on the
edge). Two of the checks alone (a 300-host sweep and a latency matrix) account for a
quarter of it, and every one of them spends its time blocked on a socket.

The obvious fix — start them together — cannot be a `ExecStart=` edit, because the unit's
`ExecStart=` lines are the estate's *install inventory*: `install-estate-checks.sh` derives
the files it copies from them, and `check-installed-checks.py` derives the files it holds to
`origin/main` the same way. A unit that names one orchestrator would install one
orchestrator. So the lines stay exactly where they are, and each one now *submits* its
command here instead of running it; one final line drains the queue.

    ExecStart=-/usr/bin/python3 …/run-estate-checks.py add /usr/bin/python3 …/check-one.py …
    ExecStart=-/usr/bin/python3 …/run-estate-checks.py drain --jobs 6

The queue is a directory per run, keyed by systemd's `INVOCATION_ID`: the submits of one
`systemctl start` share it, two runs cannot see each other's list, and a run that was killed
mid-drain leaves a directory that the next submit prunes rather than replays.

WHAT IT IS NOT
--------------
It does not change what a check does, how it reports, or which textfile it writes — the
series, the alert rules and `check-installed-checks.py` all read the same files as before.
It does not decide that a failed check failed the unit: the finding is the textfile (see the
unit's own comment), so `drain` prints the verdicts to the journal and exits 0, exactly as
the `-` on every check line already meant. And it does not raise the concurrency ceiling on
its own: `--jobs` is set by the unit, because every check is a process on a 2-vCPU box that
also runs the estate.

Exit codes: 0 = the queue ran (whatever the checks said), 2 = the queue itself could not be
built or read, which is a deploy problem rather than an estate one.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

#: Where the submits and the drain meet. `/run` because the list is worthless after a
#: reboot and a reboot is the one time a leftover would be replayed.
DEFAULT_SPOOL = "/run/estate-checks"
#: Checks running together. The edge is 2 vCPU and also carries 24 containers; the point is
#: to overlap the waiting, not to fork the whole estate at once.
DEFAULT_JOBS = 6
#: One check's ceiling. The slowest honest one measured 54 s; `check-sso-redirects` carries
#: its own `--deadline 420`, so this only catches something that is genuinely wedged.
DEFAULT_TIMEOUT = 900
#: A run directory another run never drained is stale after this long. Long enough that it
#: cannot be a run in progress, short enough that a `systemctl restart` mid-drain does not
#: accumulate them.
STALE_SECONDS = 600


def spool_root() -> Path:
    return Path(os.environ.get("ESTATE_CHECK_SPOOL") or DEFAULT_SPOOL)


def run_id() -> str:
    """This run's directory name.

    `INVOCATION_ID` is systemd's own per-start identifier, which is what makes the submits
    of one `systemctl start` distinguishable from the next one's. Outside systemd (a person
    running the two subcommands by hand) the parent pid stands in, which is unique enough
    for a queue that is drained by that same parent.
    """
    return os.environ.get("INVOCATION_ID") or f"manual-{os.getppid()}"


def run_dir() -> Path:
    return spool_root() / run_id()


def prune(root: Path, keep: Path) -> list[str]:
    """Drop run directories that no drain will ever read.

    Only ones older than `STALE_SECONDS`: a directory being filled *right now* belongs to a
    run whose drain has not started, and deleting it would silently drop checks from that
    run's report. That is the failure this function must not cause.
    """
    dropped: list[str] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return dropped
    now = time.time()
    for entry in entries:
        if entry == keep or not entry.is_dir():
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < STALE_SECONDS:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        dropped.append(entry.name)
    return dropped


def add(argv: list[str]) -> int:
    """Record one command for the drain that follows. Returns 2 if it could not."""
    if not argv:
        print("run-estate-checks: add needs a command", file=sys.stderr)
        return 2
    directory = run_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        prune(directory.parent, directory)
    except OSError as error:
        print(f"run-estate-checks: cannot queue in {directory}: {error}", file=sys.stderr)
        return 2
    # O_EXCL rather than a counter read from the directory: a submit that failed to write
    # must not consume a slot, or a check would vanish without anything saying so.
    index = 0
    while True:
        path = directory / f"{index:04d}.json"
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            index += 1
            continue
        except OSError as error:
            print(f"run-estate-checks: cannot queue {argv[0]}: {error}", file=sys.stderr)
            return 2
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"argv": argv}, stream)
        return 0


def label(argv: list[str]) -> str:
    """The script's own name, which is what a person reading the journal is looking for."""
    for argument in argv:
        name = os.path.basename(argument)
        if name.startswith("check-"):
            return name
    return os.path.basename(argv[0])


def run_one(argv: list[str], timeout: float) -> tuple[str, str, int | None, str, float]:
    started = time.monotonic()
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)
        status = "ok" if done.returncode == 0 else "failed"
        return label(argv), status, done.returncode, (done.stdout or "") + (done.stderr or ""), time.monotonic() - started
    except subprocess.TimeoutExpired:
        return label(argv), "timeout", None, "", time.monotonic() - started
    except OSError as error:
        return label(argv), "error", None, str(error), time.monotonic() - started


def tail(text: str, lines: int = 6) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(f"    {line}" for line in kept[-lines:])


def drain(jobs: int, timeout: float) -> int:
    directory = run_dir()
    if not directory.is_dir():
        print("run-estate-checks: nothing was queued, so there is nothing to drain "
              f"({directory})", file=sys.stderr)
        return 2
    try:
        files = sorted(directory.glob("*.json"))
        queued = [json.loads(path.read_text(encoding="utf-8"))["argv"] for path in files]
    except (OSError, ValueError, KeyError) as error:
        print(f"run-estate-checks: cannot read the queue in {directory}: {error}", file=sys.stderr)
        return 2
    if not queued:
        print("run-estate-checks: the queue is empty", file=sys.stderr)
        shutil.rmtree(directory, ignore_errors=True)
        return 2

    print(f"run-estate-checks: {len(queued)} check(s), {jobs} at a time", flush=True)
    started = time.monotonic()
    failures: list[tuple[str, str, str]] = []
    lock = threading.Lock()
    finished = 0

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(run_one, argv, timeout): argv for argv in queued}
        for future in as_completed(futures):
            name, status, code, output, seconds = future.result()
            with lock:
                finished += 1
                suffix = f"  rc={code}" if code not in (0, None) else ""
                print(f"[{finished:>2}/{len(queued)}] {name:<38} {seconds:6.1f}s  {status}{suffix}",
                      flush=True)
                if status != "ok":
                    failures.append((name, status, output))

    elapsed = time.monotonic() - started
    for name, status, output in failures:
        print(f"run-estate-checks: {status}: {name}", flush=True)
        if output.strip():
            print(tail(output), flush=True)
    print(f"run-estate-checks: {len(queued)} check(s) in {elapsed:.1f}s "
          f"({len(queued) - len(failures)} clean, {len(failures)} not)", flush=True)
    # The verdicts are the textfiles; a check being unhappy is not this unit failing. That
    # is what the `-` on every ExecStart has always meant, and flattening it here would page
    # twice for one finding.
    shutil.rmtree(directory, ignore_errors=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Queue the estate checks and run them side by side.")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("add", help="record one command for the drain that follows")
    submit.add_argument("argv", nargs=argparse.REMAINDER,
                        help="the command, exactly as the unit would have run it")

    finish = sub.add_parser("drain", help="run everything queued for this invocation")
    finish.add_argument("--jobs", type=int, default=int(os.environ.get("ESTATE_CHECK_JOBS") or DEFAULT_JOBS),
                        help=f"checks running together (default {DEFAULT_JOBS})")
    finish.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"one check's ceiling in seconds (default {DEFAULT_TIMEOUT})")

    parsed = parser.parse_args(argv)
    if parsed.command == "add":
        return add([argument for argument in parsed.argv if argument != "--"])
    if parsed.jobs < 1:
        print("run-estate-checks: --jobs must be at least 1", file=sys.stderr)
        return 2
    return drain(parsed.jobs, parsed.timeout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
