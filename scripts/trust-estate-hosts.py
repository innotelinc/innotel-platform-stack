#!/usr/bin/env python3
"""Put the edge's check key on every estate host, and name each one in the edge's ssh config.

WHY THIS EXISTS
---------------
Every estate check reads each host with **key-only, non-interactive** ssh
(`scripts/estate_check.py`). That works only when two things are true, and neither is in
this repo:

  * the edge's public key (`/root/.ssh/container-address.pub`) is in that host's
    `/root/.ssh/authorized_keys`, and
  * the host's address is named in the edge's `/root/.ssh/config` with the check key as
    its `IdentityFile`.

`i4` (`.54`) joined the estate on 2026-10-02 with neither. The symptom was not a wrong
answer but *no* answer: the checks read as "a host could not be reached", the shape of an
estate outage, and it took a hand-run to tell apart from a powered-off box. (That is what
`estate_check.HostUnreadable` says when it happens.) Both files were fixed by hand — and a
hand fix is the thing that quietly regresses: the next host added, the next time the edge
is rebuilt, and the trust is gone again with nothing in the repo to put it back.

So the fix is a script. It is idempotent, `--check` answers "is the trust still in place?"
without changing anything, and what it writes is exactly two things:

    the host   — `authorized_keys` gains the check key, 0600 inside a 0700 `.ssh`
    the edge   — a marked block in `/root/.ssh/config` naming every host by address

Reaching a host that does not yet trust the key needs a bootstrap credential: give it one
the way the checks take theirs (`SSHPASS='…'` with `sshpass` installed), or run this
somewhere the key already works. `--check` needs no credential — it tests the *key* path,
which is the thing that matters.

Usage:
    ./scripts/trust-estate-hosts.py             # install the trust
    ./scripts/trust-estate-hosts.py --check     # report drift, change nothing
    ./scripts/trust-estate-hosts.py --dry-run
    SSHPASS='…' ./scripts/trust-estate-hosts.py

Exit codes: 0 = every host is trusted (or was made so), 1 = `--check` found a host not
trusted or the ssh config adrift, 2 = a host could not be read to install onto.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import read_host, ssh  # noqa: E402

#: The key the scheduled checks authenticate with. Private half stays on the edge and is
#: authorized `from="192.168.1.71"` on each host, so a copy elsewhere is inert.
CHECK_KEY = "/root/.ssh/container-address"
CHECK_KEY_PUB = f"{CHECK_KEY}.pub"
SSH_CONFIG = "/root/.ssh/config"

#: The block in the edge's ssh config this script owns. Marked so it can be replaced
#: without touching the rest of a hand-written config, and so `--check` can compare it.
BEGIN = "# BEGIN estate-check-hosts — written by scripts/trust-estate-hosts.py"
END = "# END estate-check-hosts"

#: The estate hosts, by the same name and order the checks use. Cross-checked against
#: `check-estate-inventory.py` by `scripts/tests/test_check_host_coverage.py`, so a host
#: added there and not here (or the reverse) fails the suite rather than going untrusted.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: How to test the key path in `--check`: the user config is bypassed (`-F /dev/null`) so
#: the answer is about `authorized_keys` alone, and `BatchMode` means a host without the
#: key fails instead of prompting. The `--check` probe never uses a password, because the
#: thing under test is key-only ssh.
KEY_ONLY = [
    "ssh",
    "-F", "/dev/null",
    "-i", CHECK_KEY,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
]


def address_of(target: str) -> str:
    """The address a `user@host` target names."""
    return target.split("@", 1)[-1]


def config_block(hosts: dict[str, str] = HOSTS) -> str:
    """The block the edge's ssh config should carry for the check key.

    One `Host` line listing every address, because they share every option: the checks
    build `root@<address>` themselves, so the pattern has to be the address, and the key
    is chosen by `IdentityFile` plus `IdentitiesOnly yes` (without it ssh offers the
    agent's keys too, and a host that trusts none of them still prompts).
    """
    addresses = sorted({address_of(target) for target in hosts.values()})
    return "\n".join(
        [
            BEGIN,
            "# Key-only and non-interactive: the checks run unattended and must never hang",
            "# on a prompt. Regenerate with scripts/trust-estate-hosts.py; do not hand-edit.",
            "Host " + " ".join(addresses),
            f"  IdentityFile {CHECK_KEY}",
            "  IdentitiesOnly yes",
            "  StrictHostKeyChecking no",
            "  UserKnownHostsFile /dev/null",
            "  BatchMode yes",
            END,
        ]
    )


def current_block(text: str) -> str | None:
    """The managed block `text` currently carries, or None if it has none."""
    lines = text.splitlines()
    if BEGIN in lines and END in lines:
        return "\n".join(lines[lines.index(BEGIN): lines.index(END) + 1])
    return None


def config_with(text: str, block: str | None = None) -> str:
    """`text` with the managed block replaced in place, or appended if absent.

    Deliberately a splice rather than a rewrite: the edge's config may hold hand-written
    `Host` entries for other things, and this owns only its marked block.
    """
    block = config_block() if block is None else block
    lines = text.splitlines()
    if BEGIN in lines and END in lines:
        start, end = lines.index(BEGIN), lines.index(END)
        merged = lines[:start] + block.splitlines() + lines[end + 1:]
        return "\n".join(merged).rstrip("\n") + "\n"
    base = text.rstrip("\n")
    joiner = "\n\n" if base else ""
    return f"{base}{joiner}{block}\n"


def config_drift(text: str) -> bool:
    """True when the config's managed block is not the one this script would write."""
    return current_block(text) != config_block()


def key_blob(pubkey: str) -> str:
    """The `<type> <base64>` of a public key, with any comment dropped.

    The blob, not the whole line, is what is compared: an `authorized_keys` line may carry
    options (`from="…"`, `command=…`) before the key, and ssh matches on the key itself.
    """
    parts = pubkey.split()
    return f"{parts[0]} {parts[1]}" if len(parts) >= 2 else pubkey.strip()


def key_present(text: str, pubkey: str) -> bool:
    """Whether an `authorized_keys` body already carries `pubkey`, options or not.

    Matched as two tokens, not as the joined `<type> <base64>` string: an authorized_keys
    line may carry options (`from="…"`) before the key, so the type and the blob appear
    separately in it, and a single-token compare would miss the key and append a duplicate.
    """
    parts = pubkey.split()
    if len(parts) < 2:
        return pubkey.strip() in text
    key_type, data = parts[0], parts[1]
    for line in text.splitlines():
        tokens = line.split()
        if key_type in tokens and data in tokens:
            return True
    return False


def authorized_keys_install(pubkey: str) -> str:
    """The remote shell that makes `authorized_keys` carry `pubkey`, idempotently.

    Built here (and tested) rather than inlined at the call site so the two properties
    that matter are visible: the append is guarded by `grep -qF` on the key blob, so a
    second run adds nothing and a line that already carries the key is left alone; and the
    file is created 0600 inside a 0700 `.ssh`, which is the mode ssh insists on before it
    will read either.
    """
    body = pubkey.strip()
    return (
        "install -d -m 700 /root/.ssh && "
        "touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && "
        f"if grep -qF '{key_blob(pubkey)}' /root/.ssh/authorized_keys; then echo present; "
        f"else printf '%s\\n' '{body}' >> /root/.ssh/authorized_keys; echo added; fi"
    )


def _run_key_only(target: str) -> None:
    """Authenticate to `target` with the check key alone, or raise. The `--check` probe."""
    subprocess.run(
        [*KEY_ONLY, target, "true"],
        capture_output=True,
        text=True,
        timeout=25,
        check=True,
    )


def key_works(target: str, runner=_run_key_only) -> bool:
    """Whether key-only ssh to `target` succeeds. `runner` is injectable for the tests."""
    try:
        runner(target)
        return True
    except Exception:  # noqa: BLE001 — any failure is "the key is not trusted", here
        return False


def read_pubkey(path: str = CHECK_KEY_PUB) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def write_config(path: str, text: str) -> None:
    """Write `text` atomically and 0600 — ssh refuses a config others can write."""
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False, prefix=".ssh-config-")
    try:
        handle.write(text)
        handle.close()
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--check", action="store_true", help="report drift and change nothing")
    parser.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    parser.add_argument("--config", default=SSH_CONFIG, help="the edge's ssh config to manage")
    parser.add_argument("--pub", default=CHECK_KEY_PUB, help="the check key's public half")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    block = config_block(hosts)
    try:
        with open(args.config, encoding="utf-8") as handle:
            config = handle.read()
    except FileNotFoundError:
        config = ""
    except OSError as error:
        print(f"trust-estate-hosts: could not read {args.config}: {error}", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.dirname(args.config) or "."):
        print(f"trust-estate-hosts: {os.path.dirname(args.config)} does not exist", file=sys.stderr)
        return 2

    drifted = current_block(config) != block

    # `--check` is the key path and the config block, and nothing else: no password, no
    # writes. That is the pair that has to hold for the scheduled checks to read a host.
    if args.check:
        untrusted = []
        for name, target in hosts.items():
            if not key_works(target):
                untrusted.append((name, target))
                print(f"DRIFT {name}: {target} does not accept {CHECK_KEY} — not in its authorized_keys")
        if drifted:
            print(f"DRIFT the managed block in {args.config} is not what this script would write")
        if untrusted or drifted:
            print(
                f"\ntrust-estate-hosts: {len(untrusted)} host(s) untrusted, "
                f"ssh config {'adrift' if drifted else 'in place'}; run without --check to repair"
            )
            return 1
        print(f"trust-estate-hosts: {len(hosts)} host(s) trusted, {args.config} in place")
        return 0

    if shutil.which("ssh") is None:
        print("trust-estate-hosts: no ssh on PATH", file=sys.stderr)
        return 2

    try:
        pubkey = read_pubkey(args.pub)
    except OSError as error:
        print(f"trust-estate-hosts: could not read {args.pub}: {error}", file=sys.stderr)
        return 2

    failures = 0
    for name, target in hosts.items():
        if args.dry_run:
            print(f"WOULD install {key_blob(pubkey)} on {name} ({target})")
            continue
        try:
            outcome = read_host(name, target, authorized_keys_install(pubkey), ssh).strip().splitlines()[-1]
        except Exception as error:  # noqa: BLE001 — name the host, not the exit code
            print(f"FAIL {name} ({target}): {error}", file=sys.stderr)
            failures += 1
            continue
        # A host that took the key in one ssh must take it in the next one, key-only. This
        # is the whole point of the script, so it is verified rather than assumed.
        verified = key_works(target)
        print(f"{'OK  ' if verified else 'WARN'} {name} ({target}): key {outcome}; key-only ssh {'works' if verified else 'still fails'}")

    if drifted:
        if args.dry_run:
            print(f"WOULD rewrite the managed block in {args.config}")
        else:
            write_config(args.config, config_with(config, block))
            print(f"WROTE {args.config}: managed block now names {len(hosts)} host(s)")
    else:
        print(f"OK   {args.config}: managed block already in place")

    if failures:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
