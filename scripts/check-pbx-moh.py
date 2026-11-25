#!/usr/bin/env python3
"""Does a call on hold still play the console's audio, or has the mount gone quiet?

WHY THIS EXISTS
---------------
`docs/pbx-sso-2026-11-25.md` made the Asterisk Console's music-on-hold real: FreePBX
mounts the console's `…-pbx-data/_data/media/moh` directory at `/var/lib/asterisk/moh`,
the console publishes into its own store (`MOH_PUBLISH_DIR=/data/media/moh`), and
`musiconhold_console.conf` defines `[default]` and `[jazz]` with a directory *inside*
that mount.

Every one of those is a file or a mount outside this repository, and each fails
silently. A mount dropped from Coolify's stored compose leaves the stock
`asterisk-moh-opsound-*` files at `/var/lib/asterisk/moh` with no `default/` or `jazz/`
below it, so `mode=files` plays nothing and the caller hears silence; a class removed
from `musiconhold_console.conf` after an update falls back to FreePBX's own `[default]`,
which points at the mount itself; and audio deleted from the console's store is a class
Asterisk still lists with no file in it. None of those closes a door, so nothing else in
the estate reports them — the only symptom is on hold music that is not there.

WHAT IT ASSERTS
---------------
  1. FreePBX mounts the console's media directory at `/var/lib/asterisk/moh`, the path
     Asterisk reads — and, as a note, at `/data/media/moh`, the path the console's
     generated config names;
  2. every class `moh show classes` names has its directory **under that mount**, and
     that directory holds at least one readable, non-empty file inside FreePBX;
  3. `moh show files` names at least one file for every class — the list Asterisk
     actually plays from, which is not the same fact as the directory existing;
  4. every class with audio in the console's own publish directory has an Asterisk
     class. A published class nothing plays is silence, and the console calls that
     published.

Reads the two containers over ssh, the way `check-pbx-console.py` does. No credential is
used or stored: everything here is `docker exec` and `docker inspect`.

Run it on the edge (it needs the PBX over ssh):

    ./scripts/check-pbx-moh.py
    ./scripts/check-pbx-moh.py --pbx 192.168.1.52 --ssh-key /root/.ssh/id_ed25519
    ./scripts/check-pbx-moh.py --prom ${TEXTFILE_DIR}/estate-check-pbx-moh.prom

Exit codes: 0 = every published class plays from readable audio, 1 = drift, 2 = something
could not be read.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # the estate's shared telemetry contract, beside this file when it is installed
    from estate_check import Audit, Finding, write_prom  # noqa: E402

    HAVE_TELEMETRY = True
except ImportError:  # a bare copy still has to run: --prom is then simply unavailable
    Audit = Finding = write_prom = None  # type: ignore[assignment]
    HAVE_TELEMETRY = False

#: The `check` label this check publishes under (`estate-checks.yml` alerts on it).
CHECK = "pbx_moh"

CONSOLE_PORT = 8097
#: Where Asterisk reads the classes, and therefore where the console's media directory
#: has to be mounted. `musiconhold_console.conf` names a *subdirectory* of this.
MOH_MOUNT = "/var/lib/asterisk/moh"
#: The second path the same directory is mounted at: the one the console's generated
#: config names. A call does not need it, so its absence is a note rather than a failure.
CONSOLE_PATH = "/data/media/moh"


class Drift(Exception):
    """One assertion did not hold."""


class Unreadable(Exception):
    """The state could not be read — which is not the same as drift."""


@dataclass(frozen=True)
class MohClass:
    """One class as `moh show classes` reports it: a name and the directory it reads."""

    name: str
    directory: str


def run(command: list[str], timeout: int = 25) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unreadable(f"{' '.join(command[:3])}…: {error}") from error
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise Unreadable(f"{' '.join(command[:3])}…: {detail[0] if detail else 'failed'}")
    return done.stdout


def ssh_command(pbx: str, command: str, ssh_key: str | None) -> list[str]:
    prefix = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
              "-o", "ConnectTimeout=10"]
    if ssh_key:
        prefix += ["-i", ssh_key, "-o", "IdentitiesOnly=yes"]
    return [*prefix, f"root@{pbx}", command]


def container_on(pbx: str, pattern: str, ssh_key: str | None, what: str) -> str:
    """The one running container whose name matches, or `Unreadable` — compose renames them."""
    listing = f"docker ps --format '{{{{.Names}}}}' | grep {shlex.quote(pattern)}"
    names = [line.strip() for line in run(ssh_command(pbx, listing, ssh_key)).splitlines()
             if line.strip()]
    if len(names) != 1:
        raise Unreadable(f"{pbx}: expected one {what} container, found {names or 'none'}")
    return names[0]


def console_container(pbx: str, ssh_key: str | None) -> str:
    """The console, found by the port it publishes."""
    names = [line.strip() for line in run(ssh_command(
        pbx, f"docker ps --filter publish={CONSOLE_PORT} --format '{{{{.Names}}}}'",
        ssh_key)).splitlines() if line.strip()]
    if len(names) != 1:
        raise Unreadable(f"{pbx}: expected one container publishing :{CONSOLE_PORT}, "
                         f"found {names or 'none'}")
    return names[0]


def mounts(pbx: str, container: str, ssh_key: str | None) -> list[dict]:
    """A container's mounts, with `Destination` and `Source` exactly as docker holds them."""
    text = run(ssh_command(pbx, f"docker inspect {shlex.quote(container)}", ssh_key), timeout=40)
    try:
        inspected = json.loads(text)
    except ValueError as error:
        raise Unreadable(f"{container}: docker inspect did not return JSON: {error}") from error
    if not isinstance(inspected, list) or not inspected:
        raise Unreadable(f"{container}: docker inspect returned no container")
    return (inspected[0].get("Mounts") or [])


def mount_at(found: list[dict], destination: str) -> dict | None:
    """The mount whose destination is exactly this path (a subpath is a different mount)."""
    for mount in found:
        if (mount.get("Destination") or "").rstrip("/") == destination.rstrip("/"):
            return mount
    return None


def mount_covering(found: list[dict], path: str) -> dict | None:
    """The mount a path lives under, nearest first.

    A directory inside a mounted volume is not a mount of its own: the console's
    `MOH_PUBLISH_DIR=/data/media/moh` sits inside the volume mounted at `/data`, which is
    exactly what makes it survive a recreate. The question there is "is this path on a
    volume", not "is there a mount whose destination is this path".
    """
    target = path.rstrip("/") + "/"
    best: dict | None = None
    for mount in found:
        destination = (mount.get("Destination") or "").rstrip("/")
        if target.startswith(destination + "/"):
            if best is None or len(destination) > len((best.get("Destination") or "").rstrip("/")):
                best = mount
    return best


def parse_classes(text: str) -> list[MohClass]:
    """`moh show classes` -> the classes and their directories.

    The CLI prints three lines per class: `Class: <name>`, then indented `Mode:` and
    `Directory:`. Parsing the labelled lines rather than the spacing keeps this working
    when Asterisk pads the table differently, and a class with no `Directory:` line is
    returned with an empty directory so it is reported rather than dropped.
    """
    classes: list[MohClass] = []
    names: list[str] = []
    directories: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        label, separator, value = line.strip().partition(":")
        if not separator:
            continue
        label, value = label.strip().lower(), value.strip()
        if label == "class" and value:
            current = value
            names.append(value)
        elif label == "directory" and current and value:
            directories[current] = value
    for name in names:
        classes.append(MohClass(name, directories.get(name, "")))
    return classes


def parse_files(text: str) -> dict[str, list[str]]:
    """`moh show files` -> class -> the files Asterisk will play for it."""
    files: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        label, separator, value = line.strip().partition(":")
        if not separator:
            continue
        label, value = label.strip().lower(), value.strip()
        if label == "class" and value:
            current = value
            files.setdefault(value, [])
        elif label == "file" and current and value:
            files[current].append(value)
    return files


def parse_counts(text: str) -> dict[str, int]:
    """`COUNT <directory>|<n>` lines -> directory -> readable non-empty files in it."""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        label, _, rest = line.strip().partition(" ")
        if label != "COUNT":
            continue
        directory, _, number = rest.partition("|")
        try:
            counts[directory.rstrip("/")] = int(number)
        except ValueError:
            continue
    return counts


def parse_dir(text: str) -> str:
    """The `DIR <path>` line a publish-directory listing starts with, or ''."""
    for line in text.splitlines():
        label, _, path = line.strip().partition(" ")
        if label == "DIR" and path.strip():
            return path.strip().rstrip("/")
    return ""


#: Counts the readable, non-empty files in each immediate subdirectory of `$d`.
#: `-r` as well as `-s`: a file the asterisk user cannot open is as silent as a missing
#: one, and the mount can be right while the mode on the console's copy is not.
COUNT_SCRIPT = """
d=@@DIR@@
[ -d "$d" ] || { printf 'DIR %s\\n' "$d"; exit 3; }
printf 'DIR %s\\n' "$d"
for c in "$d"/*/; do
  [ -d "$c" ] || continue
  n=0
  for f in "${c%/}"/*; do
    [ -f "$f" ] && [ -r "$f" ] && [ -s "$f" ] && n=$((n+1))
  done
  printf 'COUNT %s|%s\\n' "${c%/}" "$n"
done
"""


def count_directories(pbx: str, container: str, directory: str,
                      ssh_key: str | None) -> str:
    """The COUNT_SCRIPT's output inside one container.

    `MOH_PUBLISH_DIR` is the console's own setting, so its script reads the variable and
    falls back to Asterisk's path; the FreePBX side is given the literal mount.
    """
    target = (f'"${{MOH_PUBLISH_DIR:-{MOH_MOUNT}}}"' if directory == ""
              else f'"{directory}"')
    script = COUNT_SCRIPT.replace("@@DIR@@", target)
    return run(ssh_command(
        pbx, f"docker exec {shlex.quote(container)} sh -c {shlex.quote(script)}", ssh_key))


def class_name(path: str) -> str:
    """`/var/lib/asterisk/moh/jazz` -> `jazz`; a trailing slash is not part of the name."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the console's music on hold still plays in Asterisk.")
    parser.add_argument("--pbx", default="192.168.1.52")
    parser.add_argument("--ssh-key")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    audit = Audit() if HAVE_TELEMETRY else None

    def fail(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="fail", message=message))

    def note(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="warn", message=message))

    def publish(ran_ok: bool) -> None:
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, audit, ran_ok)
        elif args.prom:
            print("check-pbx-moh: --prom needs estate_check.py beside this file", file=sys.stderr)

    failures: list[str] = []

    try:
        freepbx = container_on(args.pbx, "^freepbx-", args.ssh_key, "FreePBX")
        console = console_container(args.pbx, args.ssh_key)
        moh_mounts = mounts(args.pbx, freepbx, args.ssh_key)
        console_mounts = mounts(args.pbx, console, args.ssh_key)
        classes = parse_classes(run(ssh_command(
            args.pbx, f"docker exec {shlex.quote(freepbx)} asterisk -rx 'moh show classes'",
            args.ssh_key)))
        played = parse_files(run(ssh_command(
            args.pbx, f"docker exec {shlex.quote(freepbx)} asterisk -rx 'moh show files'",
            args.ssh_key)))
        in_asterisk = parse_counts(count_directories(args.pbx, freepbx, MOH_MOUNT,
                                                     args.ssh_key))
        publish_text = count_directories(args.pbx, console, "", args.ssh_key)
    except Unreadable as error:
        print(f"FAIL  {error}", file=sys.stderr)
        publish(False)
        return 2

    # 1. The mount Asterisk reads, and the second path the console's config names.
    mounted = mount_at(moh_mounts, MOH_MOUNT)
    if mounted is None:
        message = (f"{freepbx}: nothing is mounted at {MOH_MOUNT} — Asterisk reads the "
                   "image's stock files there, so the console's classes are not on this "
                   "host and every call on hold plays silence")
        failures.append(message)
        fail("mount", message)
        print(f"FAIL  {'mount':24} nothing at {MOH_MOUNT}")
    else:
        print(f"PASS  {'mount':24} {mounted.get('Source', '?')} at {MOH_MOUNT}")
        if mounted.get("RW"):
            note("mount_rw", f"{MOH_MOUNT} is mounted read-write; Asterisk only reads it, "
                             "and the console writes its store, so ro is the intended shape")
    if mount_at(moh_mounts, CONSOLE_PATH) is None:
        note("console_path", f"{freepbx} does not mount the media directory at "
                             f"{CONSOLE_PATH}; Asterisk reads {MOH_MOUNT} so calls are "
                             "unaffected, but the console-generated path is only a directory")

    # 2. Every class Asterisk knows reads a directory under the mount, with audio in it.
    if not classes:
        message = (f"{freepbx}: `moh show classes` names no class — FreePBX's stock "
                   "classes are the only thing left and the console's audio plays nowhere")
        failures.append(message)
        fail("classes", message)
        print(f"FAIL  {'classes':24} none defined")
    for moh_class in classes:
        directory = moh_class.directory.rstrip("/")
        under_mount = directory.startswith(MOH_MOUNT.rstrip("/") + "/")
        if not directory:
            message = (f"{moh_class.name}: no directory reported, so nothing says where "
                       "its audio should be read from")
        elif not under_mount:
            message = (f"{moh_class.name}: reads {directory}, which is not under "
                       f"{MOH_MOUNT} — a class pointed at the mount itself plays silence "
                       "over the console's class subdirectories")
        elif in_asterisk.get(directory, 0) == 0:
            message = (f"{moh_class.name}: {directory} holds no readable, non-empty audio "
                       f"inside {freepbx} — the mount is there and nothing was published "
                       "into it")
        else:
            message = ""
        if message:
            failures.append(message)
            fail("class_audio", message)
            print(f"FAIL  {moh_class.name:24} {message.split(': ', 1)[-1]}")
            continue
        if not played.get(moh_class.name):
            message = (f"{moh_class.name}: `moh show files` names no file for the class — "
                       "Asterisk has the class and will play nothing")
            failures.append(message)
            fail("class_files", message)
            print(f"FAIL  {moh_class.name:24} no file in `moh show files`")
            continue
        count = in_asterisk.get(directory, 0)
        print(f"PASS  {moh_class.name:24} {count} file(s) at {directory}")

    # 3. What the console calls published has a class to play it.
    publish_dir = parse_dir(publish_text) or CONSOLE_PATH
    published = {class_name(path): count for path, count in parse_counts(publish_text).items()}
    console_mount = mount_covering(console_mounts, publish_dir)
    if console_mount is None:
        message = (f"{console}: {publish_dir} is not inside any volume of the console, so "
                   "the classes it publishes live only inside this container's filesystem "
                   "and a recreate loses them")
        failures.append(message)
        fail("console_store", message)
        print(f"FAIL  {'console store':24} {publish_dir} is on no volume of {console}")
    else:
        print(f"PASS  {'console store':24} {publish_dir} is inside "
              f"{console_mount.get('Destination', '?')} "
              f"({console_mount.get('Source', '?')})")

    known = {moh_class.name for moh_class in classes}
    with_audio = {name for name, count in published.items() if count > 0}
    for name in sorted(with_audio - known):
        message = (f"{name}: {published[name]} file(s) in the console's publish directory "
                   f"({publish_dir}/{name}) and no Asterisk class — a published class "
                   "nothing plays is silence on hold")
        failures.append(message)
        fail("console_class", message)
        print(f"FAIL  {name:24} published in the console, no Asterisk class")
    for name in sorted((known & set(published)) - with_audio):
        message = (f"{name}: Asterisk has the class and the console's publish directory "
                   f"({publish_dir}/{name}) holds no readable audio")
        failures.append(message)
        fail("console_audio", message)
        print(f"FAIL  {name:24} no audio in the console's store")
    if with_audio and not (with_audio - known):
        print(f"PASS  {'console publish':24} {len(with_audio)} class(es) from {publish_dir}")

    for failure in failures:
        print(f"      {failure}")
    print()
    print("every published class plays from the console's audio" if not failures
          else f"{len(failures)} assertion(s) no longer hold")
    publish(True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
