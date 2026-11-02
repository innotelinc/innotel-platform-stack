#!/usr/bin/env python3
"""Give every Stalwart mailbox the same identity in Authentik, keyed by its address.

WHY THIS EXISTS
---------------
Stalwart names a mailbox by its **full address** (`admin@innotel.us`), and Authentik is
the estate's one identity provider. When the two disagree — a mailbox whose owner has no
Authentik user, or one whose Authentik username is a bare name — the single sign-on a
person uses every day resolves to a different identity from the one their mail is stored
under, and the app that bridges them has to guess.

This script makes the mail address the identity in both places, in one direction only:

    Stalwart account  ->  Authentik user   (username = email = <name>@<domain>)

It is additive and idempotent. It never deletes, renames or deactivates an Authentik
user, and it never touches Stalwart. An existing user is matched by username *or* email
(case-insensitively) and left alone; only a genuinely missing one is created, without a
password (SSO is the door) and in the group named by `--group`.

Not every mailbox belongs to a person. Reserved role addresses (`postmaster@`, `abuse@`),
mailing lists and per-service mailboxes (`jellyfin@`, `monit@`) are skipped by default,
because turning them into identities is how an estate grows thirty accounts nobody signs
in to. `--include-all` syncs every addressable account instead.

Usage (needs both management APIs; credentials are read from the environment):

    STALWART_URL=http://127.0.0.1:8080 STALWART_ADMIN=admin:password \\
    AUTHENTIK_API_URL=https://auth.cerulean.innotel.us AUTHENTIK_TOKEN=... \\
        ./scripts/sync-mail-identities.py                # dry run, the default
        ./scripts/sync-mail-identities.py --apply        # create the missing users

Exit codes: 0 = in sync (or dry run with a plan), 1 = `--apply` was refused for some user,
2 = an API could not be read.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def read_json(url: str, auth: dict[str, str], data: dict | None = None, method: str | None = None):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Accept": "application/json", **auth}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode()
        return json.loads(payload) if payload.strip() else {}


def stalwart_accounts(base: str, admin: str) -> list[tuple[str, str]]:
    """Every internal account as (address, description), read over Stalwart's JMAP API."""
    auth = {"Authorization": "Basic " + base64.b64encode(admin.encode()).decode()}
    result = read_json(base.rstrip("/") + "/jmap/", auth, {
        "using": ["urn:ietf:params:jmap:core"],
        "methodCalls": [["x:Domain/get", {}, "0"], ["x:Account/get", {}, "1"]],
    })
    responses = {call_id: body for _, body, call_id in result["methodResponses"]}
    domains = {d["id"]: d["name"] for d in responses["0"].get("list", [])}
    accounts = []
    for account in responses["1"].get("list", []):
        name = account.get("name")
        domain = domains.get(account.get("domainId"))
        if not name or not domain:
            # An account with no domain is not addressable by email; skip it rather
            # than invent an address.
            continue
        accounts.append((f"{name}@{domain}".lower(), account.get("description") or name))
    return accounts


def authentik_users(base: str, token: str) -> dict[str, dict]:
    """Existing users keyed by both username and email, lowercased."""
    auth = {"Authorization": "Bearer " + token}
    url = base.rstrip("/") + "/api/v3/core/users/?page_size=200"
    users: dict[str, dict] = {}
    while url:
        page = read_json(url, auth)
        for user in page.get("results", []):
            for key in (user.get("username"), user.get("email")):
                if key:
                    users.setdefault(key.lower(), user)
        url = (page.get("pagination") or {}).get("next")
    return users


def resolve_group(base: str, token: str, group: str) -> str:
    """Turn a group name into the pk Authentik's user API actually requires.

    `groups` accepts pks only; passing `Mail` is rejected with "not a valid UUID".
    A pk is returned unchanged, a name is matched exactly (case-insensitively).
    """
    if len(group) == 36 and group.count("-") == 4:
        return group
    auth = {"Authorization": "Bearer " + token}
    url = base.rstrip("/") + "/api/v3/core/groups/?search=" + urllib.parse.quote(group)
    page = read_json(url, auth)
    for candidate in page.get("results", []):
        if candidate["name"].lower() == group.lower():
            return str(candidate["pk"])
    raise ValueError(f"no Authentik group named {group!r}")


def create_user(base: str, token: str, address: str, name: str, group: str | None) -> dict:
    payload: dict[str, object] = {
        "username": address,
        "email": address,
        "name": name,
        "is_active": True,
        "type": "internal",
        "path": "users",
    }
    if group:
        payload["groups"] = [group]
    return read_json(base.rstrip("/") + "/api/v3/core/users/",
                     {"Authorization": "Bearer " + token}, payload, method="POST")


# Local parts that are roles, lists or per-service mailboxes rather than a person's
# address. Kept as an explicit list so the decision is auditable, not inferred.
RESERVED = {
    "abuse", "hostmaster", "noreply", "no-reply", "postmaster", "webmaster",
}
SERVICE = {
    "alertmanager", "authentik", "authentikadmins", "cerulean", "default", "grafana",
    "incus", "jellyfin", "mail", "monit", "pbx", "prometheus", "proxmox", "root",
    "signara-alerts", "stalwart", "traefik", "zeus",
}


def is_person(address: str) -> bool:
    """Whether an address is plausibly a person, not a role or a service."""
    local, _, _ = address.partition("@")
    if any(character.isspace() for character in local) or local in RESERVED:
        return False
    if local in SERVICE or local.startswith("pm") and local[2:].isdigit():
        return False
    return not (local.endswith("_admins") or local.endswith("-admins")
                or local in {"paid_users", "users", "admins"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="create the missing users")
    parser.add_argument("--group", help="Authentik group name or pk to put new users in")
    parser.add_argument("--include-all", action="store_true",
                        help="also sync role, list and service mailboxes")
    args = parser.parse_args(argv)

    stalwart_url = os.environ.get("STALWART_URL", "http://127.0.0.1:8080")
    stalwart_admin = os.environ.get("STALWART_ADMIN", "")
    api = os.environ.get("AUTHENTIK_API_URL", "https://auth.cerulean.innotel.us")
    token = os.environ.get("AUTHENTIK_TOKEN", "")
    if not stalwart_admin or not token:
        print("sync-mail-identities: STALWART_ADMIN and AUTHENTIK_TOKEN are required",
              file=sys.stderr)
        return 2

    try:
        accounts = stalwart_accounts(stalwart_url, stalwart_admin)
        existing = authentik_users(api, token)
    except (urllib.error.URLError, KeyError, ValueError) as error:
        print(f"sync-mail-identities: could not read an estate API: {error}", file=sys.stderr)
        return 2

    missing = [(address, name) for address, name in accounts
               if address not in existing and (args.include_all or is_person(address))]
    print(f"mailboxes={len(accounts)} authentik-users={len(existing)} missing={len(missing)}")
    for address, _ in missing:
        print(f"  MISSING {address}")
    if not missing:
        print("every mailbox already has an identity keyed by its address")
        return 0
    if not args.apply:
        print("\ndry run: nothing was created (pass --apply)")
        return 0

    if args.group:
        try:
            args.group = resolve_group(api, token, args.group)
            print(f"group: {args.group}")
        except (urllib.error.URLError, ValueError) as error:
            print(f"sync-mail-identities: {error}", file=sys.stderr)
            return 2

    failures = 0
    for address, name in missing:
        try:
            user = create_user(api, token, address, name, args.group)
            print(f"  CREATED {address} (pk={user.get('pk')})")
        except urllib.error.HTTPError as error:
            failures += 1
            print(f"  FAILED {address}: {error.code} {error.read().decode()[:160]}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
