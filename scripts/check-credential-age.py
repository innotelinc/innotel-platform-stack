#!/usr/bin/env python3
"""Warn when a vault-held credential has not been rotated inside its window.

WHY THIS EXISTS
---------------
The estate's checks prove that the credentials they read still work; none of them asks how
old a credential is. That gap became concrete on 2026-11-16: the Mailjet pair had to be
replaced because it had been exposed, and the replacement could not be made through the
API at all — Mailjet only issues a new master pair in its dashboard (`docs/
mailjet-key-rotation.md`). A credential that is never rotated and never broken is
indistinguishable, from every other check here, from one that was rotated yesterday, so the
only thing that can say "this is overdue" is the vault's own record of when the secret was
last written.

WHAT IT READS
-------------
KV v2 metadata, not the secret: `GET /v1/<mount>/metadata/<name>` returns `updated_time`
(when the current version was written) and the version table. The secret's values are never
fetched, so this check cannot leak what it measures.

`VAULT_ADDR` and `VAULT_TOKEN` come from `/etc/innotel/estate-checks.env` on the edge. The
token is a **read-only** policy limited to `cerulean/metadata/*` — it can read timestamps and
nothing else; the `data` paths are not granted.

THE RULES
---------
1. A secret whose current version is older than `--max-age-days` (60 by default) is a
   **warning**: it still works, which is exactly why nothing else notices it. The window
   sits inside the 90-day rotation policy on purpose — it starts the conversation a month
   before the policy would, so a rotation can be scheduled rather than squeezed in.
2. A secret that is current states its age as a **note**, so "ok" says how fresh the answer
   is and a secret that stops being written does not go quiet.
3. A vault that cannot be read — no token, no address, a denied policy, a transport error —
   is **exit 2**. "Could not read it" is never a pass, and a credential-age series that
   silently disappears is the failure this check exists to make visible.
4. A path that does not exist is a **failure**: the consumer of that path is reading a
   secret the estate believes it has.

Usage:
    ./scripts/check-credential-age.py
    ./scripts/check-credential-age.py --path cerulean/mailjet --max-age-days 90
    ./scripts/check-credential-age.py --prom /var/lib/node_exporter/textfile/credential-age.prom

Exit codes: 0 = every secret is inside its window, 1 = at least one is not, 2 = could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from estate_check import Audit, Finding, write_prom  # noqa: E402

CHECK = "credential_age"
#: The pair that caused this check to exist. `--path` adds to it rather than replacing it,
#: so a second credential does not silently drop the first from the audit.
DEFAULT_PATHS = ("cerulean/mailjet",)
DEFAULT_MAX_AGE_DAYS = 60.0
#: Where a person is told to look when the overdue secret is the one this check was written
#: for. The message names a runbook, never a value.
ROTATION_DOCS = {
    "cerulean/mailjet": "docs/mailjet-key-rotation.md",
}


class VaultUnreadable(RuntimeError):
    """The vault, the token or one path's metadata could not be read — exit 2, not a pass."""


class NoSuchSecret(RuntimeError):
    """The metadata endpoint answered 404: the estate believes in a secret that is gone."""


def split_path(path: str) -> tuple[str, str]:
    """`cerulean/mailjet` → (`cerulean`, `mailjet`); the mount is the first segment."""
    mount, _, name = path.strip("/").partition("/")
    if not mount or not name:
        raise ValueError(f"a vault path needs a mount and a name, got {path!r}")
    return mount, name


def metadata_url(addr: str, path: str) -> str:
    mount, name = split_path(path)
    return f"{addr.rstrip('/')}/v1/{mount}/metadata/{name}"


def top_level(keys: list[str], mount: str) -> list[str]:
    """The secrets in a metadata listing, with the directories left out.

    A listing mixes both (`acme/` beside `mailjet`); a directory has no age of its own, and
    its children are named by `--path` when one is what matters.
    """
    return [f"{mount}/{key}" for key in keys if not key.endswith("/")]


def list_secrets(addr: str, token: str, mount: str = "cerulean",
                 timeout: float = 10.0) -> list[str]:
    """Every top-level secret under `mount`, so a new secret is aged without an edit."""
    url = f"{addr.rstrip('/')}/v1/{mount}/metadata?list=true"
    request = urllib.request.Request(url, headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            keys = json.loads(response.read(512_000))["data"]["keys"]
    except Exception as error:  # noqa: BLE001 — denied, transport, bad JSON: all unreadable
        raise VaultUnreadable(f"listing {mount}/metadata failed: {error}") from error
    return top_level(keys, mount)


def read_metadata(addr: str, token: str, path: str, timeout: float = 10.0) -> dict:
    """The KV v2 metadata document for one path. Never fetches the values."""
    request = urllib.request.Request(metadata_url(addr, path),
                                     headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(64_000))["data"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise NoSuchSecret(path) from error
        raise VaultUnreadable(f"{path}: HTTP {error.code} from {metadata_url(addr, path)}") from error
    except Exception as error:  # noqa: BLE001 — DNS, TLS, timeout, bad JSON: all unreadable
        raise VaultUnreadable(f"{path}: {error}") from error


def parse_time(value: str) -> datetime:
    """Vault writes RFC 3339 with a `Z`; `fromisoformat` needs the offset spelled out.

    The fraction is trimmed to six digits first. Vault reports `updated_time` with
    **nanoseconds** (`2026-11-17T10:14:01.779670597+00:00`), and `fromisoformat` accepts at
    most microseconds on these interpreters, so it raised `ValueError` on every real
    timestamp and the check reported a vault it could not read (measured 2026-11-17, the
    first time this check ran against a live vault). A valid timestamp is not a broken
    vault, and the extra digits are below the resolution of a 60-day window.
    """
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip().replace("Z", "+00:00"))
    return datetime.fromisoformat(text)


def last_written(metadata: dict) -> datetime:
    """When the current version was written, from `updated_time` or the version table.

    `updated_time` is the field the API documents; the version table is the fallback for a
    document that predates it. Either is the fact this check is about — never `created_time`,
    which is when the path first existed and would call a rotated secret old forever.
    """
    if metadata.get("updated_time"):
        return parse_time(metadata["updated_time"])
    current = str(metadata.get("current_version") or "")
    version = (metadata.get("versions") or {}).get(current) or {}
    if version.get("created_time"):
        return parse_time(version["created_time"])
    raise VaultUnreadable("metadata carries neither updated_time nor a current version time")


def age_days(written: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return (now - written).total_seconds() / 86_400


def audit(
    paths: list[str],
    max_age_days: float,
    *,
    addr: str,
    token: str,
    reader=read_metadata,
    now: datetime | None = None,
) -> Audit:
    """One finding per path: a warning when overdue, a note when current."""
    result = Audit()
    now = now or datetime.now(timezone.utc)
    for path in paths:
        try:
            metadata = reader(addr, token, path)
            age = age_days(last_written(metadata), now)
        except NoSuchSecret:
            result.findings.append(Finding(
                "missing-secret", "fail",
                f"{path} has no metadata in the vault — the consumer of that path is reading "
                "a secret the estate believes it holds"))
            continue
        limit = f"{max_age_days:.0f}d"
        if age > max_age_days:
            where = ROTATION_DOCS.get(path)
            result.findings.append(Finding(
                "overdue", "warn",
                f"{path} was last written {age:.0f} days ago (window {limit}) — it still "
                "works, which is why nothing else reports it"
                + (f"; rotate it as {where} describes" if where else "")))
        else:
            result.findings.append(Finding(
                "current", "note",
                f"{path} was last written {age:.0f} days ago, inside the {limit} window"))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", action="append", dest="paths", metavar="MOUNT/NAME",
                        help=f"a vault path to age (repeatable; default {', '.join(DEFAULT_PATHS)})")
    parser.add_argument("--all", action="store_true",
                        help="also age every top-level secret the mount lists")
    parser.add_argument("--mount", default="cerulean",
                        help="the mount --all lists (default cerulean)")
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                        help="how old a secret may be before it is a warning")
    parser.add_argument("--addr", default=os.environ.get("VAULT_ADDR", ""),
                        help="vault address (default $VAULT_ADDR)")
    parser.add_argument("--token", default=os.environ.get("VAULT_TOKEN", ""),
                        help="read-only token (default $VAULT_TOKEN)")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran_ok=ran)
        except OSError as error:
            print(f"check-credential-age: could not write {args.prom}: {error}", file=sys.stderr)

    if not args.addr or not args.token:
        emit(None, ran=False)
        print("check-credential-age: VAULT_ADDR/VAULT_TOKEN are not set — the check carries no "
              "vault credential of its own", file=sys.stderr)
        return 2

    listed: list[str] = []
    if args.all:
        try:
            listed = list_secrets(args.addr, args.token, args.mount, timeout=args.timeout)
        except VaultUnreadable as error:
            emit(None, ran=False)
            print(f"check-credential-age: the vault listing could not be read: {error}",
                  file=sys.stderr)
            return 2
    paths = list(dict.fromkeys([*DEFAULT_PATHS, *(args.paths or []), *listed]))

    try:
        result = audit(paths, args.max_age_days, addr=args.addr, token=args.token,
                       reader=lambda a, t, p: read_metadata(a, t, p, timeout=args.timeout))
    except VaultUnreadable as error:
        emit(None, ran=False)
        print(f"check-credential-age: the vault could not be read: {error}", file=sys.stderr)
        return 2

    emit(result, ran=True)
    if args.json:
        print(json.dumps({
            "findings": [{"code": f.code, "level": f.level, "message": f.message}
                         for f in result.findings],
            "failures": len(result.failures), "warnings": len(result.warnings),
        }, indent=2, sort_keys=True))
        return 0 if result.ok else 1

    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(f"check-credential-age: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s), {len(result.notes)} note(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
