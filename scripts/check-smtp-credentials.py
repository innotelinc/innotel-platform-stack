#!/usr/bin/env python3
"""Fail when a running container carries an SMTP password in its environment.

WHY THIS EXISTS
---------------
On 2026-11-20 the estate's mail senders were audited and two of them handed their
submission password to the container as an environment variable. That is not a style
problem: `SMTP_PASS` in the environment is readable by `docker inspect` — by any process
that can talk to the Docker socket, by anything that dumps the container's config into a
ticket, and by a shell history that captured an inspection command. It happened: while
auditing Signara, an inspection that printed the container's env put a live app password
into a transcript, and the password had to be rotated. The fix was a mounted file
(`smtp_auth_password_file` for Alertmanager, `SMTP_PASS_FILE` for the API), and by the
next day the value was in neither container.

Nothing asserted that. The estate has checks for mail *authentication* (SPF/DKIM/DMARC
alignment), for mail *relay*, for mail *identities* and for alert *delivery*, and every
one of them goes green on a stack whose password is in `docker inspect` — the mail
arrives, the check is happy, and the credential is one `docker inspect` away. So this
check reads the thing those checks never look at: the environment of every running
container on every estate host.

THE RULES
---------
1. **A mail credential in a container's environment is a failure.** The key has to name
   a mail transport (`SMTP`, `MAIL`, `EMAIL`) *and* a credential (`PASS`, `PASSWORD`,
   `SECRET`), because this is about the submission password and not about every secret
   the estate keeps in env. An empty value is not a leak and is not a finding — a
   `SMTP_PASS=` line that holds nothing gives nobody anything.
2. **A file-backed name is the fix, not the finding.** `SMTP_PASS_FILE`, `…_PATH`,
   `…_DIR` name where the value lives; those are skipped by construction, which is what
   lets the same check pass on a stack that has been fixed rather than only on one that
   never set the variable.
3. **A named exemption is a note, never silence.** Authentik 2026.8.6 has no file-backed
   secret and no email settings model (only `authentik/lib/default.yml` plus env), so its
   password *can only* be an environment variable; that is the product's limit, not a
   deployment choice, and `docs/estate-mail-senders-audit-2026-11-20.md` says so. The
   exemption is keyed by host, instance and key so it cannot cover a different container
   that starts carrying the same variable, and it is published as a note so the
   deliberate case still reaches the dashboard.
4. **A host that cannot be read is exit 2, never a pass.** An unreachable host is exactly
   where a password can sit unnoticed; its silence is not evidence.

Reading is one `ssh` per host and one `incus exec` per running instance, so a host costs
a second or two. Values are parsed in memory and never printed, published or logged: a
finding names the host, the instance, the container, the variable and the value's length.

Usage:
    ./scripts/check-smtp-credentials.py
    ./scripts/check-smtp-credentials.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-smtp-credentials.py      # where the estate uses passwords
    ./scripts/check-smtp-credentials.py --hosts probe=root@192.168.1.50

Exit codes: 0 = no container has a mail password in its environment, 1 = one does,
2 = a host could not be read (never silently a pass).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, read_host, ssh, write_prom  # noqa: E402

CHECK = "smtp_credentials"

HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: host -> instance -> key -> why a mail password in this environment is not a finding.
#: Empty values are not allowed by the test, so an exemption always carries the reason.
EXEMPT: dict[str, dict[str, dict[str, str]]] = {
    "i4": {
        "proxy": {
            "AUTHENTIK_EMAIL__PASSWORD": (
                "Authentik 2026.8.6 has no file-backed secret and no email settings model "
                "(only authentik/lib/default.yml plus env), so its submission password can "
                "only be an environment variable — a product limit, documented in "
                "docs/estate-mail-senders-audit-2026-11-20.md"
            ),
        },
    },
}

#: What makes a variable a mail transport between the estate and a relay.
TRANSPORT = re.compile(r"SMTP|MAIL|EMAIL")
#: What makes it a credential rather than, say, a hostname or a port.
CREDENTIAL = re.compile(r"PASS|PASSWORD|SECRET")
#: A name that says where the value lives is the fix this check exists to encourage.
FILE_BACKED = ("_FILE", "_PATH", "_DIR")

#: Instances that run no Docker at all are expected: this is the number the run reports so
#: "no findings" cannot mean "nothing was read".
HOST_SCRIPT = r"""
set -u
for inst in $(incus list --format csv -c n,s | awk -F, '$2=="RUNNING"{print $1}'); do
  out=$(incus exec "$inst" -- sh -c \
    'docker ps -q | xargs -r docker inspect --format "{{.Name}}|{{json .Config.Env}}"' 2>/dev/null) || continue
  [ -n "$out" ] || continue
  echo "@@ $inst"
  printf '%s\n' "$out"
done
"""


@dataclass
class Container:
    """One running container as the host reports it: env in memory, never printed."""

    instance: str
    name: str
    env: dict[str, str] = field(default_factory=dict)

    def matches(self) -> list[str]:
        """The variables in this container that are mail passwords with a value."""
        found = []
        for key, value in self.env.items():
            if not value or key.upper().endswith(FILE_BACKED):
                continue
            if TRANSPORT.search(key.upper()) and CREDENTIAL.search(key.upper()):
                found.append(key)
        return sorted(found)


def parse(output: str) -> tuple[list[Container], int]:
    """Parse the host script's output into containers, and count instances read.

    `docker inspect --format '{{.Name}}|{{json .Config.Env}}'` prints one line per
    container: the name, a pipe, and the environment as a JSON array of `KEY=value`. A
    value that itself contains a pipe is safe because only the first pipe is split on.
    """
    containers: list[Container] = []
    instances = 0
    current = ""
    for line in output.splitlines():
        if line.startswith("@@ "):
            current = line[3:].strip()
            instances += 1
            continue
        if not current or "|" not in line:
            continue
        name, _, raw = line.partition("|")
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            continue
        env: dict[str, str] = {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, str) or "=" not in entry:
                continue
            key, _, value = entry.partition("=")
            env[key] = value
        containers.append(Container(current, name.lstrip("/"), env))
    return containers, instances


def audit(views: dict[str, list[Container]]) -> Audit:
    """The rules, over data the caller already has. Pure, so the rules are the test."""
    out = Audit()
    for host, containers in views.items():
        for container in sorted(containers, key=lambda c: (c.instance, c.name)):
            for key in container.matches():
                where = f"{host} {container.instance}: {container.name}"
                reason = EXEMPT.get(host, {}).get(container.instance, {}).get(key)
                if reason:
                    out.findings.append(
                        Finding(
                            "smtp_password_in_env_deliberate",
                            "note",
                            f"{where} carries {key} in its environment, so `docker inspect` "
                            f"shows it — deliberate: {reason}",
                        )
                    )
                    continue
                length = len(container.env[key])
                out.findings.append(
                    Finding(
                        "smtp_password_in_env",
                        "fail",
                        f"{where} carries {key} in its environment ({length} chars), so "
                        f"`docker inspect` prints a live credential — mount the value as a "
                        f"file and point a *_FILE variable at it: `smtp_auth_password_file` "
                        f"for Alertmanager, `<VAR>_FILE` for an app that reads one "
                        f"(docs/estate-mail-senders-audit-2026-11-20.md)",
                    )
                )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            if name.strip():
                hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran)
        except OSError as error:
            print(f"check-smtp-credentials: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-smtp-credentials: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    views: dict[str, list[Container]] = {}
    instances = 0
    try:
        for host, target in hosts.items():
            containers, read = parse(read_host(host, target, HOST_SCRIPT, ssh))
            views[host] = containers
            instances += read
    except Exception as error:
        print(f"check-smtp-credentials: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-smtp-credentials: reads hosts with key-based `ssh`; where the estate "
                "uses passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(views)
    emit(result, ran=True)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    containers = sum(len(c) for c in views.values())
    print(
        f"\ncheck-smtp-credentials: {len(result.failures)} failure(s), "
        f"{len(result.warnings)} warning(s) — read {containers} running container(s) in "
        f"{instances} Docker instance(s) across {len(views)} host(s)"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
