#!/usr/bin/env python3
"""Does the Asterisk Console still have a *live* AMI link, or has it gone simulated?

WHY THIS EXISTS
---------------
`pbx.innotel.site` used to say "Simulated · AMI connection closed" and nothing in the
estate cared: the console answered its own healthcheck, the PBX answered every door, and
the two facts that make the link real were both invisible.

There are three of them, and each lives somewhere nobody reviews:

  * **the `ami-bridge` sidecar**, which is what makes the published `5038` work at all.
    Asterisk binds the manager to `127.0.0.1` inside the container, so the published port
    lands on a port nothing listens on; `alpine/socat` shares FreePBX's netns and forwards
    `15038 → 127.0.0.1:5038`. It is a compose service of its own, and on 2026-11-25 a
    redeploy that stopped early left it `Created` while everything else came up — the
    console went back to "AMI connection closed" with the stack looking healthy.
  * **the console's own settings**, which live in its SQLite volume and not in this
    repository. They were once `freepbx` (a name the console cannot resolve, on a
    different compose network) with the `asterisk-console` user, and read exactly like a
    connection that is merely down.
  * **the console's AMI client**, which reconnects on its own and reports what it saw in
    `/api/dashboard` — the same `ami.connected` the page renders.

WHAT IT ASSERTS
---------------
  1. exactly one `ami-bridge-*` container is running on the PBX host;
  2. the console's stored settings name the PBX host and port, and a manager user;
  3. a login on `<ami_host>:<ami_port>` with those credentials is accepted — the path the
     console itself uses;
  4. `/api/dashboard` reports `ami.connected: true` with no `lastError`;
  5. the reconciler that keeps the settings pinned is itself still on a schedule —
     `console-ami-pin.timer` enabled and firing, and its last run clean. A timer that
     stopped is invisible otherwise: the page stays right until the next image update
     resets the settings, and then it is wrong until somebody notices.

The console's admin password is read from its own container environment over ssh, never
from this repository and never printed: the check logs in the way the page does.

Run it on the edge (it needs the PBX over ssh and the console's HTTP port):

    ./scripts/check-pbx-console.py
    ./scripts/check-pbx-console.py --pbx 192.168.1.52 --ssh-key /root/.ssh/id_ed25519
    ./scripts/check-pbx-console.py --prom ${TEXTFILE_DIR}/estate-check-pbx-console.prom
    ./scripts/check-pbx-console.py --pin-timer ''   # not on the edge, where the timer is

Exit codes: 0 = the link is live, 1 = drift, 2 = something could not be read.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # the estate's shared telemetry contract, beside this file when it is installed
    from estate_check import Audit, Finding, unit_state, write_prom  # noqa: E402
    HAVE_TELEMETRY = True
except ImportError:  # a bare copy still has to run: --prom is then simply unavailable
    Audit = Finding = write_prom = unit_state = None  # type: ignore[assignment]
    HAVE_TELEMETRY = False

#: The `check` label this check publishes under (`estate-checks.yml` alerts on it).
CHECK = "console_ami"

CONSOLE_PORT = 8097
AMI_PORT = 5038
BRIDGE_PREFIX = "ami-bridge"


class Drift(Exception):
    """One assertion did not hold."""


class Unreadable(Exception):
    """The state could not be read — which is not the same as drift."""


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


def bridge_state(pbx: str, ssh_key: str | None) -> tuple[str | None, str | None]:
    """The running bridge's name, and any stopped one, so 'gone' and 'not started' differ."""
    running = [line.strip() for line in run(ssh_command(
        pbx, f"docker ps --format '{{{{.Names}}}}' | grep '^{BRIDGE_PREFIX}-'", ssh_key)
    ).splitlines() if line.strip()]
    stopped = [line.strip() for line in run(ssh_command(
        pbx, f"docker ps -a --format '{{{{.Names}}}}' | grep '^{BRIDGE_PREFIX}-'", ssh_key)
    ).splitlines() if line.strip()]
    if len(running) > 1:
        raise Unreadable(f"{pbx}: {len(running)} {BRIDGE_PREFIX}-* containers running: "
                         f"{', '.join(running)}")
    return (running[0] if running else None,
            next((name for name in stopped if name not in running), None))


def freepbx_container(pbx: str, ssh_key: str | None) -> str:
    """The FreePBX container, which compose renames on every recreate."""
    names = [line.strip() for line in run(ssh_command(
        pbx, "docker ps --format '{{.Names}}' | grep '^freepbx-'", ssh_key)).splitlines()
        if line.strip()]
    if len(names) != 1:
        raise Unreadable(f"{pbx}: expected one freepbx-* container, found {names or 'none'}")
    return names[0]


def stranded_bridge(pbx: str, bridge: str, freepbx: str, ssh_key: str | None) -> str:
    """Does the sidecar still share the *current* FreePBX namespace?

    `network_mode: service:freepbx` holds a reference to one container's sandbox. A
    `docker restart` of FreePBX builds a new sandbox, and the sidecar keeps forwarding
    into the old one — still `Up`, still listening on 15038, and connecting to nothing.
    Measured here on 2026-11-25: every check above it read fine while the console said
    "AMI connection closed".
    """
    mode = run(ssh_command(
        pbx, f"docker inspect {bridge} --format '{{{{.HostConfig.NetworkMode}}}}'",
        ssh_key)).strip()
    freight = run(ssh_command(
        pbx, f"docker inspect {freepbx} --format '{{{{.Id}}}}'", ssh_key)).strip()
    target = mode.split(":", 1)[1] if mode.startswith("container:") else ""
    if target != freight:
        return (f"{bridge} is running in a namespace that is not {freepbx}'s "
                f"({mode or 'no network mode'}): FreePBX was recreated under it, so 15038 "
                f"forwards to a dead sandbox — recreate it with `docker compose up -d "
                "--no-deps ami-bridge`")
    return ""


def admin_password(pbx: str, container: str, ssh_key: str | None) -> str:
    """The console's own admin password, from its container environment.

    Read here and nowhere else: it is a credential the estate already holds, and putting
    a copy in the repository would make this check the thing that leaks it. Never printed.
    """
    env = run(ssh_command(
        pbx, f"docker inspect {container} --format "
             f"'{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}'", ssh_key))
    for line in env.splitlines():
        key, _, value = line.partition("=")
        if key == "ADMIN_PASSWORD" and value:
            return value
    raise Unreadable(f"{container}: no ADMIN_PASSWORD in its environment, so the console "
                     "cannot be asked what its AMI client is doing")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is the answer, not something to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


def http_status(url: str, timeout: int = 10) -> int | None:
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


def console_api(url: str, path: str, password: str, user: str = "admin") -> dict:
    """Log in and read one JSON path, with the cookie the console sets."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps({"username": user, "password": password}).encode()
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/auth/login", data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with opener.open(request, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise Unreadable(f"console login refused: HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise Unreadable(f"console at {url}: {error}") from error
    request = urllib.request.Request(f"{url.rstrip('/')}{path}",
                                     headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=15) as response:
            return json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
        raise Unreadable(f"console {path}: {error}") from error


def ami_login(host: str, port: int, user: str, secret: str, timeout: int = 10) -> str:
    """Speak enough AMI to know the credentials are accepted — what the console does."""
    payload = (f"Action: Login\r\nUsername: {user}\r\nSecret: {secret}\r\n"
               "Events: off\r\n\r\n").encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            banner = sock.recv(256).decode("utf-8", "replace")
            if "Asterisk Call Manager" not in banner:
                return f"{host}:{port} answered, but not with an AMI banner: {banner[:60]!r}"
            sock.sendall(payload)
            answer = sock.recv(512).decode("utf-8", "replace")
    except OSError as error:
        return f"{host}:{port} could not be reached: {error}"
    if "Authentication accepted" in answer or ("Response: Success" in answer
                                               and "Login" in answer):
        return ""
    return f"{host}:{port} refused the console's manager credentials: {answer.strip()[:120]!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the Asterisk Console's live AMI link.")
    parser.add_argument("--pbx", default="192.168.1.52")
    parser.add_argument("--console-url", default=f"http://192.168.1.52:{CONSOLE_PORT}")
    parser.add_argument("--console-user", default="admin")
    parser.add_argument("--ssh-key")
    parser.add_argument("--pin-timer", default="console-ami-pin.timer", metavar="UNIT",
                        help="the reconciler timer to assert, on this host (default "
                             "%(default)s; pass an empty string off the edge)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    audit = Audit() if HAVE_TELEMETRY else None

    def fail(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="fail", message=message))

    def note(code: str, message: str) -> None:
        if audit is not None:
            audit.findings.append(Finding(code=code, level="note", message=message))

    def publish(ran_ok: bool) -> None:
        if args.prom and HAVE_TELEMETRY:
            write_prom(args.prom, CHECK, audit, ran_ok)
        elif args.prom:
            print("check-pbx-console: --prom needs estate_check.py beside this file",
                  file=sys.stderr)

    failures: list[str] = []

    try:
        container = console_container(args.pbx, args.ssh_key)
        running_bridge, stopped_bridge = bridge_state(args.pbx, args.ssh_key)
        password = admin_password(args.pbx, container, args.ssh_key)
    except Unreadable as error:
        print(f"FAIL  {error}", file=sys.stderr)
        publish(False)
        return 2

    # 1. The sidecar that makes the published port real — running, and in *this* FreePBX's
    # namespace, because "running" survived the fault that took the link down.
    if running_bridge:
        try:
            freepbx = freepbx_container(args.pbx, args.ssh_key)
            stranded = stranded_bridge(args.pbx, running_bridge, freepbx, args.ssh_key)
        except Unreadable as error:
            print(f"FAIL  {error}", file=sys.stderr)
            publish(False)
            return 2
        if stranded:
            failures.append(stranded)
            fail("ami_bridge_netns", stranded)
            print(f"FAIL  {'ami-bridge':24} stranded in an old network namespace")
        else:
            print(f"PASS  {'ami-bridge':24} running beside FreePBX")
    else:
        detail = (f"{stopped_bridge} exists but is not running — the published :{AMI_PORT} "
                  "has nothing behind it" if stopped_bridge else
                  "no ami-bridge-* container exists at all")
        failures.append(f"{args.pbx}: {detail}")
        fail("ami_bridge", f"{args.pbx}: {detail}")
        print(f"FAIL  {'ami-bridge':24} not running")

    # 2–4. What the console believes and what the wire says.
    try:
        settings = console_api(args.console_url, "/api/settings", password,
                               args.console_user)
        dashboard = console_api(args.console_url, "/api/dashboard", password,
                                args.console_user)
    except Unreadable as error:
        print(f"FAIL  {error}", file=sys.stderr)
        if audit is not None:
            audit.findings.append(Finding(code="console", level="fail", message=str(error)))
        publish(False)
        return 2

    values = settings.get("values") or settings
    host = str(values.get("ami_host") or "")
    port = str(values.get("ami_port") or "")
    user = str(values.get("ami_user") or "")
    secret = str(values.get("ami_secret") or "")

    problems = []
    if host != args.pbx:
        problems.append(f"ami_host is {host!r}, not the PBX ({args.pbx}) — a compose DNS "
                        "name or a loopback address is not reachable from the console")
    if port != str(AMI_PORT):
        problems.append(f"ami_port is {port!r}, not {AMI_PORT}")
    if not user or not secret:
        problems.append("ami_user/ami_secret is empty, so the console is in its simulated "
                        "mode whatever the wire says")
    if problems:
        failures += problems
        for problem in problems:
            fail("settings", problem)
        print(f"FAIL  {'AMI settings':24} {problems[0]}")
    else:
        print(f"PASS  {'AMI settings':24} {host}:{port} as {user}")

    if not problems and running_bridge:
        wire = ami_login(host, int(port), user, secret)
        if wire:
            failures.append(wire)
            fail("ami_login", wire)
            print(f"FAIL  {'AMI login':24} refused")
        else:
            print(f"PASS  {'AMI login':24} accepted on {host}:{port}")
    elif problems:
        print(f"SKIP  {'AMI login':24} settings are not usable")

    ami = (dashboard.get("ami") or {})
    connected = bool(ami.get("connected"))
    last_error = ami.get("lastError")
    if connected:
        print(f"PASS  {'console client':24} /api/dashboard says connected")
    else:
        message = (f"the console's own AMI client is not connected"
                   f"{f' (lastError: {last_error})' if last_error else ''} — the page shows "
                   "\"Simulated / AMI connection closed\"")
        failures.append(message)
        fail("console_client", message)
        print(f"FAIL  {'console client':24} not connected")
    if connected and last_error:
        note("last_error", f"connected now, but the last error was {last_error!r}")

    # 5. The reconciler that makes the settings durable is itself on a schedule. Read here
    # rather than over ssh because it is installed on the host this check runs on — the
    # edge — and a job nobody reads is the same silence one layer out.
    if args.pin_timer:
        if not HAVE_TELEMETRY or unit_state is None:
            print(f"NOTE  {'AMI pin timer':24} needs estate_check.py beside this file")
        else:
            try:
                timer = unit_state(args.pin_timer)
                last = unit_state(args.pin_timer.removesuffix(".timer") + ".service")
            except RuntimeError as error:
                print(f"FAIL  {error}", file=sys.stderr)
                publish(False)
                return 2
            problems = []
            if timer.load != "loaded" or timer.enabled != "enabled":
                problems.append(
                    f"{args.pin_timer} is not enabled (LoadState={timer.load or 'unreadable'}, "
                    f"UnitFileState={timer.enabled or 'none'}) — an image update's reset "
                    "would not be repaired until somebody runs the job by hand")
            elif timer.active != "active":
                problems.append(f"{args.pin_timer} is enabled but not active "
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
                    fail("ami_pin_timer", problem)
                print(f"FAIL  {'AMI pin timer':24} {problems[0]}")
            else:
                print(f"PASS  {'AMI pin timer':24} {args.pin_timer} enabled and firing")

    for failure in failures:
        print(f"      {failure}")
    print()
    print("the console's AMI link is live" if not failures
          else f"{len(failures)} assertion(s) no longer hold")
    publish(True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
