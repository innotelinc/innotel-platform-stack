#!/usr/bin/env python3
"""Publish the mail container's own preflight, every half hour.

WHY THIS EXISTS
---------------
`docs/mail-discovery-canonical.md` is an ordered apply whose steps are all cheap and
whose failures are all identical from the outside: Stalwart's start-up fetch is cached
as failed, every bearer token answers 500 while anonymous requests answer 200, and the
page says "Authentication Failed" after a correct sign-in. Step 1 exists so a person
sees the eleven things the change depends on *before* touching any of them.

`check-stalwart-oidc-cache.py` reads the copy: is it current, does it parse, does it
name the right issuer, can both users read their file. It is silent about everything
the apply can still get wrong — a leaf whose SAN was left behind by a rename, an anchor
about to expire, a DNAT that was never installed, a provider that has stopped answering
from inside the container. Those are exactly the eleven facts in the preflight, so this
check runs that preflight rather than reimplementing it.

THE POINT IS THAT THERE IS ONE COPY OF THE RULES
------------------------------------------------
The script is streamed into the container and executed there (`bash /dev/stdin
preflight`), so the verdict published as a series is the verdict the runbook gives a
person, byte for byte. Nothing is installed on the container, no path is guessed, and a
new fact added to the runbook's preflight arrives here without an edit.

The script's own guard is what makes the answer trustworthy: it refuses unless
`/etc/stalwart/stalwart.env` and `stalwart.service` are both present, so a container that
merely looks like the mail server is reported as *could not run* rather than as a pass.
That is the same correction `cba7784` made to the preflight itself, one layer out.

READ-ONLY
---------
`preflight` writes nothing, restarts nothing and refreshes nothing; this check only
streams it in and reads its output. Exit 2 means the question could not be asked.

Usage:
    ./scripts/check-stalwart-oidc-preflight.py
    ./scripts/check-stalwart-oidc-preflight.py --prom /path/to/textfile
    ./scripts/check-stalwart-oidc-preflight.py --host root@192.168.1.51 --container mail

Exit codes: 0 = every fact holds, 1 = at least one does not, 2 = could not run.
"""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, ssh_prefix, write_prom  # noqa: E402

CHECK = "stalwart_oidc_preflight"

#: The host the mail container runs on and the container itself (`i1`, per the container
#: address table). The unit file that starts this check is the authority for both. It read
#: `i2` until 2026-11-14: `incus exec mail` there finds no instance, so every run ended at
#: exit 2 and the preflight never actually ran.
HOST = "root@192.168.1.51"
CONTAINER = "mail"

#: The preflight is not installed anywhere on the container — the setup script is run from
#: wherever an operator put it — so this check carries its own copy of the file it ships
#: beside: `scripts/` is what `/opt/innotel/estate-checks/` is filled from.
SCRIPT = Path(__file__).resolve().with_name("stalwart-oidc-discovery-setup.sh")

#: Two hops, one ssh channel and a handful of `curl`s; the preflight's own probes are
#: bounded at 5–10 s each, so this is generous without holding the unit hostage.
TIMEOUT_SECONDS = 180

#: ssh's own failure code: not a verdict about the mail server.
SSH_FAILURE = 255

#: One line of the preflight: `  ok   <label>  <value>` or `  FAIL <label>  <value>`.
#: The label is padded to 22 columns and separated by two spaces, so the split is on two
#: or more spaces — never on the first one, or a label with a space in it ("leaf covers")
#: would be cut in half.
LINE = re.compile(r"^\s{2}(ok|FAIL)\s+(\S.*?)\s{2,}(.*)$")
VERDICT = re.compile(r"^PREFLIGHT (OK|NOT OK)\b", re.MULTILINE)


@dataclass
class Facts:
    """What the container's preflight said, before any judgement about it."""

    lines: list[tuple[bool, str, str]] = field(default_factory=list)
    verdict: str | None = None

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(label, value) for ok, label, value in self.lines if not ok]


@dataclass
class Result:
    """The far side's exit status and output."""

    code: int
    stdout: str = ""
    stderr: str = ""


def parse(output: str) -> Facts:
    """Turn the preflight's report into facts. Pure, so the rules below are testable.

    A line that is not one of the markers is section furniture (`units`, `the pieces`)
    and carries no verdict. The script's own summary line is kept, because it is what
    says whether the run reached the end: a report with no verdict is a script that
    stopped halfway and must not read as a pass.
    """
    facts = Facts()
    for line in output.splitlines():
        match = VERDICT.match(line)
        if match:
            facts.verdict = match.group(1)
            continue
        match = LINE.match(line)
        if match:
            ok, label, value = match.groups()
            facts.lines.append((ok == "ok", label.strip(), value.strip()))
    return facts


def code_for(label: str) -> str:
    """A finding code from a label: `leaf covers` → `leaf_covers`."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or "preflight"


def audit(facts: Facts) -> Audit:
    """Read the facts as findings: one per line that does not hold."""
    out = Audit()
    for label, value in facts.failures:
        out.findings.append(Finding(code_for(label), "fail", f"{label}: {value}"))

    if facts.verdict == "NOT OK" and not out.findings:
        # The run said the switch must not go ahead and named nothing: that is a bug in
        # the preflight, and it must still be a failure rather than a quiet green.
        out.findings.append(
            Finding("verdict_without_reason", "fail",
                    "the preflight said NOT OK but marked no line, so nothing can be fixed from it")
        )
    if not out.findings:
        out.findings.append(
            Finding("pieces_intact", "note",
                    f"{len(facts.lines)} facts read, all as the runbook's step 1 expects")
        )
    return out


def run_preflight(script: str, host: str = HOST, container: str = CONTAINER,
                  timeout: int = TIMEOUT_SECONDS) -> Result:
    """Stream the setup script into the container and run its `preflight`.

    The script is written to a file inside the container and run from it, rather than
    streamed to `bash /dev/stdin`: the script resolves its own directory through
    `BASH_SOURCE[0]`, so it needs a real path (`bash -s` leaves that unset and `set -u`
    fails before a fact is read), and on i1 `bash /dev/stdin` is refused outright —
    `bash: /dev/stdin: Permission denied` — which this check reported as exit 126 for as
    long as it pointed at the wrong host. The temporary file is removed before returning.
    """
    remote = (
        f"incus exec {shlex.quote(container)} -- sh -c "
        "'cat > /tmp/stalwart-preflight.sh && bash /tmp/stalwart-preflight.sh preflight; "
        "rc=$?; rm -f /tmp/stalwart-preflight.sh; exit $rc'"
    )
    completed = subprocess.run(
        [*ssh_prefix(), "-o", "ConnectTimeout=25", host, remote],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--host", metavar="TARGET", default=HOST, help="ssh target of the host running the container")
    parser.add_argument("--container", metavar="NAME", default=CONTAINER, help="container holding the mail server")
    parser.add_argument("--script", metavar="PATH", default=str(SCRIPT),
                        help="the setup script whose preflight is run (default: the one beside this check)")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-stalwart-oidc-preflight: could not write {args.prom}: {error}", file=sys.stderr)

    try:
        script = Path(args.script).read_text(encoding="utf-8")
    except OSError as error:
        print(f"check-stalwart-oidc-preflight: cannot read the setup script beside this check "
              f"({error}); deploy `scripts/` with the check, not only `check-*.py`", file=sys.stderr)
        emit(None, ran=False)
        return 2

    if shutil.which("ssh") is None:
        print("check-stalwart-oidc-preflight: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        result = run_preflight(script, args.host, args.container)
    except Exception as error:  # noqa: BLE001 — any failure here is "could not run"
        print(f"check-stalwart-oidc-preflight: could not reach {args.host}: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    detail = " ".join((result.stderr or result.stdout).split())[-300:]

    if result.code in (SSH_FAILURE, 2):
        # 255 is ssh; 2 is the preflight's own refusal. Neither is a verdict about the
        # pieces, and both must page as "could not run" rather than as a failure of the
        # mail server — the container not being the mail server is a placement problem.
        why = "could not reach" if result.code == SSH_FAILURE else "could not run the preflight in"
        print(f"check-stalwart-oidc-preflight: {why} {args.container} on {args.host}: "
              f"{detail or 'no output'}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    if result.code not in (0, 1):
        print(f"check-stalwart-oidc-preflight: the preflight exited {result.code}: "
              f"{detail or 'no output'}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    facts = parse(result.stdout)
    if facts.verdict is None:
        print(f"check-stalwart-oidc-preflight: {args.container} did not finish the preflight "
              f"(no verdict line): {detail or 'no output'}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    report = audit(facts)
    emit(report, ran=True)
    for level in ("fail", "note"):
        for finding in [f for f in report.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    print(f"\ncheck-stalwart-oidc-preflight: {len(report.failures)} failure(s), "
          f"{len(facts.lines)} fact(s) read")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
