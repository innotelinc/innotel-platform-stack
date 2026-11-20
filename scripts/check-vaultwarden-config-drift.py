#!/usr/bin/env python3
"""Fail when a Vaultwarden environment variable disagrees with the config.json it loads.

WHY THIS EXISTS
---------------
Vaultwarden keeps its settings in `/data/config.json` (written by the `/admin` panel) and
that store **overrides the environment**, which the daemon announces on every start:

    [INFO] Using saved config from `data/config.json` for configuration.
    [WARNING] The following environment variables are being overridden by the config.json file.

That makes the compose file advisory for any key the store also holds. Measured on
2026-11-20, two settings were silently wrong for that reason and neither produced a log
line an operator would notice:

* `SMTP_PASSWORD` was in the compose environment and in `config.json`. The app never read
  the environment copy, so it was one more readable credential (`docker inspect`) that a
  rotation would have left stale — the mail-credential sweep could see it but nothing
  could see that it was inert.
* `SIGNUPS_VERIFY` was `"false"` in compose, with the comment that a registrar confirms a
  staff address so sign-up must not wait for a mailed token — while the store said `true`.
  The file described one behaviour and the app did the opposite, and registration waited
  for mail nobody expected it to need.

Every check around this one reads a *service*: that mail authenticates, that containers
are addressed correctly, that sign-in callbacks fit. None of them compares the two writable
surfaces of a `config.json`-backed service, and the disagreement is invisible from either
side alone — the compose file reads correctly and the running app behaves consistently.

THE RULES
---------
1. **Only keys both surfaces carry are compared.** A key in `config.json` alone is a panel
   setting; a key in the environment alone takes effect normally. The trap is the overlap.
2. **A difference is a failure, not a warning.** Whichever value is "right", the two files
   disagree about it and the app does not implement what the file says. That is the defect
   this check was written for, so it pages.
3. **A secret is compared, never printed.** Values whose key names a credential are
   compared as strings and reported by length only.
4. **No overlap is a failure.** If the store and the environment share no key at all, this
   check has read the wrong file, the wrong container or the wrong instantiation — it must
   not report that as agreement (the same rule the mail-credential sweep uses for a host it
   could not read).
5. **An unreadable host is exit 2.** Same reason: silence is not a pass.

Usage:
    ./scripts/check-vaultwarden-config-drift.py
    ./scripts/check-vaultwarden-config-drift.py --prom /path/to/textfile
    ./scripts/check-vaultwarden-config-drift.py --compose /usr/src/vaultwarden/docker-compose.yml

Exit codes: 0 = every shared key agrees, 1 = one disagrees, 2 = a host could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, HostUnreadable, read_host, write_prom  # noqa: E402

CHECK = "vaultwarden_config_drift"

#: Where the stack runs. `vault` is the instance and `vaultwarden` the container inside it.
HOST = "i1"
TARGET = "root@192.168.1.51"
INSTANCE = "vault"
CONTAINER = "vaultwarden"
COMPOSE = "/usr/src/vaultwarden/docker-compose.yml"
CONFIG = "/data/config.json"
#: Both paths are read **inside** the instance: the compose file is on the `vault`
#: instance's filesystem, not the host's, which is what the first run of this check got
#: wrong (it exited 2 with `cat: no such file` on i1 and was right to).

#: A key naming a credential is compared in memory and published by length only.
SECRET = re.compile(r"PASS|SECRET|TOKEN|KEY", re.IGNORECASE)


def parse_compose_environment(text: str) -> dict[str, str]:
    """The first service's `environment:` block as KEY -> value.

    Deliberately not a YAML parser. The edge has no PyYAML, and the block is a list of
    `KEY: "value"` lines; a mapping-style block (`KEY: value`) is what this stack uses.
    Comments and blank lines are skipped, and a line indented less than an entry ends the
    block, so a later service's environment is not mistaken for this one's.
    """
    environment: dict[str, str] = {}
    inside = False
    seen = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^\s{4}environment:\s*$", raw):
            if seen:
                # Only the first service's block is this check's subject; a second block
                # (another service in the same file) must not contribute or win.
                break
            seen = inside = True
            continue
        if not inside:
            continue
        entry = re.match(r"^\s{6,}([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", raw)
        if entry:
            value = entry.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            environment[entry.group(1)] = value
        elif re.match(r"^\s{0,4}\S", raw):
            inside = False
    return environment


def compare(environment: dict[str, str], config: dict[str, object]) -> list[tuple[str, str, str]]:
    """The shared keys whose values disagree, as (key, environment, config.json).

    Credential values are replaced by their length before they leave this function.
    """
    stored_keys = {name.lower(): name for name in config}
    differing: list[tuple[str, str, str]] = []
    for key in sorted(environment):
        name = stored_keys.get(key.lower())
        if name is None:
            continue
        from_env = environment[key]
        from_config = config[name]
        if SECRET.search(key):
            if str(from_config) != from_env:
                differing.append((key, f"<{len(from_env)} chars>", f"<{len(str(from_config))} chars>"))
        elif str(from_config).lower() != from_env.lower():
            differing.append((key, from_env, str(from_config)))
    return differing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=HOST, help="estate host name, for the finding text")
    parser.add_argument("--target", default=TARGET, help="ssh target")
    parser.add_argument("--instance", default=INSTANCE, help="incus instance holding the container")
    parser.add_argument("--container", default=CONTAINER, help="container name")
    parser.add_argument("--compose", default=COMPOSE, help="compose file on the host")
    parser.add_argument("--config", default=CONFIG, help="config.json inside the container")
    parser.add_argument("--prom", help="write the textfile here")
    args = parser.parse_args()

    audit = Audit()
    read = False
    try:
        compose_text = read_host(
            args.host,
            args.target,
            f"incus exec {args.instance} -- cat {args.compose}",
        )
        config_text = read_host(
            args.host,
            args.target,
            f"incus exec {args.instance} -- docker exec {args.container} cat {args.config}",
        )
        environment = parse_compose_environment(compose_text)
        config = json.loads(config_text)
        if not isinstance(config, dict):
            raise ValueError("config.json is not an object")
        read = True
    except HostUnreadable as error:
        print(f"check-vaultwarden-config-drift: {error}", file=sys.stderr)
    except (ValueError, OSError) as error:
        print(f"check-vaultwarden-config-drift: could not parse what it read: {error}", file=sys.stderr)

    shared = 0
    if read:
        stored = {name.lower() for name in config}
        shared = sum(1 for key in environment if key.lower() in stored)
        if shared == 0:
            audit.findings.append(
                Finding(
                    "no-overlap",
                    "fail",
                    f"{args.container}: the compose environment and {args.config} share no key at all "
                    f"({len(environment)} variable(s) vs {len(config)} setting(s)) — this check read "
                    "the wrong file, container or instance, so it cannot report agreement",
                )
            )
        for key, from_env, from_config in compare(environment, config):
            audit.findings.append(
                Finding(
                    key.lower(),
                    "fail",
                    f"{args.container}: {key} is {from_env} in {args.compose} but {from_config} in "
                    f"{args.config}, and the config.json wins — the file describes behaviour the app "
                    "does not have",
                )
            )

    for finding in audit.findings:
        print(f"{finding.level.upper():5} {finding.message}")
    suffix = f" — {shared} shared key(s) between the compose file and config.json" if read else ""
    print(f"check-vaultwarden-config-drift: {len(audit.failures)} failure(s){suffix}")

    if args.prom:
        write_prom(args.prom, CHECK, audit if read else None, read)
    return 0 if read and audit.ok else (2 if not read else 1)


if __name__ == "__main__":
    sys.exit(main())
