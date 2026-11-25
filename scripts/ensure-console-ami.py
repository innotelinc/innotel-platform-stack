#!/usr/bin/env python3
"""Put the Asterisk Console's AMI settings back when an image update resets them.

WHY THIS EXISTS
---------------
The console (`pbx.innotel.site`) reaches Asterisk over AMI, and what it reaches is stored
in its **own SQLite volume**, not in this repository and not in its container environment.
The image ships seed values (`freepbx`, the `asterisk-console` user) for a different
compose network, and its start-up migration writes them over whatever is stored. Measured
on 2026-11-25: a routine image update put the console back to "Simulated · AMI connection
closed" while the PBX, the sidecar and the published 5038 were all fine.

The console reads no `AMI_*` environment variable at all (checked against the shipped
image: `ADMIN_*`, `DATA_DIR`, `SEED_DEMO`, `MOH_PUBLISH_DIR`, `OMNIROUTE_*` and nothing
else), so "pin it in compose" is not available. What is available is the console's own
HTTP API, which is what its settings page uses — so this job states the two values the
estate depends on, and does nothing at all when they are already right.

WHAT IT ASSERTS, AND WHAT IT DOES NOT
-------------------------------------
It writes `ami_host` and `ami_port` and nothing else: those are the two the migration
resets and the two every other console setting is independent of. The manager credential
is not touched here — it is the console's own row, it is not reset by the same migration,
and rewriting a credential from a scheduled job would be a way to lose one.

It is a *reconciler*, not an alarm: the alarm is `check-pbx-console.py`, which asserts the
link is live and publishes a textfile for the estate's rules. Between this running every
ten minutes and that running every thirty, a reset shows up as one late check at worst.

Run it on the edge (it reads the console's admin password from its container over ssh, the
same way the check does, and calls the console's published port):

    ./scripts/ensure-console-ami.py
    ./scripts/ensure-console-ami.py --dry-run
    ./scripts/ensure-console-ami.py --pbx 192.168.1.52 --ssh-key /root/.ssh/id_ed25519

Exit codes: 0 = already right, or put right; 1 = the console answered and refused the
write; 2 = something could not be read.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import subprocess
import sys
import urllib.error
import urllib.request

CONSOLE_PORT = 8097
AMI_HOST = "192.168.1.52"
AMI_PORT = 5038
#: The two settings the image's seed migration rewrites.
FIELDS = ("ami_host", "ami_port")


class Unreadable(Exception):
    """The state could not be read — which is not the same as needing repair."""


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


def console_container(pbx: str, ssh_key: str | None) -> str:
    """The console container, found by the port it publishes (compose renames it)."""
    names = [line.strip() for line in run(ssh_command(
        pbx, f"docker ps --filter publish={CONSOLE_PORT} --format '{{{{.Names}}}}'",
        ssh_key)).splitlines() if line.strip()]
    if len(names) != 1:
        raise Unreadable(f"{pbx}: expected one container publishing :{CONSOLE_PORT}, "
                         f"found {names or 'none'}")
    return names[0]


def admin_password(pbx: str, container: str, ssh_key: str | None) -> str:
    """The console's own admin password, from its container environment. Never printed."""
    env = run(ssh_command(
        pbx, f"docker inspect {container} --format "
             f"'{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}'", ssh_key))
    for line in env.splitlines():
        key, _, value = line.partition("=")
        if key == "ADMIN_PASSWORD" and value:
            return value
    raise Unreadable(f"{container}: no ADMIN_PASSWORD in its environment, so its settings "
                     "cannot be read or written")


class Console:
    """The console's own HTTP API, the way its settings page uses it."""

    def __init__(self, base: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.call("/api/auth/login", {"username": "admin", "password": password})

    def call(self, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if body is not None:
            # Every write carries the CSRF token the login cookie set; without it the
            # console answers 403 and the repair would look like a refusal.
            for cookie in self.jar:
                if cookie.name == "ac_csrf":
                    headers["X-CSRF"] = cookie.value
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise Unreadable(f"GET {path}: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise Unreadable(f"GET {path}: {error}") from error
        return json.loads(payload or b"{}")

    def settings(self) -> dict:
        return self.call("/api/settings").get("values", {})

    def write(self, values: dict) -> None:
        self.call("/api/settings", values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pbx", default=AMI_HOST, help="the PBX host (default %(default)s)")
    parser.add_argument("--ssh-key", default="/root/.ssh/id_ed25519",
                        help="key for the PBX host (default %(default)s)")
    parser.add_argument("--ami-host", default=AMI_HOST,
                        help="the AMI address to pin (default %(default)s)")
    parser.add_argument("--ami-port", default=str(AMI_PORT),
                        help="the AMI port to pin (default %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change, write nothing")
    args = parser.parse_args(argv)

    wanted = {"ami_host": args.ami_host, "ami_port": str(args.ami_port)}
    try:
        container = console_container(args.pbx, args.ssh_key)
        console = Console(f"http://{args.pbx}:{CONSOLE_PORT}",
                          admin_password(args.pbx, container, args.ssh_key))
        stored = console.settings()
    except Unreadable as error:
        print(f"ensure-console-ami: {error}", file=sys.stderr)
        return 2

    wrong = {key: value for key, value in wanted.items()
             if str(stored.get(key, "")).strip() != value}
    if not wrong:
        print(f"ensure-console-ami: {container}: " +
              ", ".join(f"{key}={value}" for key, value in wanted.items()) + " (unchanged)")
        return 0
    if args.dry_run:
        print(f"ensure-console-ami: would set {wrong} (stored "
              f"{ {key: stored.get(key) for key in FIELDS} })")
        return 0

    try:
        console.write(wrong)
        after = console.settings()
    except Unreadable as error:
        print(f"ensure-console-ami: write refused: {error}", file=sys.stderr)
        return 1
    still = {key: value for key, value in wanted.items()
             if str(after.get(key, "")).strip() != value}
    if still:
        print(f"ensure-console-ami: wrote {wrong} but the console still reads {still}",
              file=sys.stderr)
        return 1
    print(f"ensure-console-ami: {container}: reset to " +
          ", ".join(f"{key}={value}" for key, value in wanted.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
