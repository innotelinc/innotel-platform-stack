#!/usr/bin/env python3
"""Fail when Authentik privilege widens beyond the two things this estate allows.

WHY THIS EXISTS
---------------
On 2026-10-23 an audit of the 15 users and 43 groups in Authentik found something no
page said: `plutus-admins` and `signara-admins` were flagged **superuser groups**, not
merely admin groups. Their only members were already superusers, so the estate looked
right — but anyone added to either tenant group next month would have silently become a
global Authentik administrator, over every provider, application and outpost. Nothing
watched that flag, so the next such tick would have been found by the same hand-run
audit, if it was run at all.

The estate's model is deliberately small: **`authentik Admins` is the only superuser
group**, and a superuser is the only account type allowed to hold admin rights in more
than one tenant. This check turns that model into a rule that runs every half hour.

THE RULES
---------
1. **A service account with admin rights is a failure.** Authentik's normal logins are
   type `internal`; outposts and client credentials are not, and an account that a
   machine holds must never be able to administer the directory.
2. **A superuser group other than `authentik Admins` is a failure.** This is the
   2026-10-23 finding: a tenant group whose membership is a global grant in disguise.
3. **A dormant superuser is a warning.** Never signed in, or not for `DORMANT_DAYS`:
   privileged access that nobody is using is access nobody is watching.
4. **A non-superuser in two or more admin groups is a warning.** That is a person who
   can cross tenants without being the estate's administrator.

A check that cannot read the API is a **failure to run** (exit 2), never a pass — the
same rule every check here follows, because silence is what 2026-10-01 looked like.

Usage:
    ./scripts/check-authentik-privilege.py
    ./scripts/check-authentik-privilege.py --prom /path/to/textfile
    AUTHENTIK_API_URL=… AUTHENTIK_TOKEN=… ./scripts/check-authentik-privilege.py

Exit codes: 0 = the model holds, 1 = it does not, 2 = the directory could not be read.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, write_prom  # noqa: E402

CHECK = "authentik_privilege"

#: Where the Cerulean deployment keeps the API URL and the token that can read it.
ENV_FILE = "/usr/src/projects/complete/1-primary/cerulean/.env"

#: The one group whose membership is a global grant. Anything else flagged
#: `is_superuser` is rule 2's finding.
SUPERUSER_GROUP = "authentik Admins"

#: A superuser nobody has used for this long is access nobody is watching.
DORMANT_DAYS = 90

#: Authentik's own interactive logins. Everything else is a machine.
HUMAN_TYPE = "internal"


def read_env(path: str) -> dict[str, str]:
    """The `KEY=value` file the deployment already keeps, without the shell."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            key, sep, value = line.strip().partition("=")
            if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                values[key] = value.strip().strip('"').strip("'")
    return values


def api_base(url: str) -> str:
    """The deployment's env usually names the origin; the API lives under `/api/v3`."""
    base = url.rstrip("/")
    return base if base.endswith("/api/v3") else base + "/api/v3"


def api_get(base: str, token: str, path: str) -> dict:
    """One authenticated GET, with the token in the header and never in a URL."""
    request = urllib.request.Request(api_base(base) + path)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def pages(base: str, token: str, path: str) -> list[dict]:
    """Every result of a paginated Authentik list endpoint."""
    out: list[dict] = []
    page = 1
    while True:
        payload = api_get(base, token, f"{path}{'&' if '?' in path else '?'}page={page}&page_size=200")
        out.extend(payload["results"])
        if not payload.get("pagination", {}).get("next"):
            return out
        page += 1


def gather(base: str, token: str) -> tuple[list[dict], list[dict]]:
    """The directory as it is: every user, every group."""
    return pages(base, token, "/core/users/"), pages(base, token, "/core/groups/")


def admin_groups(user: dict) -> list[str]:
    """Groups that read as administrative: the superuser group, or named `*admin*`."""
    return sorted(
        group["name"]
        for group in user.get("groups_obj") or []
        if group.get("is_superuser") or "admin" in group["name"].lower()
    )


def is_superuser(user: dict) -> bool:
    return bool(user.get("is_superuser")) or any(
        group.get("is_superuser") for group in user.get("groups_obj") or []
    )


def _days_since(stamp: str | None, now: float) -> float | None:
    if not stamp:
        return None
    try:
        parsed = time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (now - time.mktime(parsed)) / 86400.0


def audit(users: list[dict], groups: list[dict], now: float | None = None, dormant_days: int = DORMANT_DAYS) -> Audit:
    """The four rules, over data the caller already has. Pure, so the rules are the test."""
    now = time.time() if now is None else now
    result = Audit()

    for group in sorted(groups, key=lambda g: g["name"]):
        if group.get("is_superuser") and group["name"] != SUPERUSER_GROUP:
            result.findings.append(
                Finding(
                    "tenant_superuser_group",
                    "fail",
                    f"group {group['name']} is flagged a superuser group — membership is a "
                    "global grant; clear `is_superuser` and keep `authentik Admins` as the "
                    "only one",
                )
            )

    for user in sorted(users, key=lambda u: u["username"]):
        privileged = is_superuser(user) or admin_groups(user)
        if not privileged:
            continue
        if user.get("type") != HUMAN_TYPE:
            result.findings.append(
                Finding(
                    "service_account_admin",
                    "fail",
                    f"{user['username']} is a {user.get('type')} account with admin rights "
                    f"({', '.join(admin_groups(user)) or 'superuser flag'}) — a machine must "
                    "never be able to administer the directory",
                )
            )
        if is_superuser(user):
            last = _days_since(user.get("last_login"), now)
            if not user.get("is_active"):
                result.findings.append(
                    Finding("disabled_superuser", "warn", f"superuser {user['username']} is disabled — keep it or remove it")
                )
            elif last is None:
                result.findings.append(
                    Finding("dormant_superuser", "warn", f"superuser {user['username']} has never signed in")
                )
            elif last > dormant_days:
                result.findings.append(
                    Finding(
                        "dormant_superuser",
                        "warn",
                        f"superuser {user['username']} last signed in {int(last)} days ago — "
                        "privileged access nobody is watching",
                    )
                )
        else:
            admins = admin_groups(user)
            if len(admins) >= 2:
                result.findings.append(
                    Finding(
                        "cross_tenant_admin",
                        "warn",
                        f"{user['username']} is not a superuser but administers "
                        f"{len(admins)} groups: {', '.join(admins)}",
                    )
                )

    return result


def privilege_series(users: list[dict], groups: list[dict]) -> str:
    """The counts, so the shape of the directory is chartable and not only its verdict."""
    superusers = [user for user in users if is_superuser(user)]
    flagged = [group["name"] for group in groups if group.get("is_superuser")]
    lines = [
        f"# HELP {METRIC}_authentik_superusers Accounts that are Authentik superusers",
        f"# TYPE {METRIC}_authentik_superusers gauge",
        f'{METRIC}_authentik_superusers{{check="{CHECK}"}} {len(superusers)}',
        f"# HELP {METRIC}_authentik_superuser_groups Groups whose membership grants superuser",
        f"# TYPE {METRIC}_authentik_superuser_groups gauge",
        f'{METRIC}_authentik_superuser_groups{{check="{CHECK}"}} {len(flagged)}',
    ]
    for name in sorted(flagged):
        lines.append(f'{METRIC}_authentik_superuser_group{{check="{CHECK}",group="{name}"}} 1')
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", help="Authentik API base (default: AUTHENTIK_API_URL, then the Cerulean .env)")
    parser.add_argument("--token", help="API token (default: AUTHENTIK_TOKEN, then the Cerulean .env)")
    parser.add_argument("--env", default=ENV_FILE, help=f"deployment env file to read the two from (default: {ENV_FILE})")
    parser.add_argument("--dormant-days", type=int, default=DORMANT_DAYS, help=f"warning line for an unused superuser (default: {DORMANT_DAYS})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    env = read_env(args.env) if os.path.exists(args.env) else {}
    base = args.api or os.environ.get("AUTHENTIK_API_URL") or env.get("AUTHENTIK_API_URL")
    token = args.token or os.environ.get("AUTHENTIK_TOKEN") or env.get("AUTHENTIK_BOOTSTRAP_TOKEN")
    if base:
        base = api_base(base)

    def emit(result: Audit | None, ran: bool, extra: str | None = None) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran, extra=extra)
        except OSError as error:
            print(f"check-authentik-privilege: could not write {args.prom}: {error}", file=sys.stderr)

    if not base or not token:
        print(
            f"check-authentik-privilege: no API URL or token (pass --api/--token, set "
            f"AUTHENTIK_API_URL/AUTHENTIK_TOKEN, or provide {args.env})",
            file=sys.stderr,
        )
        emit(None, ran=False)
        return 2

    try:
        users, groups = gather(base, token)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as error:
        print(f"check-authentik-privilege: could not read {base}: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    result = audit(users, groups, dormant_days=args.dormant_days)
    emit(result, ran=True, extra=privilege_series(users, groups))

    superusers = sorted(user["username"] for user in users if is_superuser(user))
    print(
        f"check-authentik-privilege: {len(users)} users, {len(groups)} groups, "
        f"{len(superusers)} superuser(s): {', '.join(superusers) or 'none'}"
    )
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    print(
        f"check-authentik-privilege: {len(result.failures)} failure(s), "
        f"{len(result.warnings)} warning(s)"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
