#!/usr/bin/env python3
"""Put the receiver's retired mail domains back, and prove its check asserts them.

WHY THIS EXISTS
---------------
`mailjet-inbound-check` has one assertion with no other home: every name listed in
`RETIRED_DOMAINS` must answer **no** `MX`. It caught `signara.innotel.us` on 2026-11-11
(the name was a `CNAME` to the apex, so a query for its `MX` was answered with the
*parent's* and a retired domain kept advertising inbound routing). By 2026-11-16 that
setting had gone **empty**: `journalctl -u mailjet-inbound-check` said

    ok  retired  no retired domains declared, skipped

so the assertion that had found the problem was no longer running — and the unit reported
success either way. A check that skips is not a check that passes, and nothing in the
estate said which of the two it was.

The value lives in `/etc/oasis/mailjet-inbound-check.env`, which exists **only** on the
`mail` container: `/opt/oasis` there is plain files with no `git`, and no estate host
carries an `oasis` checkout, so an edit made on the box survives until the container is
rebuilt and then silently reverts to whatever shipped. This script is that setting's home
in a repo that a rebuild reads: it sets the list, runs the unit, and fails unless the
assertion actually ran.

WHAT IT DOES
------------
1. Reads `RETIRED_DOMAINS` from the container's env file. Nothing is rewritten when it
   already says the right thing; when it does not, the old file is kept beside it with a
   timestamp before it is replaced.
2. Starts `mailjet-inbound-check.service` and reads that run's own output back.
3. **Fails when the run says `skipped`.** An assertion that does not run is the failure
   this script exists for, not a pass with a shorter line.

The container has moved host four times, so the host is a flag rather than an assumption;
nothing here discovers it, because a wrong guess would edit a file on the wrong box.

Usage:
    SSHPASS=… ./scripts/restore-mailjet-retired-domains.py
    SSHPASS=… ./scripts/restore-mailjet-retired-domains.py --check      # change nothing
    SSHPASS=… ./scripts/restore-mailjet-retired-domains.py --host i1 --container mail

Exit codes: 0 = the list is in place and the unit asserted it, 1 = it is not,
2 = the container or its unit could not be read.
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import HostUnreadable, ssh  # noqa: E402

#: Names retired as mail domains. Adding one here is a claim that it must answer no `MX`;
#: taking one out is a claim that it is a mail domain again. `check-mail-auth.py` reads the
#: same list from the authoritative side, so the zone and the receiver disagree loudly
#: rather than quietly.
RETIRED: tuple[str, ...] = ("signara.innotel.us",)

ENV_FILE = "/etc/oasis/mailjet-inbound-check.env"
UNIT = "mailjet-inbound-check"
KEY = "RETIRED_DOMAINS"


def in_container(host: str, container: str, script: str, timeout: int = 90) -> str:
    """Run one shell line inside `container` on `host`.

    `</dev/null` is not decoration: `incus exec` without it swallows the rest of the caller's
    input, which has broken more than one hand-run here.
    """
    return ssh(host, f"incus exec {container} -- sh -c {shlex.quote(script)} </dev/null", timeout=timeout)


def read_setting(host: str, container: str) -> str | None:
    """The current value of `RETIRED_DOMAINS`, or None when the file declares no such key."""
    out = in_container(host, container, f"grep -h '^{KEY}=' {ENV_FILE} || true")
    for line in out.splitlines():
        if line.startswith(f"{KEY}="):
            return line.split("=", 1)[1].strip()
    return None


def write_setting(host: str, container: str, value: str) -> str:
    """Set the key in place, keeping a timestamped copy of the file it replaced."""
    script = (
        f"f={ENV_FILE}; cp \"$f\" \"$f.bak-$(date +%s)\"; "
        f"if grep -q '^{KEY}=' \"$f\"; then sed -i 's|^^{KEY}=.*|{KEY}={value}|' \"$f\"; "
        f"else printf '%s={value}\\n' '{KEY}' >> \"$f\"; fi; "
        f"grep -h '^{KEY}=' \"$f\""
    )
    return in_container(host, container, script)


def run_unit(host: str, container: str) -> tuple[str, str]:
    """Start the unit and return `(its show output, its own log lines)`."""
    show = in_container(
        host, container,
        f"systemctl reset-failed {UNIT} >/dev/null 2>&1; systemctl start {UNIT} >/dev/null 2>&1; "
        f"systemctl show -p Result -p ExecMainStatus {UNIT}",
    )
    log = in_container(host, container, f"journalctl -u {UNIT} -n 30 --no-pager")
    return show, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.1.51", help="host running the mail container (default i1)")
    parser.add_argument("--container", default="mail", help="container name (default mail)")
    parser.add_argument("--check", action="store_true", help="report what is set, change nothing, start nothing")
    args = parser.parse_args(argv)

    wanted = ",".join(RETIRED)
    try:
        current = read_setting(args.host, args.container)
        if args.check:
            print(f"restore-mailjet-retired-domains: {KEY}={current!r} (wanted {wanted!r})")
            if current == wanted:
                return 0
            print("  the receiver will skip its retired-domain assertion; run without --check", file=sys.stderr)
            return 1
        if current != wanted:
            print(f"  {KEY} was {current!r}; writing {wanted!r} (previous file kept as {ENV_FILE}.bak-<epoch>)")
            after = write_setting(args.host, args.container, wanted)
            if not after.strip().endswith(wanted):
                print(f"  write did not take: {after.strip()!r}", file=sys.stderr)
                return 1
        show, log = run_unit(args.host, args.container)
    except (HostUnreadable, OSError) as error:
        print(f"restore-mailjet-retired-domains: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 — ssh hands back CalledProcessError for a refused command
        print(f"restore-mailjet-retired-domains: {error.__class__.__name__}: {error}", file=sys.stderr)
        return 2

    for line in show.strip().splitlines():
        print(f"  {line.strip()}")
    retired = [line for line in log.splitlines() if re.search(r"\bretired\b", line)]
    for line in retired:
        print(line[line.find("python3"):] if "python3" in line else line)

    skipped = any("skipped" in line for line in retired)
    asserted = any(re.search(r"\bretired\b.*\bok\b|\bok\b.*\bretired\b", line) for line in retired)
    if skipped or not asserted:
        print("restore-mailjet-retired-domains: the unit did not assert the retired domains — "
              "an assertion that does not run is not a pass", file=sys.stderr)
        return 1
    if "Result=success" not in show:
        print("restore-mailjet-retired-domains: the unit did not finish successfully", file=sys.stderr)
        return 1
    print(f"restore-mailjet-retired-domains: {wanted} is declared and asserted unrouted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
