#!/usr/bin/env python3
"""Publish the verdict of the mailbox -> identity mirror, so it can retry without buzzing.

WHY THIS EXISTS
---------------
`sync-mail-identities.service` keeps every Stalwart mailbox and its Authentik identity
keyed by the same address (`docs/sso-cleanup-2026-11-02.md`). It runs every fifteen
minutes and is deliberately **not** wired to `OnFailure=` (`docs/estate-alerts-ntfy.md`):
wiring it would put the same message on the ntfy topic 96 times a day for as long as the
cause lasted, which is the wallpaper `docs/incus-ceiling-check.md` is about.

That reasoning was right and the consequence was wrong. A mirror that had been failing
since a token expired would retry quietly forever and nothing would ever say so — the only
record was a journal line the next run overwrote. A job that retries on its own needs a
**series**, not a notification: one reading that is true now, however many runs it took to
arrive at it. This publishes that reading in the family every other estate check writes
(`scripts/estate_check.py`), so the same rules page on it as on the rest.

WHAT IT READS
-------------
The timer runs *inside* the `mail` container, which has sshd but not the key these checks
hold, so it is read the way `check-alert-delivery.py` and `check-stalwart-oidc-cache.py`
read `mail`: ssh to the incus host, then `incus exec`. Inside, five facts, and four of them
are systemd's own rather than an inference from a log:

* whether the timer is active and enabled — a mirror with nothing scheduling it is not
  failing, it is stopped, which looks identical on the mail server;
* the last run's `Result` and `ExecMainStatus` — exit-code 1 is a create Authentik
  refused **and** also what the script exits with when it fails outright
* when that run finished, which is the number that says whether it is *still* running.
* which mailboxes that run named, so a failure says **which** address has no identity and
  not only how many. That one is read from the run's own log, because an exit code cannot
  carry an address: the script prints `  MISSING <address>` for what `--apply` would create
  and `  FAILED <address>: <status>` for what Authentik refused.

THE RULES
---------
1. A last run that did not succeed is a **failure**, naming the status and what it costs.
   It stays a failure for exactly as long as the failing run is the last one, so a single
   bad run between two good ones does not page twice.
2. A timer that is inactive or disabled is a **failure** — that is the state in which
   nothing is ever noticed.
3. A last run more than two intervals ago is a **failure**: the timer is not firing.
4. A clean run states the run's age as a **note**, so "ok" says how recent the answer is.
5. An unreachable host or container is **exit 2** — "could not read it" is never a pass.
6. A failed run that named mailboxes names them in the finding **and** publishes one
   series per mailbox, so the page and the metric both say which address, not how many.

READ-ONLY
---------
Nothing is triggered, retried or written here. The sync belongs to its own timer.

Usage:
    ./scripts/check-mail-identities.py
    ./scripts/check-mail-identities.py --prom /path/to/textfile
    ./scripts/check-mail-identities.py --host root@192.168.1.51 --container mail

Exit codes: 0 = the last run succeeded and the timer is live, 1 = one of those is not true,
2 = could not run.
"""
from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, ssh_prefix, write_prom  # noqa: E402

CHECK = "mail_identities"

#: The host the mail container runs on (`i2`, per the container address table) and the
#: container itself. The unit that starts this check is the authority for both.
HOST = "root@192.168.1.51"
CONTAINER = "mail"

UNIT = "sync-mail-identities.service"
TIMER = "sync-mail-identities.timer"

#: The timer fires every fifteen minutes. Two missed runs and a grace for the oneshot's
#: own 180 s `TimeoutStartSec` is a third of an hour: past that it is not slow, it is off.
INTERVAL_SECONDS = 15 * 60
STALE_SECONDS = 2 * INTERVAL_SECONDS + 5 * 60

#: What the script's own exit codes mean, so the finding says what to fix rather than a
#: number (`scripts/sync-mail-identities.py`, "Exit codes").
STATUS_MEANING = {
    0: "it exited cleanly",
    1: "`--apply` was refused for at least one user, so that mailbox may still have no "
       "identity it can sign in with — though 1 is also what the script itself exits with "
       "when it fails outright, so the logged line below says which",
    2: "an estate API could not be read — the Stalwart admin or the Authentik token",
}

#: The separator the remote script joins the last run's named mailboxes with. A line such
#: as `FAILED a@example.com: 400 …` contains the same colon the report's own lines use, so
#: the lines are folded into ESC to cross ssh and incus instead of relying on newlines
#: surviving a `$( … )` in `sh`.
LOG_SEPARATOR = "\x1f"

#: What the mirror prefixes an address with: `MISSING` is the plan `--apply` works from,
#: `FAILED` is a creation Authentik refused (`scripts/sync-mail-identities.py`, `main`).
NAMED_PREFIXES = {"MISSING": "planned", "FAILED": "refused"}

#: Two hops and a handful of `systemctl show`s; a hung ssh must not hold the unit.
TIMEOUT_SECONDS = 120

#: ssh's own failure code. Not a verdict about the mirror, so it must read as exit 2.
SSH_FAILURE = 255

#: Runs inside the container. Raw values are printed rather than interpreted on the far
#: side, so the parsing — and every rule about it — lives here, where the tests reach it.
REMOTE = f"""
set -u
printf 'timer_active:%s\\n' "$(systemctl is-active {TIMER} 2>/dev/null || echo unknown)"
printf 'timer_enabled:%s\\n' "$(systemctl is-enabled {TIMER} 2>/dev/null || echo unknown)"
printf 'result:%s\\n' "$(systemctl show -p Result --value {UNIT} 2>/dev/null || echo unknown)"
printf 'status:%s\\n' "$(systemctl show -p ExecMainStatus --value {UNIT} 2>/dev/null || echo unknown)"
# The last line the unit logged. Exit 1 is `--apply` refusing a user *and* what the script
# exits with when it crashes, and on 2026-11-16 it was a read timeout that this check
# reported as a refused user who had in fact already been mirrored. The line is printed
# raw and trimmed here, so the finding names the cause the container actually recorded.
printf 'message:%s\\n' "$(journalctl -u {UNIT} -n 1 -o cat --no-pager 2>/dev/null | tr '\\n' ' ' | cut -c1-300)"
# The mailbox(es) the last run named, so a failure can say *which* one rather than only
# that some run did not finish. The mirror prints `  MISSING <address>` for the users
# `--apply` would create and `  FAILED <address>: <status>` for the ones Authentik refused;
# both are its own formats (`scripts/sync-mail-identities.py`, main()). Only the lines
# naming an address are sent — the rest of the log is what `message:` above already quotes
# — and they are joined with ESC, because a log line can itself contain a colon.
inv="$(systemctl show -p InvocationID --value {UNIT} 2>/dev/null || true)"
printf 'invocation:%s\\n' "$inv"
if [ -n "$inv" ]; then
  printf 'unmirrored:%s\\n' "$(journalctl -u {UNIT} _SYSTEMD_INVOCATION_ID="$inv" -o cat --no-pager 2>/dev/null | grep -E '^ +(MISSING|FAILED) ' | tr '\\n' '\\037' | cut -c1-4000)"
fi
# `ExecMainExitTimestamp` is empty until the unit has run once, which is a different fact
# from "it ran at an unreadable time" and must not be flattened into it.
last="$(systemctl show -p ExecMainExitTimestamp --value {UNIT} 2>/dev/null || true)"
if [ -z "$last" ]; then
  printf 'last:never\\n'
elif epoch="$(date -d "$last" +%s 2>/dev/null)"; then
  printf 'last:%s\\n' "$epoch"
else
  printf 'last:unreadable\\n'
fi
printf 'now:%s\\n' "$(date +%s)"
"""


@dataclass
class Facts:
    """What the container said, before any judgement about it."""

    timer_active: str = "unknown"
    timer_enabled: str = "unknown"
    result: str = "unknown"
    status: int | None = None
    #: The last line the unit logged, so a failed run is named rather than guessed at
    #: from its exit code (see `STATUS_MEANING`). Empty when the unit logged nothing.
    last_message: str = ""
    #: Addresses the last run said it would create, and the ones Authentik refused. They
    #: come from that run's own log, so they are what it named, not what should be true.
    planned: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    last: int | None = None
    last_unreadable: bool = False
    now: int = 0

    @property
    def age_seconds(self) -> int | None:
        """How long ago the last run finished, or None when there is no time to subtract."""
        if self.last is None or not self.now:
            return None
        return max(0, self.now - self.last)

    @property
    def unmirrored(self) -> list[str]:
        """Addresses still without an identity: the refused ones, then the merely planned."""
        return self.refused + [address for address in self.planned if address not in self.refused]


@dataclass
class Result:
    """The container's exit status and output."""

    code: int
    stdout: str = ""
    stderr: str = ""


def probe(host: str = HOST, container: str = CONTAINER, timeout: int = TIMEOUT_SECONDS) -> Result:
    """Read the container's units over the estate's ssh path, then `incus exec`."""
    remote = f"incus exec {shlex.quote(container)} -- sh -c {shlex.quote(REMOTE)}"
    completed = subprocess.run(
        [*ssh_prefix(), "-o", "ConnectTimeout=25", host, remote],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def named_mailboxes(report: str) -> tuple[list[str], list[str]]:
    """The addresses the last run named, as (planned, refused).

    Order follows the log and duplicates are dropped, so a finding reads like the run it
    describes. Only lines that name an address count: `CREATED …` is not a gap, whatever
    else the run printed.
    """
    planned: list[str] = []
    refused: list[str] = []
    for line in report.split(LOG_SEPARATOR):
        parts = line.split()
        if len(parts) < 2:
            continue
        which = NAMED_PREFIXES.get(parts[0])
        address = parts[1].rstrip(":,")
        if which is None or "@" not in address:
            continue
        bucket = planned if which == "planned" else refused
        if address not in bucket:
            bucket.append(address)
    return planned, refused


def parse(output: str) -> Facts:
    """Turn the container's report into facts. Pure, so the rules below are testable."""
    facts = Facts()
    for line in output.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "timer_active":
            facts.timer_active = value or "unknown"
        elif key == "timer_enabled":
            facts.timer_enabled = value or "unknown"
        elif key == "result":
            facts.result = value or "unknown"
        elif key == "status":
            facts.status = int(value) if value.lstrip("-").isdigit() else None
        elif key == "message":
            facts.last_message = value
        elif key == "unmirrored":
            facts.planned, facts.refused = named_mailboxes(value)
        elif key == "last":
            if value.isdigit():
                facts.last = int(value)
            elif value == "unreadable":
                facts.last_unreadable = True
        elif key == "now":
            facts.now = int(value) if value.isdigit() else 0
    return facts


def audit(facts: Facts) -> Audit:
    """Read the facts as findings: what is wrong, and what it costs."""
    out = Audit()

    if facts.timer_active != "active":
        out.findings.append(
            Finding(
                "timer_inactive",
                "fail",
                f"{TIMER} is {facts.timer_active}: nothing is mirroring mailboxes into "
                "Authentik, so a new mailbox has no identity to sign in with and the "
                "difference only shows up as somebody unable to log in",
            )
        )
    if facts.timer_enabled != "enabled":
        out.findings.append(
            Finding(
                "timer_disabled",
                "fail",
                f"{TIMER} is {facts.timer_enabled}: it is running now but will not come "
                "back after a reboot",
            )
        )

    if facts.result != "success":
        meaning = STATUS_MEANING.get(facts.status, "no exit status was recorded")
        # What the container itself recorded, quoted, so the finding leads with the cause
        # instead of asserting the one `--apply` refusal an exit code cannot distinguish.
        logged = f" — the last line it logged was {facts.last_message!r}" if facts.last_message else ""
        if facts.unmirrored:
            # Named by the run itself, so the finding says which address rather than only
            # that one of them is unmirrored. On 2026-11-16 there were none to name: the run
            # had crashed before it could, which is why the absence is not turned into one.
            logged += " — it named these mailboxes with no identity: " + ", ".join(facts.unmirrored)
        out.findings.append(
            Finding(
                "last_run_failed",
                "fail",
                f"the last run ended {facts.result}"
                + (f" ({facts.status})" if facts.status is not None else "")
                + f", and {meaning}{logged}",
            )
        )

    if facts.last_unreadable:
        out.findings.append(
            Finding(
                "last_run_unreadable",
                "fail",
                "the last run's finish time could not be read, so an ageing mirror cannot "
                "be told apart from a stopping one",
            )
        )
    age = facts.age_seconds
    if age is None and not facts.last_unreadable:
        out.findings.append(
            Finding(
                "never_ran",
                "fail",
                f"{UNIT} has not run at all: the timer is installed and the mirror never "
                "happened",
            )
        )
    elif age is not None:
        if age > STALE_SECONDS:
            out.findings.append(
                Finding(
                    "not_firing",
                    "fail",
                    f"the last run finished {age} s ago, past two {INTERVAL_SECONDS} s "
                    "intervals: the timer is not firing",
                )
            )
        elif facts.result == "success":
            out.findings.append(
                Finding(
                    "in_sync",
                    "note",
                    f"the last run succeeded {age} s ago and the timer fires every "
                    f"{INTERVAL_SECONDS} s",
                )
            )
    return out


def metric_age(facts: Facts) -> str | None:
    """The run's age as its own series, so a rising number is visible before it fails."""
    age = facts.age_seconds
    if age is None:
        return None
    return (
        "# HELP innotel_estate_check_mail_identities_age_seconds How long ago the last "
        "mailbox->identity mirror run finished\n"
        "# TYPE innotel_estate_check_mail_identities_age_seconds gauge\n"
        f'innotel_estate_check_mail_identities_age_seconds{{check="{CHECK}"}} {age}\n'
    )


def _label(value: str) -> str:
    """Escape a label value the way the textfile format requires."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metric_unmirrored(facts: Facts) -> str | None:
    """One series per mailbox still without an identity, so the alert can name it.

    Published only when the last run did not succeed. A clean `--apply` run prints the
    addresses it created and the ones it planned, so its `MISSING`/`CREATED` lines are the
    opposite of a gap — reporting them would page on the fix.
    """
    if facts.result == "success" or not facts.unmirrored:
        return None
    head = (
        "# HELP innotel_estate_check_mail_identities_unmirrored Mailboxes the last mirror "
        "run named as having no Authentik identity\n"
        "# TYPE innotel_estate_check_mail_identities_unmirrored gauge\n"
    )
    rows = "".join(
        f'innotel_estate_check_mail_identities_unmirrored{{check="{CHECK}",'
        f'mailbox="{_label(address)}"}} 1\n'
        for address in facts.unmirrored
    )
    return head + rows


def metrics(facts: Facts) -> str | None:
    """Every series this check adds beside the shared family's own."""
    parts = [part for part in (metric_age(facts), metric_unmirrored(facts)) if part]
    return "".join(parts) or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--host", metavar="TARGET", default=HOST, help="ssh target of the host running the container")
    parser.add_argument("--container", metavar="NAME", default=CONTAINER, help="container holding the mail server")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool, facts: Facts | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=metrics(facts) if facts else None)
            except OSError as error:
                print(f"check-mail-identities: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-mail-identities: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        result = probe(args.host, args.container)
    except Exception as error:  # noqa: BLE001 — any failure here is "could not run"
        print(f"check-mail-identities: could not reach {args.host}: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    if result.code != 0:
        detail = " ".join(result.stderr.split())[-300:] or "no output"
        why = "could not reach" if result.code == SSH_FAILURE else f"the read exited {result.code}"
        print(f"check-mail-identities: {why} {args.host}: {detail}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    facts = parse(result.stdout)
    if not facts.now:
        # No `now` means the remote script did not run to the end, so nothing after it is
        # a verdict about the mirror.
        print(f"check-mail-identities: {args.container} did not finish reporting", file=sys.stderr)
        emit(None, ran=False)
        return 2

    report = audit(facts)
    emit(report, ran=True, facts=facts)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in report.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(f"\ncheck-mail-identities: {len(report.failures)} failure(s)")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
