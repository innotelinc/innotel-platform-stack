#!/usr/bin/env python3
"""Fail when the Scans share stops being one writer's and everybody else's to read.

WHY THIS EXISTS
---------------
On 2026-11-19 the household's read-only access to `Scans` was granted and verified on the
*filesystem*: `household` could read `scan.pdf` and `scans` could write a probe. That proved
the modes and the group, and nothing about Samba's own half. The share is
`read only = yes` + `write list = scans`, so the rule that matters — "everybody in
`valid users` reads, only `scans` writes" — is enforced by smbd from a stanza, and a stanza
is a file. Changing `write list`, dropping `read only`, or leaving `scans` out of
`valid users` would keep every filesystem fact true and silently hand a reader the write
bit, and the only symptom is a file nobody meant to overwrite.

Measured, the same day: the printer does not write to this share at all — its scans reach
it from HP-NOTEBOOK (Orion's `share-clients.json` names that client), so `scans` writes over
real SMB and a refused second writer is the whole guarantee. This check is that guarantee
exercised rather than assumed.

THE RULES
---------
1. **The stanza** — `testparm` on the container reports `read only = Yes`, and
   `write list`/`valid users` name the accounts the environment file declares. A share
   whose config cannot be read is exit 2, not a pass.
2. **The writer** — signs in over SMB, lists, writes a probe, and the bytes on disk land
   `-rw-rw----` in the share's `force group` (the masks the household's clients rely on).
   The probe is deleted again, and a probe left behind is a failure: it is litter the next
   run would trip over.
3. **The reader** — with `SCAN_SHARE_READ_USER`/`SCAN_SHARE_READ_PASSWORD` configured,
   signs in over SMB, lists, reads the writer's probe back byte for byte, and is **refused**
   a write. A reader who can write is the regression this exists for, and the series says
   which account. The estate's reader is `scan-reader` (created 2026-11-19): a real Unix and
   Samba account in `orion-nas`, in `[Scans]`'s `valid users` and deliberately *not* in its
   `write list`. It is the account the household's own access is exercised through, because
   no check can mint a household password and a refused write is the same rule either way.
4. **Unproven, said out loud** — without those two variables the reader half is published
   as a *note* naming exactly what is unasserted. Making it a failure would leave the
   estate permanently red for a credential no check can mint, which is how a check gets
   ignored; the note is the estate's existing way of stating a true thing it cannot fail on.

The password travels on stdin, never in `argv`: the probe script is streamed through
`ssh … incus exec … bash -s`, so no process ever shows the credential in its command line,
and no temporary credentials file is written on either side.

Usage:
    ./scripts/check-scan-share.py
    ./scripts/check-scan-share.py --prom /path/to/textfile
    SCAN_SHARE_PASSWORD='…' ./scripts/check-scan-share.py

Exit codes: 0 = the share is one writer's and read-only for the rest, 1 = it is not,
2 = the check could not run (never silently a pass).
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, HostUnreadable, ssh_prefix, write_prom  # noqa: E402

CHECK = "scan_share"

#: `nas` runs on `i2` and is reached through it, the same hop the other container checks
#: use. The share is exercised from inside the container, where `smbclient` lives and where
#: 127.0.0.1 is smbd itself.
HOST_NAME = "i2"
HOST = "root@192.168.1.52"
CONTAINER = "nas"

SHARE = "Scans"
SHARE_PATH = "/srv/storage/Users/scans"
PROBE = "estate-check-probe.txt"

#: What a writer's probe has to look like on disk: the masks `[Scans]` declares, in the
#: group it forces, with no bits for "other" at all.
PROBE_MODE = "-rw-rw----"

#: `ls -l`'s permission column. A failed `ls` prints a sentence, whose words would
#: otherwise parse as a mode, an owner and a group — measured, and it reported a group
#: mismatch for a probe that was simply not written.
MODE = re.compile(r"[-dlbcps][rwxsStT-]{9}")


def build_probe(password: str, reader_user: str = "", reader_password: str = "") -> str:
    """The shell the container runs. Pure, so the probes can be read and tested.

    Only the passwords are interpolated, through `shlex.quote`; everything else is literal.
    `smbclient` is fed the password on stdin so it never reaches a command line, and the
    `Password for [WORKGROUP\\…]` prompt it prints is dropped so it cannot be mistaken for
    a finding.
    """
    script = f"""set -u
PW={shlex.quote(password)}
RU={shlex.quote(reader_user)}
RW={shlex.quote(reader_password)}
SHARE={shlex.quote(SHARE_PATH)}
SMB={shlex.quote(f"//127.0.0.1/{SHARE}")}
PROBE={shlex.quote(PROBE)}

smb() {{
  printf '%s\\n' "$2" | smbclient "$SMB" -U "$1" -c "$3" 2>&1 | grep -v '^Password for '
  # Index 1, not 0: `PIPESTATUS[0]` is the `printf` that feeds the password, which
  # succeeds whenever there is a password to write — measured, it reported the reader's
  # refused write as a success and the check failed the estate for doing the right thing.
  return "${{PIPESTATUS[1]}}"
}}

echo "@@cfg_read_only"
testparm -s --section-name={SHARE} --parameter-name="read only" 2>/dev/null
echo "@@cfg_write_list"
testparm -s --section-name={SHARE} --parameter-name="write list" 2>/dev/null
echo "@@cfg_valid_users"
testparm -s --section-name={SHARE} --parameter-name="valid users" 2>/dev/null
echo "@@cfg_group_members"
# `@group` is how Orion's generator names a share's audience, and smbd expands it.
# Read literally it would fail every account the group admits, so the group is
# resolved here, where the accounts actually live.
for token in $(testparm -s --section-name={SHARE} --parameter-name="valid users" 2>/dev/null | tr ',' ' '); do
  case "$token" in
    @*) getent group "${{token#@}}" 2>/dev/null | cut -d: -f4 | tr ',' ' ' ;;
  esac
done
echo "@@cfg_force_group"
testparm -s --section-name={SHARE} --parameter-name="force group" 2>/dev/null
echo "@@cfg_path"
testparm -s --section-name={SHARE} --parameter-name=path 2>/dev/null

printf 'estate probe\\n' > /tmp/estate-probe.txt
echo "@@writer_list"
smb "${{SCAN_SHARE_USER}}" "$PW" "ls"
echo "@@writer_list_rc=$?"
echo "@@writer_put"
smb "${{SCAN_SHARE_USER}}" "$PW" "put /tmp/estate-probe.txt $PROBE"
echo "@@writer_put_rc=$?"
echo "@@disk"
ls -l "$SHARE/$PROBE" 2>&1

if [ -n "$RU" ]; then
  echo "@@reader_list"
  smb "$RU" "$RW" "ls"
  echo "@@reader_list_rc=$?"
  echo "@@reader_get"
  rm -f /tmp/estate-readback.txt
  smb "$RU" "$RW" "get $PROBE /tmp/estate-readback.txt"
  echo "@@reader_get_rc=$?"
  echo "@@reader_bytes"
  if cmp -s /tmp/estate-probe.txt /tmp/estate-readback.txt; then echo same; else echo different; fi
  echo "@@reader_put"
  smb "$RU" "$RW" "put /tmp/estate-probe.txt estate-reader-write-attempt.txt"
  echo "@@reader_put_rc=$?"
  echo "@@reader_put_left"
  if [ -e "$SHARE/estate-reader-write-attempt.txt" ]; then echo present; else echo absent; fi
fi

echo "@@cleanup"
smb "${{SCAN_SHARE_USER}}" "$PW" "del estate-reader-write-attempt.txt"
smb "${{SCAN_SHARE_USER}}" "$PW" "del $PROBE"
echo "@@disk_after"
if [ -e "$SHARE/$PROBE" ]; then echo present; else echo absent; fi
rm -f /tmp/estate-probe.txt /tmp/estate-readback.txt
"""
    return script


def parse(text: str) -> dict[str, str]:
    """`@@key` markers to values. Pure, for the tests."""
    values: dict[str, str] = {}
    key: str | None = None
    buffer: list[str] = []

    for line in text.splitlines():
        if line.startswith("@@"):
            if key is not None:
                values[key] = "\n".join(buffer).strip()
                buffer = []
            body = line[2:]
            if "=" in body:
                name, _, value = body.partition("=")
                values[name.strip()] = value.strip()
                key = None
            else:
                key = body.strip()
        elif key is not None:
            buffer.append(line)
    if key is not None:
        values[key] = "\n".join(buffer).strip()
    return values


def _names(value: str) -> list[str]:
    """The accounts a Samba list parameter names."""
    return [part for part in value.replace(",", " ").split() if part]


def audit(
    reading: dict[str, str],
    writer: str,
    readers: list[str],
    force_group: str = "orion-nas",
) -> Audit:
    """Judge one reading. Pure, so the rules are the thing under test."""
    out = Audit()

    def rc(key: str) -> int | None:
        value = reading.get(key, "").strip()
        return int(value) if value.isdigit() else None

    def message(key: str) -> str:
        return " ".join(reading.get(key, "").split())[:300]

    if reading.get("cfg_read_only", "").strip().lower() != "yes":
        out.findings.append(
            Finding(
                "share_not_read_only",
                "fail",
                f"[{SHARE}] does not report `read only = Yes` "
                f"({reading.get('cfg_read_only', '').strip() or 'no value'}) — every valid user "
                f"can write it",
            )
        )

    if writer not in _names(reading.get("cfg_write_list", "")):
        out.findings.append(
            Finding(
                "writer_not_write_listed",
                "fail",
                f"[{SHARE}]'s `write list` ({reading.get('cfg_write_list', '').strip() or 'empty'}) "
                f"does not name {writer} — the scanner's uploads would be refused",
            )
        )

    valid = _names(reading.get("cfg_valid_users", ""))
    # A group in `valid users` admits everyone smbd says is in it; expanding it here
    # is what lets the generated `@orion-nas` pass without weakening the rule.
    admitted = set(valid) | set(_names(reading.get("cfg_group_members", "")))
    for account in [writer, *readers]:
        if account and admitted and account not in admitted:
            out.findings.append(
                Finding(
                    "account_not_valid",
                    "fail",
                    f"{account} is not in [{SHARE}]'s `valid users` ({' '.join(valid)}) — "
                    f"it cannot even open the share",
                )
            )

    configured_path = reading.get("cfg_path", "").strip()
    if configured_path and configured_path != SHARE_PATH:
        out.findings.append(
            Finding(
                "wrong_path",
                "fail",
                f"[{SHARE}] serves {configured_path}, not {SHARE_PATH} — the scans are landing "
                f"somewhere else",
            )
        )

    if rc("writer_list_rc") != 0:
        out.findings.append(
            Finding("writer_cannot_list", "fail", f"{writer} could not list [{SHARE}]: {message('writer_list')}")
        )
    if rc("writer_put_rc") != 0:
        out.findings.append(
            Finding(
                "writer_cannot_write",
                "fail",
                f"{writer} could not write to [{SHARE}]: {message('writer_put')}",
            )
        )
    else:
        disk = reading.get("disk", "").strip()
        fields = disk.split()
        if not disk or len(fields) < 4 or not MODE.fullmatch(fields[0]):
            out.findings.append(
                Finding(
                    "probe_not_on_disk",
                    "fail",
                    f"the probe {writer} just wrote is not on disk at {SHARE_PATH}/{PROBE}: {disk or 'no output'}",
                )
            )
        else:
            mode, _links, owner, group = fields[0], fields[1], fields[2], fields[3]
            if mode != PROBE_MODE:
                out.findings.append(
                    Finding(
                        "probe_mode",
                        "fail",
                        f"a file {writer} writes is {mode}, not {PROBE_MODE} — the share's masks are "
                        f"not being applied, so the household's readers and any 'other' user differ "
                        f"from what [{SHARE}] declares",
                    )
                )
            if group != force_group:
                out.findings.append(
                    Finding(
                        "probe_group",
                        "fail",
                        f"a file {writer} writes lands in group {group}, not {force_group} — "
                        f"the readers' group access is gone",
                    )
                )
            if owner != writer:
                out.findings.append(
                    Finding(
                        "probe_owner",
                        "fail",
                        f"a file {writer} writes is owned by {owner}, not {writer}",
                    )
                )

    if reading.get("disk_after", "").strip() == "present":
        out.findings.append(
            Finding(
                "probe_left_behind",
                "fail",
                f"{SHARE_PATH}/{PROBE} is still there after cleanup — the check is leaving litter "
                f"in the folder people read",
            )
        )

    if readers:
        reader_rc = rc("reader_list_rc")
        if reader_rc != 0:
            out.findings.append(
                Finding(
                    "reader_cannot_list",
                    "fail",
                    f"read-only reader {readers[0]} could not list [{SHARE}]: {message('reader_list')}",
                )
            )
        if rc("reader_get_rc") != 0 or reading.get("reader_bytes", "").strip() != "same":
            out.findings.append(
                Finding(
                    "reader_cannot_read",
                    "fail",
                    f"read-only reader {readers[0]} could not read {PROBE} back "
                    f"({message('reader_get') or 'no output'}) — the household's access is a listing, "
                    f"not a readable file",
                )
            )
        if rc("reader_put_rc") == 0 or reading.get("reader_put_left", "").strip() == "present":
            out.findings.append(
                Finding(
                    "reader_can_write",
                    "fail",
                    f"read-only reader {readers[0]} wrote to [{SHARE}] — `read only`/`write list` "
                    f"is not being enforced",
                )
            )
    else:
        out.findings.append(
            Finding(
                "reader_unproven",
                "note",
                f"no SCAN_SHARE_READ_USER/SCAN_SHARE_READ_PASSWORD is configured, so only the "
                f"writer and the stanza were exercised: that household/dhunter are *refused* a "
                f"write is unproven",
            )
        )

    return out


def run_probe(script: str, host: str = HOST, container: str = CONTAINER, timeout: int = 45) -> str:
    """Stream the probe into the container and return what it printed.

    The script goes over **stdin** — through `ssh` and `incus exec` — so the password inside
    it never appears in any process's `argv`, and no temporary file carries it.
    """
    command = f"incus exec {container} -- bash -s"
    try:
        completed = subprocess.run(
            [*ssh_prefix(), "-o", f"ConnectTimeout={timeout}", host, command],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired as error:
        raise HostUnreadable(HOST_NAME, host, f"timed out after {timeout + 30}s") from error
    if completed.returncode != 0:
        raise HostUnreadable(
            HOST_NAME,
            host,
            (completed.stderr or completed.stdout or "").strip() or f"exit {completed.returncode}",
        )
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=HOST, help=f"ssh target that runs the container (default: {HOST})")
    parser.add_argument("--container", default=CONTAINER, help=f"instance name (default: {CONTAINER})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-scan-share: could not write {args.prom}: {error}", file=sys.stderr)

    writer = os.environ.get("SCAN_SHARE_USER", "").strip() or "scans"
    password = os.environ.get("SCAN_SHARE_PASSWORD", "")
    reader = os.environ.get("SCAN_SHARE_READ_USER", "").strip()
    reader_password = os.environ.get("SCAN_SHARE_READ_PASSWORD", "")

    if not password:
        print(
            "check-scan-share: SCAN_SHARE_PASSWORD is not set — the share cannot be exercised, "
            "and a check that asserts nothing is not a pass",
            file=sys.stderr,
        )
        emit(None, ran=False)
        return 2
    if bool(reader) != bool(reader_password):
        print(
            "check-scan-share: SCAN_SHARE_READ_USER and SCAN_SHARE_READ_PASSWORD must be set "
            "together (or both left out to publish the reader half as a note)",
            file=sys.stderr,
        )
        emit(None, ran=False)
        return 2
    if shutil.which("ssh") is None:
        print("check-scan-share: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    script = build_probe(password, reader, reader_password).replace(
        "${SCAN_SHARE_USER}", shlex.quote(writer)
    )
    try:
        text = run_probe(script, host=args.host, container=args.container)
    except Exception as error:  # noqa: BLE001 — an unreachable host is one finding
        print(f"check-scan-share: could not read {HOST_NAME}: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-scan-share: reads the host with key-based `ssh`; where the estate "
                "uses passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    reading = parse(text)
    result = audit(reading, writer=writer, readers=[reader] if reader else [])
    emit(result, ran=True)

    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    if result.ok:
        print(
            f"ok   [{SHARE}] is read-only ({reading.get('cfg_read_only', '').strip() or 'default'}), "
            f"{writer} writes {PROBE_MODE} in {reading.get('cfg_force_group', '').strip()}, "
            f"and {'refuses ' + reader if reader else 'no second writer was exercised'}"
        )
    print(f"\ncheck-scan-share: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
