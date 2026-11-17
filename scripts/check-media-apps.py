#!/usr/bin/env python3
"""Fail when a media app that a request has to cross is not running.

WHY THIS EXISTS
---------------
On 2026-11-17 `req.innotel.us` answered 200, Jellyseerr's own door was open and its Jellyfin
library sync ran on schedule — and every request still failed. The app behind the request
was Radarr, and its `/config/config.xml` was **zero bytes**. `monarch` was under memory
pressure and the kernel OOM killer took a container scope at 00:16:42; the kill landed
while Radarr was writing its config, so the file was left truncated. Radarr then refused
to start (`/config/config.xml is corrupt or invalid`) and the container still read `Up` in
`docker ps` while the process sat at `Press enter to exit...`.

Nothing saw it, because every check that looks at the media estate looks at **doors**.
`check-media-doors.py` asks whether `media.` and `iptv.` answer 200, and they did. A
request is the one path that crosses apps — Jellyseerr → Radarr/Sonarr → Prowlarr →
SABnzbd — so a dead app in the middle is invisible from every door in front of it.

WHAT IT ASSERTS
---------------
For every app a request must cross, each named in `EXPECTED` with the reason it is there:

  1. RUNNING — the container exists and `.State.Status` is `running`. `exited`,
     `created`, `restarting`, `dead` and a container that is not there at all are
     failures, because `docker ps` prints `Up` for a container whose process has already
     given up.
  2. RESTARTS ARE REPORTED, NOT PAGED — a running container's `RestartCount` is published
     as its own series and raised as a warning. It is cumulative for the container's life,
     so treating it as a failure would leave an app permanently red for a restart that
     healed hours ago; the failure is the app not running now.

THE REPAIR IT NAMES
-------------------
The state is recoverable without losing data. Each app keeps scheduled backups and every
backup carries `config.xml`, which holds the API key the request UI is paired with:

    cd /docker/appdata/<app> && unzip -p Backups/scheduled/<newest>.zip config.xml \\
      > config.xml

then restart the container. **Do not delete the file instead**: the app regenerates one
with a *new* API key, and Jellyseerr keeps its own copy pointing at the old one — a dead
request path that looks like nothing at all in either app's UI.

Run it where the container lives (`monarch`, on `i2`):

    ./scripts/check-media-apps.py
    ./scripts/check-media-apps.py --prom ${TEXTFILE_DIR}/estate-check-media-apps.prom
    ./scripts/check-media-apps.py --json

Exit codes: 0 = every app a request crosses is running, 1 = one is not, 2 = the check
could not run (a host unreachable is never silently a pass).
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, read_host, write_prom  # noqa: E402

CHECK = "media_apps"

#: Where the media estate lives: the `monarch` container, on `i2`.
HOST = "i2"
TARGET = "root@192.168.1.52"
CONTAINER = "monarch"

#: Every app a request crosses, in the order it crosses them, each with the reason it is
#: on the list. A name here is a container that must be running; the reasons are what a
#: reader needs to know which user-visible path died with it.
EXPECTED: tuple[tuple[str, str], ...] = (
    ("jellyfin", "the server every player and the request UI read the library from"),
    ("jellyseerr", "the request UI behind req.innotel.us"),
    ("radarr", "what a movie request is handed to"),
    ("sonarr", "what a TV request is handed to"),
    ("prowlarr", "the indexers both request apps search"),
    ("sabnzbd", "the downloader the request apps hand off to"),
)

#: Each app's own settings file, as the path *inside* its container. A zero-byte file here
#: is the exact state that made this check necessary: Radarr reads `config.xml` before it
#: will start, so a truncated one turns a running app into one that refuses to boot, while
#: `docker ps` still prints `Up` for a container whose process has already given up.
CONFIG_FILES: tuple[tuple[str, str], ...] = (
    ("jellyfin", "/config/system.xml"),  # hotio image keeps it at the data root, not /config/config
    ("jellyseerr", "/app/config/settings.json"),
    ("radarr", "/config/config.xml"),
    ("sonarr", "/config/config.xml"),
    ("prowlarr", "/config/config.xml"),
    ("sabnzbd", "/config/sabnzbd.ini"),
)


@dataclass
class AppState:
    """One container as `docker inspect` described it."""

    name: str
    state: str
    restarts: int

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def missing(self) -> bool:
        return self.state in ("", "missing")


def command(names: tuple[str, ...] | None = None) -> str:
    """The one remote command: `docker inspect` each expected app, one line each.

    `docker ps --format` cannot print `RestartCount`, and its `Up` is exactly the reading
    that hid this failure, so the state comes from `inspect`. A container that does not
    exist yields the `|| echo` line rather than a non-zero exit that would read as a host
    that could not be read.
    """
    wanted = names if names is not None else tuple(name for name, _reason in EXPECTED)
    inspect = (
        'docker inspect --format '
        '"{{.Name}}|{{.State.Status}}|{{.RestartCount}}|{{.State.Running}}" "$c" '
        '2>/dev/null || echo "$c|missing|0|false"'
    )
    loop = f'for c in {" ".join(wanted)}; do {inspect}; done'
    # The config probe rides behind the same loop: two fields per line, which `parse`
    # skips and `parse_configs` reads, so the whole check stays one round trip.
    pairs = " ".join(f"{name}:{path}" for name, path in CONFIG_FILES)
    configs = (
        f"for p in {pairs}; do a=${{p%%:*}}; f=${{p#*:}}; "
        'sz=$(docker exec "$a" stat -c %s "$f" 2>/dev/null) || sz=missing; '
        'printf "%s|%s\\n" "$a" "$sz"; done'
    )
    # `shlex.quote`, not `json.dumps`: the payload must reach the container shell
    # with `$c` still literal. Through a double-quoted `sh -c` the host shell expands
    # it first, `docker inspect` gets no argument, every app reads as missing and the
    # check reports six failures that are its own (2026-11-17).
    return f"incus exec {CONTAINER} -- sh -c {shlex.quote(loop + '; ' + configs)}"


def parse(text: str) -> dict[str, AppState]:
    """`name -> AppState` from the raw inspect output. Pure, so the parser is under test.

    Lines are `name|state|restarts|running` with the leading `/` docker puts on
    `.Name`. Anything else — a shell warning, an empty line — is skipped rather than
    guessed at.
    """
    out: dict[str, AppState] = {}
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 4:
            continue
        name = fields[0].lstrip("/")
        if not name:
            continue
        try:
            restarts = int(fields[2])
        except ValueError:
            restarts = 0
        out[name] = AppState(name=name, state=fields[1], restarts=restarts)
    return out


def parse_configs(text: str) -> dict[str, int | None]:
    """`name -> config size in bytes` from the same output, `None` when unreadable.

    A two-field line is a config probe; anything else belongs to the state parser and is
    ignored here. `None` (missing, unreadable, no `stat`) is distinct from `0`, because
    the repair differs: `0` is a truncation to restore, `None` is a path that is not there.
    """
    out: dict[str, int | None] = {}
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 2:
            continue
        name = fields[0].lstrip("/")
        if not name:
            continue
        try:
            out[name] = int(fields[1])
        except ValueError:
            out[name] = None
    return out


def audit(
    observed: dict[str, AppState], configs: dict[str, int | None] | None = None
) -> Audit:
    """Every expected app that a request cannot currently reach, as findings."""
    out = Audit()
    config_paths = dict(CONFIG_FILES)
    for name, reason in EXPECTED:
        app = observed.get(name)
        if app is None or app.missing:
            out.findings.append(
                Finding(
                    "not_running",
                    "fail",
                    f"{name} ({reason}) has no container on {CONTAINER} at all — "
                    f"every request that needs this hop fails",
                )
            )
            continue
        if not app.running:
            out.findings.append(
                Finding(
                    "not_running",
                    "fail",
                    f"{name} ({reason}) is {app.state}, not running — `docker ps` still "
                    f"prints Up for it; `docker logs {name}` on {CONTAINER} says why",
                )
            )
            continue
        if configs is not None:
            path = config_paths.get(name)
            size = configs.get(name)
            if path is not None and size is None:
                out.findings.append(
                    Finding(
                        "config_unreadable",
                        "fail",
                        f"{name} ({reason}) is running but has no readable {path} — the "
                        f"process is up with no settings, so the request path it serves is "
                        f"not the one you configured",
                    )
                )
            elif path is not None and size == 0:
                out.findings.append(
                    Finding(
                        "config_empty",
                        "fail",
                        f"{name} ({reason}) has a zero-byte {path} — the shape an OOM kill "
                        f"leaves behind when it lands mid-write, and the app refuses to start "
                        f"after its next restart; restore it from Backups/scheduled rather "
                        f"than deleting it, which would mint a new API key",
                    )
                )
        if app.restarts:
            out.findings.append(
                Finding(
                    "restarted",
                    "warn",
                    f"{name} ({reason}) is running but has restarted {app.restarts} time(s) "
                    f"— if it dies again, check its config file is not truncated",
                )
            )
    return out


def prometheus_extra(
    observed: dict[str, AppState], configs: dict[str, int | None] | None = None
) -> str:
    """This check's own series, on top of the shared `innotel_estate_check` contract."""
    lines = [
        "# HELP innotel_estate_media_app_running 1 when the app a request crosses is running",
        "# TYPE innotel_estate_media_app_running gauge",
    ]
    for name, _reason in EXPECTED:
        app = observed.get(name)
        lines.append(f'innotel_estate_media_app_running{{app="{name}"}} {1 if app and app.running else 0}')
    lines += [
        "# HELP innotel_estate_media_app_restarts RestartCount of each app a request crosses",
        "# TYPE innotel_estate_media_app_restarts gauge",
    ]
    for name, _reason in EXPECTED:
        app = observed.get(name)
        lines.append(f'innotel_estate_media_app_restarts{{app="{name}"}} {app.restarts if app else 0}')
    lines += [
        "# HELP innotel_estate_media_app_config_bytes Size of each app's own config file, -1 when unreadable",
        "# TYPE innotel_estate_media_app_config_bytes gauge",
    ]
    for name, _reason in EXPECTED:
        size = (configs or {}).get(name)
        lines.append(
            f'innotel_estate_media_app_config_bytes{{app="{name}"}} {-1 if size is None else size}'
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=HOST, help=f"the host holding the container (default {HOST})")
    parser.add_argument("--target", default=TARGET, help=f"ssh target for it (default {TARGET})")
    parser.add_argument("--container", default=CONTAINER, help=f"incus container (default {CONTAINER})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--json", action="store_true", help="print the findings as JSON")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool, extra: str | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=extra)
            except OSError as error:
                print(f"check-media-apps: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-media-apps: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        text = read_host(args.host, args.target, command())
        observed = parse(text)
        configs = parse_configs(text)
    except Exception as error:  # noqa: BLE001 — any ssh failure is the same finding here
        print(f"check-media-apps: could not read {args.host}: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    result = audit(observed, configs)
    if args.json:
        print(json.dumps([f.__dict__ for f in result.findings], indent=2))
    else:
        running = sum(1 for name, _r in EXPECTED if (observed.get(name) or AppState(name, "", 0)).running)
        truncated = sum(1 for name, _r in EXPECTED if configs.get(name) == 0)
        print(f"check-media-apps: {running}/{len(EXPECTED)} app(s) a request crosses are running "
              f"on {CONTAINER}, {len(result.failures)} finding(s), {truncated} with an empty config")
        for finding in result.findings:
            print(f"{finding.level.upper()} {finding.message}")

    emit(result, ran=True, extra=prometheus_extra(observed, configs))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
