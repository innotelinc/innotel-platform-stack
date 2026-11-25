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
     published;
  5. `/var/lib/asterisk/moh` is mounted **read-only**. The console owns the audio and
     Asterisk only reads it, so a write through this mount edits the console's store —
     and nothing else would say so;
  6. `musiconhold.conf` includes `musiconhold_console.conf` **after**
     `musiconhold_additional.conf`. Asterisk lets a later definition win, and FreePBX's
     own `[default]` points at the mount itself; the include is one line in a file the
     image ships stock, so an image update or a recreated config volume silently
     reinstates silence while every mount still looks right;
  7. the reconciler that puts those two files back (`pbx-moh-config.timer`) is enabled,
     active, and its last run is clean and recent. It is read on this host, the edge,
     where it is installed: a repair job that stopped is invisible until the next
     volume is recreated, which is the one moment nobody is watching.

It also publishes the files it counted per class
(`innotel_estate_check_moh_files{class="…",source="asterisk|console"}`), so the
dashboard says *which* class went quiet rather than only that the check went red.

Reads the two containers over ssh, the way `check-pbx-console.py` does. No credential is
used or stored: everything here is `docker exec` and `docker inspect`.

Run it on the edge (it needs the PBX over ssh):

    ./scripts/check-pbx-moh.py
    ./scripts/check-pbx-moh.py --pbx 192.168.1.52 --ssh-key /root/.ssh/id_ed25519
    ./scripts/check-pbx-moh.py --prom ${TEXTFILE_DIR}/estate-check-pbx-moh.prom
    ./scripts/check-pbx-moh.py --config-timer ''   # not on the edge, where it is installed

Exit codes: 0 = every published class plays from readable audio, 1 = drift, 2 = something
could not be read.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # the estate's shared telemetry contract, beside this file when it is installed
    from estate_check import Audit, Finding, unit_state, write_prom  # noqa: E402

    HAVE_TELEMETRY = True
except ImportError:  # a bare copy still has to run: --prom is then simply unavailable
    Audit = Finding = unit_state = write_prom = None  # type: ignore[assignment]
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
#: The file Asterisk's class definitions come from, and the two includes whose order is
#: load-bearing: later definitions win, and FreePBX's generated `[default]` reads the
#: mount itself rather than a class subdirectory of it.
INCLUDE_FILE = "/etc/asterisk/musiconhold.conf"
CONSOLE_INCLUDE = "musiconhold_console.conf"
FREEPBX_INCLUDE = "musiconhold_additional.conf"
#: What the include read prints when the file is absent, so "missing" is a finding
#: rather than an unreadable container.
MISSING_MARKER = "@@NO-MUSICONHOLD-CONF@@"
#: The timer that writes the class file and the include back after a recreated volume.
#: It is installed beside this check on the edge, which is where `unit_state` reads it.
DEFAULT_TIMER = "pbx-moh-config.timer"


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


def parse_includes(text: str) -> list[str]:
    """The `#include`d filenames in `musiconhold.conf`, in the order Asterisk reads them.

    Asterisk reads the file top to bottom and a later section replaces an earlier one of
    the same name, so the order of these lines is the whole point: the console's file has
    to come after FreePBX's generated one. Only the basename is kept — the include may be
    relative to `/etc/asterisk` or absolute, and both mean the same file to Asterisk.
    """
    included: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#include"):
            continue
        value = stripped[len("#include"):].strip().strip('"')
        if value:
            included.append(value.rsplit("/", 1)[-1])
    return included


def include_problem(included: list[str] | None) -> str:
    """What is wrong with the include order, or '' when the console's file wins.

    `included` is None when `musiconhold.conf` itself is absent, which is a different
    finding from the include line being gone: without the file Asterisk has no class
    definitions at all.
    """
    if included is None:
        return (f"{INCLUDE_FILE} does not exist inside FreePBX, so no class file is "
                "included and every class `moh show classes` should name is gone")
    if CONSOLE_INCLUDE not in included:
        return (f"{INCLUDE_FILE} does not include {CONSOLE_INCLUDE}, so the console's "
                "classes are never defined — Asterisk falls back to FreePBX's own "
                "[default], which reads the mount itself and plays silence over the "
                "class subdirectories")
    if FREEPBX_INCLUDE in included and included.index(CONSOLE_INCLUDE) < included.index(
            FREEPBX_INCLUDE):
        return (f"{CONSOLE_INCLUDE} is included before {FREEPBX_INCLUDE}, so FreePBX's "
                "generated [default] replaces the console's — mode=files then reads "
                "the mount itself, which is a directory of class subdirectories, and "
                "the caller hears silence")
    return ""


def escape_label(value: str) -> str:
    """Escape a label value the way the textfile format requires (\\, \", newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def moh_series(classes: list[MohClass], in_asterisk: dict[str, int],
               published: dict[str, int]) -> str:
    """The per-class file counts, as its own metric family.

    One series per class and side, so a panel can say *which* class lost its audio rather
    than only that the check is red: `source="asterisk"` is what `moh show files` will
    play, `source="console"` is what the console says it published. They disagreeing is
    the failure this whole check exists for, and it is invisible in a single status.
    """
    counts: dict[str, dict[str, int]] = {}
    for moh_class in classes:
        counts.setdefault(moh_class.name, {})["asterisk"] = in_asterisk.get(
            moh_class.directory.rstrip("/"), 0)
    for name, number in published.items():
        counts.setdefault(name, {})["console"] = number
    lines = [
        "# HELP innotel_estate_check_moh_files Readable, non-empty audio files per "
        "music-on-hold class, as each side counts them",
        "# TYPE innotel_estate_check_moh_files gauge",
    ]
    for name in sorted(counts):
        for source in sorted(counts[name]):
            lines.append(
                f'innotel_estate_check_moh_files{{check="{CHECK}",'
                f'class="{escape_label(name)}",source="{source}"}} {counts[name][source]}')
    return "\n".join(lines)


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


def read_includes(pbx: str, container: str, ssh_key: str | None) -> list[str] | None:
    """The includes `musiconhold.conf` carries, or None when the file is not there.

    A missing file is a finding rather than an unreadable container, so the command still
    exits 0 and prints a marker: `cat` failing would be reported as "could not read",
    which is the wrong exit code and the wrong alert.
    """
    script = (f"if [ -r {INCLUDE_FILE} ]; then cat {INCLUDE_FILE}; "
              f"else printf '%s\\n' {MISSING_MARKER}; fi")
    text = run(ssh_command(
        pbx, f"docker exec {shlex.quote(container)} sh -c {shlex.quote(script)}", ssh_key))
    if MISSING_MARKER in text:
        return None
    return parse_includes(text)


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
    parser.add_argument("--config-timer", default=DEFAULT_TIMER, metavar="UNIT",
                        help="the reconciler timer to assert on this host (default "
                             "%(default)s); pass '' off the edge, where it is not installed")
    args = parser.parse_args(argv)

    audit = Audit() if HAVE_TELEMETRY else None

    def fail(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="fail", message=message))

    def note(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="warn", message=message))

    def publish(ran_ok: bool, extra: str | None = None) -> None:
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, audit, ran_ok, extra=extra)
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
        included = read_includes(args.pbx, freepbx, args.ssh_key)
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
            message = (f"{MOH_MOUNT} is mounted read-write inside {freepbx}; Asterisk only "
                       "reads it and the console owns the audio, so a write through this "
                       "mount edits the console's store — ro is the intended shape")
            failures.append(message)
            fail("mount_rw", message)
            print(f"FAIL  {'mount_ro':24} {MOH_MOUNT} is mounted read-write")
    if mount_at(moh_mounts, CONSOLE_PATH) is None:
        note("console_path", f"{freepbx} does not mount the media directory at "
                             f"{CONSOLE_PATH}; Asterisk reads {MOH_MOUNT} so calls are "
                             "unaffected, but the console-generated path is only a directory")

    # 1b. The include that defines the classes, in the order that lets it win.
    problem = include_problem(included)
    if problem:
        failures.append(problem)
        fail("include_order", problem)
        print(f"FAIL  {'includes':24} {problem}")
    else:
        print(f"PASS  {'includes':24} {CONSOLE_INCLUDE} after "
              f"{FREEPBX_INCLUDE if FREEPBX_INCLUDE in included else 'nothing else'}")

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

    # 4. The reconciler that owns the two config files is itself on a schedule. Read here
    # rather than over ssh because it is installed on the host this check runs on — the
    # edge — and a job nobody reads is the same silence one layer out.
    if args.config_timer:
        if not HAVE_TELEMETRY or unit_state is None:
            print(f"NOTE  {'MOH config timer':24} needs estate_check.py beside this file")
        else:
            try:
                timer = unit_state(args.config_timer)
                last = unit_state(args.config_timer.removesuffix(".timer") + ".service")
            except RuntimeError as error:
                print(f"FAIL  {error}", file=sys.stderr)
                publish(False)
                return 2
            problems = []
            if timer.load != "loaded" or timer.enabled != "enabled":
                problems.append(
                    f"{args.config_timer} is not enabled (LoadState={timer.load or 'unreadable'}, "
                    f"UnitFileState={timer.enabled or 'none'}) — a recreated volume would "
                    "keep playing silence until somebody ran the repair by hand")
            elif timer.active != "active":
                problems.append(f"{args.config_timer} is enabled but not active "
                                f"(ActiveState={timer.active}) — the schedule exists and "
                                "nothing is firing it")
            if last.exited_at is None:
                problems.append(f"{last.name} has never run since it was enabled — the "
                                "timer is installed and has not fired once")
            elif not last.last_run_ok:
                problems.append(f"{last.name}'s last run was not clean (Result={last.result}, "
                                f"ExecMainStatus={last.exit_status})")
            elif time.time() - last.exited_at > 2 * 60 * 60:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last.exited_at))
                problems.append(f"{last.name} last ran at {when}, over two hours ago — it is "
                                "scheduled every ten minutes, so the timer has stopped")
            if problems:
                failures += problems
                for problem in problems:
                    fail("config_timer", problem)
                print(f"FAIL  {'MOH config timer':24} {problems[0]}")
            else:
                print(f"PASS  {'MOH config timer':24} {args.config_timer} enabled and firing")

    for failure in failures:
        print(f"      {failure}")
    print()
    print("every published class plays from the console's audio" if not failures
          else f"{len(failures)} assertion(s) no longer hold")
    publish(True, moh_series(classes, in_asterisk, published))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
