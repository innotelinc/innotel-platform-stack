#!/usr/bin/env python3
"""Drill the PBX's break-glass path: close one name's gate, measure, put it back.

The question this answers is the one `docs/pbx-sso-2026-11-25.md` claims but nothing
re-tests: when the Authentik outpost cannot be reached, does the edge **fail closed** (no
password form, 500) and is the LAN flag still the only way in?

It is deliberately one name at a time. The drill points that name's
`location /outpost.goauthentik.io` at a closed port through the NPM API, measures, and
restores in a `finally` — every other name keeps its own outpost throughout, so the blast
radius is one host for the length of five requests.

What it measures:

    edge  /admin/config.php, gate unreachable   -> 500  (never the form)
    LAN   :8083, flag absent                    -> 403
    LAN   :8083, flag armed                     -> 200  (the FreePBX form)
    LAN   :8083, flag disarmed again            -> 403
    edge  /admin/config.php, restored           -> 302  (back to Authentik)

The flag is armed through `docker exec` on the PBX host, and if it was already armed when
the drill started it is left armed and the run says so: the drill does not tidy away a
state somebody chose.

    ./scripts/drill-pbx-breakglass.py --name pbx.telno.services \
        --token-file /root/.npm-token --ssh-key /root/.ssh/id_ed25519

Exit codes: 0 = every measurement matched, 1 = at least one did not, 2 = the drill could
not run, 3 = **the restore failed** (the name is still broken; fix it by hand).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEAD_PORT = 9
FLAG = "/run/freepbx-sso-break-glass"
OUTPOST = "proxy_pass http://192.168.1.127:9000/outpost.goauthentik.io;"
NPM_FIELDS = (
    "domain_names forward_scheme forward_host forward_port access_list_id "
    "certificate_id ssl_forced caching_enabled block_exploits advanced_config meta "
    "allow_websocket_upgrade http2_support enabled locations hsts_enabled "
    "hsts_subdomains"
).split()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 302 is the answer here, not something to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


def status(url: str, timeout: int = 10) -> int | None:
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


def npm(url: str, token: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:120]
        except Exception:  # noqa: BLE001 — a body we cannot read is not worth failing on
            pass
        hint = (" — NPM answers 500 for a token it does not know, so check --token-file"
                if error.code == 500 else "")
        raise RuntimeError(f"NPM {method} {path}: HTTP {error.code}{hint} {detail}") from error


def ssh(pbx: str, command: str, key: str | None) -> subprocess.CompletedProcess:
    prefix = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
              "-o", "ConnectTimeout=10"]
    if key:
        prefix += ["-i", key, "-o", "IdentitiesOnly=yes"]
    return subprocess.run([*prefix, f"root@{pbx}", command], capture_output=True,
                          text=True, timeout=40)


def flag_state(pbx: str, container: str, key: str | None) -> bool:
    done = ssh(pbx, f"docker exec {container} test -e {FLAG} && echo armed || echo disarmed",
               key)
    return done.stdout.strip() == "armed"


def set_flag(pbx: str, container: str, key: str | None, armed: bool) -> None:
    command = ("touch" if armed else "rm -f") + f" {FLAG}"
    done = ssh(pbx, f"docker exec {container} {command}", key)
    if done.returncode != 0:
        raise RuntimeError(f"{'arming' if armed else 'disarming'} the flag failed: "
                           f"{done.stderr.strip()}")


def freepbx_container(pbx: str, key: str | None) -> str:
    done = ssh(pbx, "docker ps --format '{{.Names}}' | grep '^freepbx-'", key)
    names = [line for line in done.stdout.splitlines() if line.strip()]
    if len(names) != 1:
        raise RuntimeError(f"{pbx}: expected one freepbx-* container, found {names or 'none'}")
    return names[0]


def wait_for(url: str, wanted: int, seconds: int = 60) -> int | None:
    """Poll until `url` answers `wanted`, returning the last status seen."""
    last = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        last = status(url)
        if last == wanted:
            return last
        time.sleep(2)
    return last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drill the PBX break-glass path.")
    parser.add_argument("--name", default="pbx.telno.services",
                        help="the one vhost to make unreachable")
    parser.add_argument("--npm-url", default="http://127.0.0.1:81")
    parser.add_argument("--token-file", default="/root/.npm-token")
    parser.add_argument("--pbx", default="192.168.1.52")
    parser.add_argument("--ssh-key")
    parser.add_argument("--lan-port", type=int, default=8083)
    args = parser.parse_args(argv)

    try:
        with open(args.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as error:
        print(f"cannot read the NPM token: {error}", file=sys.stderr)
        return 2

    try:
        hosts = npm(args.npm_url, token, "GET", "/api/nginx/proxy-hosts")
        host = next((h for h in hosts if args.name in (h.get("domain_names") or [])), None)
        if host is None:
            print(f"no NPM host serves {args.name}", file=sys.stderr)
            return 2
        original = host.get("advanced_config") or ""
        if OUTPOST not in original:
            print(f"{args.name}: no outpost location at {OUTPOST} — this host does not "
                  "use the pattern the drill closes", file=sys.stderr)
            return 2
        broken = original.replace(OUTPOST,
                                 OUTPOST.replace("192.168.1.127:9000",
                                                 f"127.0.0.1:{DEAD_PORT}"))
        if broken == original:
            print("the broken config is identical to the original; refusing", file=sys.stderr)
            return 2
        container = freepbx_container(args.pbx, args.ssh_key)
        was_armed = flag_state(args.pbx, container, args.ssh_key)
        if was_armed:
            print(f"note: {FLAG} was already armed; it will be left armed", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 — any of these is "could not run"
        print(f"drill could not run: {error}", file=sys.stderr)
        return 2

    edge_url = f"https://{args.name}/admin/config.php"
    lan_url = f"http://{args.pbx}:{args.lan_port}/admin/config.php"
    results: list[tuple[str, int | None, int]] = []
    restore_ok = False
    try:
        body = {key: host[key] for key in NPM_FIELDS}
        body["advanced_config"] = broken
        npm(args.npm_url, token, "PUT", f"/api/nginx/proxy-hosts/{host['id']}", body)

        results.append(("edge, gate unreachable", wait_for(edge_url, 500, 30), 500))
        results.append(("LAN :8083, flag absent", status(lan_url), 403))
        set_flag(args.pbx, container, args.ssh_key, True)
        results.append(("LAN :8083, flag armed", wait_for(lan_url, 200, 20), 200))
        set_flag(args.pbx, container, args.ssh_key, False)
        results.append(("LAN :8083, flag disarmed", wait_for(lan_url, 403, 20), 403))
    finally:
        try:
            body = {key: host[key] for key in NPM_FIELDS}
            body["advanced_config"] = original
            npm(args.npm_url, token, "PUT", f"/api/nginx/proxy-hosts/{host['id']}", body)
            results.append(("edge, restored", wait_for(edge_url, 302, 60), 302))
            restore_ok = results[-1][1] == 302
        except Exception as error:  # noqa: BLE001 — the restore is the one thing that matters
            print(f"RESTORE FAILED for {args.name}: {error}", file=sys.stderr)
        finally:
            if not was_armed:
                try:
                    set_flag(args.pbx, container, args.ssh_key, False)
                except Exception as error:  # noqa: BLE001
                    print(f"could not make sure the flag is disarmed: {error}",
                          file=sys.stderr)

    mismatched = 0
    print(f"\n{args.name}\n")
    for label, got, want in results:
        ok = got == want
        mismatched += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'}  {label:28} got {got}, expected {want}")

    if not restore_ok:
        print(f"\n{args.name} is still pointed at the closed port — fix it by hand",
              file=sys.stderr)
        return 3
    if was_armed:
        print(f"\nnote: {FLAG} is armed, as it was when the drill started")
    print("\nthe gate fails closed and the LAN flag is the way in" if not mismatched
          else f"\n{mismatched} measurement(s) did not match")
    return 0 if not mismatched else 1


if __name__ == "__main__":
    sys.exit(main())
