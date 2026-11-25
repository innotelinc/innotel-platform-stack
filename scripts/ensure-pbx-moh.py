#!/usr/bin/env python3
"""Put the console's music-on-hold classes back when a recreated volume ships stock.

WHY THIS EXISTS
---------------
`docs/pbx-sso-2026-11-25.md` records the day the FreePBX `asterisk-config` volume came
back from a redeploy with the image's own files: `musiconhold_console.conf` was gone and
`musiconhold.conf` had lost its `#include`. Every mount was still right, the console still
published its audio, and every call on hold played silence — Asterisk had nothing but
FreePBX's generated `[default]`, which reads the mount itself, a directory of class
subdirectories.

Both files are hand-written inside a docker volume that a recreate replaces, so the
repair in that runbook had to be run by a person who already knew the symptoms. This is
that repair on a timer: it reads the two files, writes only what is wrong, keeps a copy
of what it replaced, reloads `res_musiconhold.so`, and reads `moh show classes` back
before calling it repaired.

WHAT IT DOES, AND WHAT IT DOES NOT
----------------------------------
It owns exactly the two classes the console publishes (`default` and `jazz`): a class
that is missing is appended, and a class whose `directory=` is wrong has that one line
rewritten. Any other section in the file is left as it is. In `musiconhold.conf` it
moves the console's include to the end when it is missing or ahead of FreePBX's
generated file — Asterisk lets the later definition win, so its position is the whole
point — and it removes duplicate copies of that one line.

It does not touch the mount: `/var/lib/asterisk/moh` comes from Coolify's stored compose,
and a mount that is missing is not something a file inside the volume can fix. It does
not touch the console's audio. And it is a *reconciler*, not the alarm: that is
`check-pbx-moh.py`, which asserts the mount, the audio, the include order and this job's
own timer, so a repair that does not hold is still a red check.

A missing `musiconhold.conf` itself is not repaired: the file is FreePBX's, and inventing
one would replace its `custom`/`additional` includes with this repository's idea of them.
It exits 1 and says so instead.

Run it on the edge (it reads FreePBX over ssh, the same way the check does):

    ./scripts/ensure-pbx-moh.py
    ./scripts/ensure-pbx-moh.py --dry-run
    ./scripts/ensure-pbx-moh.py --pbx 192.168.1.52 --ssh-key /root/.ssh/id_ed25519

Exit codes: 0 = already right, or put right and read back; 1 = a file it must have is
missing, or the repair did not take; 2 = something could not be read.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field

#: Where Asterisk reads the classes, and therefore where the console's media directory is
#: mounted. The two class directories below live inside it.
MOH_MOUNT = "/var/lib/asterisk/moh"
#: The class definitions, and the file whose include order decides whether they win.
CLASS_FILE = "/etc/asterisk/musiconhold_console.conf"
INCLUDE_FILE = "/etc/asterisk/musiconhold.conf"
CONSOLE_INCLUDE = "musiconhold_console.conf"
FREEPBX_INCLUDE = "musiconhold_additional.conf"
#: Where a replaced file is kept before it is overwritten, inside the same volume so it
#: survives with the fix. `moh-<UTC stamp>/` per run, like the runbook's hand repair.
BACKUP_ROOT = "/etc/asterisk/backup"
#: What the read prints for a file that is not there, as a value rather than a failed
#: `cat`: a missing file is a repair to make, not a container that could not be read.
MISSING = "@@NO-FILE@@"
SPLIT = "@@FILE-END@@"
#: The two classes the console publishes, and the directory Asterisk must read for each.
CLASSES = {"default": f"{MOH_MOUNT}/default", "jazz": f"{MOH_MOUNT}/jazz"}


class Unreadable(Exception):
    """The state could not be read — which is not the same as needing repair."""


def run(command: list[str], timeout: int = 30, input_text: str | None = None) -> str:
    try:
        done = subprocess.run(command, input=input_text, capture_output=True,
                              text=True, timeout=timeout)
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


def container_on(pbx: str, pattern: str, ssh_key: str | None) -> str:
    """The one running container whose name matches, or `Unreadable` — compose renames it."""
    listing = f"docker ps --format '{{{{.Names}}}}' | grep {shlex.quote(pattern)}"
    names = [line.strip() for line in run(ssh_command(pbx, listing, ssh_key)).splitlines()
             if line.strip()]
    if len(names) != 1:
        raise Unreadable(f"{pbx}: expected one FreePBX container, found {names or 'none'}")
    return names[0]


@dataclass
class Section:
    """One `[name]` block and the lines under it, as the file holds them."""

    name: str
    body: list[str] = field(default_factory=list)


def split_sections(text: str) -> tuple[list[str], list[Section]]:
    """A class file split into the lines before the first header, and its sections.

    Section order and every unrecognised line are kept: the file is Asterisk's, and this
    is meant to add or correct a class, not to rewrite somebody's formatting.
    """
    preamble: list[str] = []
    sections: list[Section] = []
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) > 2 and stripped.startswith("[") and stripped.endswith("]"):
            sections.append(Section(stripped[1:-1].strip()))
        elif sections:
            sections[-1].body.append(line)
        else:
            preamble.append(line)
    return preamble, sections


def render(preamble: list[str], sections: list[Section]) -> str:
    """The file back out: one blank line between blocks, a trailing newline."""
    chunks: list[str] = []
    head = "\n".join(preamble).strip("\n")
    if head:
        chunks.append(head)
    for section in sections:
        body = "\n".join(section.body).strip("\n")
        chunks.append(f"[{section.name}]\n{body}".strip("\n"))
    return "\n\n".join(chunks) + "\n"


def class_directories(text: str) -> dict[str, str]:
    """Section name -> its `directory=` value; the last one wins, as Asterisk reads it."""
    directories: dict[str, str] = {}
    _, sections = split_sections(text)
    for section in sections:
        for line in section.body:
            key, separator, value = line.strip().partition("=")
            if separator and key.strip().lower() == "directory":
                directories[section.name] = value.strip()
    return directories


def parse_cli_classes(text: str) -> dict[str, str]:
    """`moh show classes` -> class -> directory, read back after a reload.

    The CLI prints `Class: <name>` and an indented `Directory:`, so it is parsed by label
    rather than by spacing — the same format `check-pbx-moh.py` reads, and a class with
    no directory stays present with an empty one rather than vanishing.
    """
    classes: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        label, separator, value = line.strip().partition(":")
        if not separator:
            continue
        label, value = label.strip().lower(), value.strip()
        if label == "class" and value:
            current = value
            classes.setdefault(current, "")
        elif label == "directory" and current and value:
            classes[current] = value
    return classes


def expected_class_file() -> str:
    """The two classes and nothing else, for the case where the file is gone."""
    return render([], [Section(name, ["mode=files", "sort=alpha", f"directory={directory}"])
                       for name, directory in CLASSES.items()])


def reconcile_classes(text: str | None) -> str | None:
    """The class file to write, or None when every class already reads its directory.

    Only the two files' lines that matter are touched: a missing class is appended whole
    (`mode=files`, `sort=alpha` and its directory), and a present class with the wrong
    `directory=` has that one line replaced. `text=None` is a missing file and renders
    both classes; any other file's sections survive untouched.
    """
    if text is None:
        return expected_class_file()
    preamble, sections = split_sections(text)
    changed = False
    for name, directory in CLASSES.items():
        match = next((section for section in sections if section.name == name), None)
        if match is None:
            sections.append(Section(name, ["mode=files", "sort=alpha",
                                           f"directory={directory}"]))
            changed = True
            continue
        for index, line in enumerate(match.body):
            key, separator, _ = line.strip().partition("=")
            if separator and key.strip().lower() == "directory":
                if line.strip() != f"directory={directory}":
                    match.body[index] = f"directory={directory}"
                    changed = True
                break
        else:
            match.body.append(f"directory={directory}")
            changed = True
    return render(preamble, sections) if changed else None


def is_console_include(line: str) -> bool:
    """True for the one include line this job owns, absolute or relative, quoted or not."""
    stripped = line.strip()
    if not stripped.startswith("#include"):
        return False
    value = stripped[len("#include"):].strip().strip('"')
    return value.rsplit("/", 1)[-1] == CONSOLE_INCLUDE


def reconcile_includes(text: str | None) -> str | None:
    """`musiconhold.conf` with the console's include last, or None when it already is.

    Asterisk reads the file top to bottom and a later section replaces an earlier one of
    the same name, so `musiconhold_additional.conf` has to come first. Duplicates of the
    console's own line are dropped rather than multiplied by repeated repairs.
    """
    if text is None:
        return None
    lines = text.splitlines()
    kept = [line for line in lines if not is_console_include(line)]
    kept.append(f"#include {CONSOLE_INCLUDE}")
    return "\n".join(kept) + "\n" if kept != lines else None


def read_state(pbx: str, container: str,
               ssh_key: str | None) -> tuple[str | None, str | None]:
    """Both files in one `docker exec`, each followed by a marker.

    One round trip rather than two, and a missing file is the marker rather than a
    non-zero `cat` — that distinction is what keeps "the volume came back stock" an exit
    0 repair instead of an exit 2 "could not read".
    """
    script = (f"for f in {CLASS_FILE} {INCLUDE_FILE}; do "
              f"if [ -r \"$f\" ]; then cat \"$f\"; else printf '%s\\n' {MISSING}; fi; "
              f"printf '%s\\n' {SPLIT}; done")
    text = run(ssh_command(
        pbx, f"docker exec {shlex.quote(container)} sh -c {shlex.quote(script)}", ssh_key))

    def one(part: str) -> str | None:
        return None if MISSING in part else part

    parts = text.split(SPLIT)
    return one(parts[0]), one(parts[1] if len(parts) > 1 else "")


def backup(pbx: str, container: str, ssh_key: str | None) -> str:
    """Copy the files this run may replace into `BACKUP_ROOT/moh-<UTC stamp>/`."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    directory = f"{BACKUP_ROOT}/moh-{stamp}"
    script = (f"mkdir -p {directory} && "
              f"for f in {CLASS_FILE} {INCLUDE_FILE}; do "
              f"[ -f \"$f\" ] && cp -a \"$f\" {directory}/; done; "
              f"printf '%s\\n' {directory}")
    return run(ssh_command(
        pbx, f"docker exec {shlex.quote(container)} sh -c {shlex.quote(script)}",
        ssh_key)).strip()


def write_file(pbx: str, container: str, path: str, content: str,
               ssh_key: str | None) -> None:
    """Replace one file with exactly `content`, over stdin — never a shell heredoc.

    The content is the estate's own text, and passing it as the process's stdin means no
    quoting layer can mangle it.
    """
    command = ssh_command(
        pbx, f"docker exec -i {shlex.quote(container)} sh -c "
             f"{shlex.quote(f'cat > {path}')}", ssh_key)
    try:
        done = subprocess.run(command, input=content, capture_output=True, text=True,
                              timeout=40)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unreadable(f"writing {path}: {error}") from error
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise Unreadable(f"writing {path}: {detail[0] if detail else 'failed'}")


def reload_and_read(pbx: str, container: str, ssh_key: str | None) -> dict[str, str]:
    """Give the file back to the asterisk user, reload the module, read the classes."""
    own = (f"chown asterisk:asterisk {CLASS_FILE} 2>/dev/null || true; "
           f"chmod 664 {CLASS_FILE}")
    run(ssh_command(pbx, f"docker exec {shlex.quote(container)} sh -c "
                          f"{shlex.quote(own)}", ssh_key))
    run(ssh_command(pbx, f"docker exec {shlex.quote(container)} asterisk -rx "
                          f"\"module reload res_musiconhold.so\"", ssh_key))
    return parse_cli_classes(run(ssh_command(
        pbx, f"docker exec {shlex.quote(container)} asterisk -rx 'moh show classes'",
        ssh_key)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pbx", default="192.168.1.52", help="the PBX host (default %(default)s)")
    parser.add_argument("--ssh-key", help="key for the PBX host (the ssh default if unset)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change, write nothing")
    args = parser.parse_args(argv)

    try:
        freepbx = container_on(args.pbx, "^freepbx-", args.ssh_key)
        class_text, include_text = read_state(args.pbx, freepbx, args.ssh_key)
    except Unreadable as error:
        print(f"ensure-pbx-moh: {error}", file=sys.stderr)
        return 2

    if include_text is None:
        print(f"ensure-pbx-moh: {INCLUDE_FILE} does not exist inside {freepbx}; it is "
              "FreePBX's own file, so this job will not invent one — restore the "
              "asterisk-config volume, then run it again", file=sys.stderr)
        return 1

    changes: dict[str, str] = {}
    class_plan = reconcile_classes(class_text)
    if class_plan is not None:
        changes[CLASS_FILE] = class_plan
    include_plan = reconcile_includes(include_text)
    if include_plan is not None:
        changes[INCLUDE_FILE] = include_plan

    if not changes:
        print(f"ensure-pbx-moh: {freepbx}: " +
              ", ".join(f"[{name}] {directory}" for name, directory in CLASSES.items()) +
              f" and the {CONSOLE_INCLUDE} include are in place (unchanged)")
        return 0
    if args.dry_run:
        for path, content in changes.items():
            print(f"ensure-pbx-moh: would rewrite {path}:")
            for line in content.splitlines():
                print(f"    {line}")
        return 0

    try:
        directory = backup(args.pbx, freepbx, args.ssh_key)
        for path, content in changes.items():
            write_file(args.pbx, freepbx, path, content, args.ssh_key)
        found = reload_and_read(args.pbx, freepbx, args.ssh_key)
    except Unreadable as error:
        print(f"ensure-pbx-moh: {error}", file=sys.stderr)
        return 1

    wrong = {name: directory for name, directory in CLASSES.items()
             if found.get(name) != directory}
    if wrong:
        print(f"ensure-pbx-moh: wrote {', '.join(changes)} (backup {directory}), but "
              f"`moh show classes` reads {wrong} — the repair did not take",
              file=sys.stderr)
        return 1
    print(f"ensure-pbx-moh: {freepbx}: rewrote {', '.join(changes)} (backup {directory}); "
          "`moh show classes` reads " +
          ", ".join(f"{name}={found[name]}" for name in CLASSES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
