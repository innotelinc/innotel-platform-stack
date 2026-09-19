#!/usr/bin/env python3
"""Audit .env credentials for values that cannot survive a URL.

Why this exists: dograh builds its ARI WebSocket URL by interpolating the stored
password into a query string — ``ws://host:8088/ari/events?api_key=user:pw&app=x``
— and a ``#`` in that password ends the query as a URI fragment, so the client
rejects the whole URL (``InvalidURI: fragment identifier is meaningless``). The
symptom appears nowhere near the cause: the PBX just stops seeing an ARI client,
calls routed into its Stasis app hang up, and the only clue is a red banner that
says a template or a service is missing. Capstone carried exactly that for a
while, which is what this script exists to catch before it bites again.

What it classifies:

  ERROR — a value that is *used inside a URL* and cannot survive it:
            * any ``*_URL`` / ``*_URI`` / ``*_DSN`` / ``*_ENDPOINT`` whose own
              value contains a ``#`` (everything after it is a fragment)
            * any key that a URL value references as ``${KEY}`` or ``$KEY``
              (that is how a credential gets into a URL without the URL looking
              like it holds one — the ARI password case) where the referenced
              value contains ``#`` or whitespace
            * ``DATABASE_URL``-style values with a raw space in the userinfo
  WARN  — a credential-shaped key whose value contains ``#`` or whitespace:
            dangerous when any call site puts it in a URL or in a single-quoted
            shell assignment, and impossible to tell from the .env alone

Exit status is 1 when anything is reported, so a host can run it on a schedule
or a repo can gate CI with it.

Usage:

    python3 ips/scripts/env-credential-audit.py                 # .env files below cwd
    python3 ips/scripts/env-credential-audit.py path/to/.env    # explicit files
    python3 ips/scripts/env-credential-audit.py --root /srv     # scan a tree
    python3 ips/scripts/env-credential-audit.py --quiet         # errors only

Only key *names* and the kind of problem are printed. A value is never echoed —
an audit that leaks the secrets it is auditing is not an improvement.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Keys whose name says "this is a credential". `KEY` is included but matched
# case-sensitively-ish by intent: a bare `KEY=` in a .env is usually an API key,
# while `MONKEY` should not match, hence the word boundaries below.
CREDENTIAL = re.compile(r"(PASS|PASSWORD|PWD|SECRET|TOKEN|APIKEY|API_KEY|_KEY$|^KEY$)", re.I)
# A plural key holds a *list* of credentials (space- or comma-separated), so a
# space inside it is a delimiter, not a broken secret.
CREDENTIAL_LIST = re.compile(r"_(TOKENS|KEYS|SECRETS|PASSWORDS|TOKEN_LIST)$", re.I)
URL_VALUE = re.compile(r"^(.*_URL|.*_URI|.*_DSN|.*_ENDPOINT|.*_ENDPOINTS)$", re.I)
REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# The characters that break a query string / userinfo when embedded raw.
BREAKING = {"#": "a '#' ends the URL at a fragment, so everything after it is dropped"}


def unquote(value: str) -> str:
    """Read a value the way dotenv (and a shell) would.

    Quoted values keep their contents verbatim. An *unquoted* value ends at
    ``" #"``: that is a trailing comment, not part of the secret. Getting this
    wrong is not cosmetic — it turns ``AUTHENTIK_BASE_URL=https://… # issuer``
    into a URL with a space and a hash in it, and the audit then reports a
    credential problem that does not exist while the real one goes unnoticed.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    comment = value.find(" #")
    if comment >= 0:
        value = value[:comment]
    return value.rstrip()


def parse(path: Path) -> dict[str, str]:
    """``{KEY: value}`` for the assignments in one .env file."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match:
            values[match.group(1)] = unquote(match.group(2))
    return values


def problems(env: dict[str, str]) -> list[tuple[str, str, str]]:
    """``[(severity, key, what's wrong)]`` for one parsed .env."""
    found: list[tuple[str, str, str]] = []

    # Keys some URL in this same file interpolates — the ARI-password shape.
    referenced: set[str] = set()
    for key, value in env.items():
        if URL_VALUE.match(key):
            for braced, bare in REFERENCE.findall(value):
                referenced.add(braced or bare)

    for key, value in env.items():
        if not value:
            continue
        if URL_VALUE.match(key):
            # A URL may carry its credential inline (scheme://user:pw@host) rather
            # than through ${KEY}. A '#' is only fatal when something follows it
            # that matters — a query, or embedded userinfo — because a bare
            # `/#/route` is a legitimate hash route.
            if " " in value:
                found.append(
                    ("ERROR", key, "URL value contains a raw space (invalid in userinfo and in a query)")
                )
            elif "#" in value and ("?" in value or "@" in value.split("://", 1)[-1]):
                found.append(
                    (
                        "ERROR",
                        key,
                        "URL value contains '#' after a query or embedded credential, "
                        "so that part of the URL is dropped as a fragment",
                    )
                )
            continue

        if CREDENTIAL_LIST.search(key):
            continue
        if not CREDENTIAL.search(key):
            continue

        broken = [f"{ch} — {why}" for ch, why in BREAKING.items() if ch in value]
        if any(ch.isspace() for ch in value):
            broken.append("whitespace — cannot appear raw in a URL or an unquoted assignment")
        if not broken:
            continue

        # A referenced credential is an ERROR (we know a URL consumes it);
        # anything else is a WARN because the call sites are not visible here.
        severity = "ERROR" if key in referenced else "WARN"
        found.append((severity, key, "; ".join(broken)))
    return found


def env_files(args) -> list[Path]:
    if args.files:
        return [Path(p) for p in args.files]
    root = Path(args.root).resolve()
    skip = {"node_modules", ".venv", ".git", "dist", "build", ".next", "upstream", "out"}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if ".env" in filenames:
            found.append(Path(dirpath) / ".env")
        if len(found) > 200:  # a runaway walk is a bug, not an inventory
            break
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help=".env files to audit")
    parser.add_argument("--root", default=".", help="scan for .env files below this directory")
    parser.add_argument("--quiet", action="store_true", help="report ERRORs only")
    args = parser.parse_args()

    host = os.uname().nodename
    files = env_files(args)
    if not files:
        print(f"env-credential-audit: no .env files found under {Path(args.root).resolve()}")
        return 0

    errors = warns = 0
    lines: list[str] = []
    for path in files:
        env = parse(path)
        for severity, key, why in problems(env):
            if severity == "ERROR":
                errors += 1
            elif args.quiet:
                continue
            else:
                warns += 1
            lines.append(f"  {severity:<5} {path}::{key} — {why}")

    print(f"env-credential-audit: {host} — {len(files)} file(s), {errors} error(s), {warns} warning(s)")
    for line in lines:
        print(line)
    if errors or (warns and not args.quiet):
        print()
        print("  A value that cannot survive its URL fails later, somewhere else:")
        print("  the service simply never connects. Rotate the value to one without")
        print("  '#' or whitespace (`openssl rand -hex 24`), then re-render it.")
    return 1 if errors or warns else 0


if __name__ == "__main__":
    sys.exit(main())
