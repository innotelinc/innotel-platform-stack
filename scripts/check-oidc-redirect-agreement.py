#!/usr/bin/env python3
"""Fail when a mount's OIDC role and the provider it signs in through disagree.

WHY THIS EXISTS
---------------
Vault's UI answered "Missing auth_url. Please check that allowed_redirect_uris for the role
include this mount path" on 2026-11-20, and the role *did* allow the callback it was asked
for — the **https** one. The browser was on http, so it asked for the http form, and Vault
compares the origin verbatim. The provider had the same narrow list. Two lists, two files,
same origin, and nothing in the estate compared them.

That class of breakage is invisible until someone signs in, and it presents as a message
about the role even when the provider is the one that would have refused: Authentik rejects
an unlisted `redirect_uri` at authorize time with a "Redirect URI Error" page, and Vault
rejects an unlisted one at callback time with `unauthorized redirect_uri` in its log. Both
end with a person unable to sign in and no metric anywhere.

So this check reads both lists and compares them as sets:

  * a URI the role allows and the provider does not -> the sign-in fails at Authentik;
  * a URI the provider allows and the role does not -> the sign-in fails at Vault;
  * equal sets -> the flow can start and finish, for that origin.

THE RULES
---------
1. Any disagreement is a **failure**, in both directions, naming which side is missing it.
2. A target that cannot be read is **exit 2**: a check that cannot see both lists must never
   pass quietly, or a renamed host or an expired token would look like agreement.
3. Lists are compared as sets, but repeated items are reported, because a duplicate in the
   role is a role that was written by hand twice.

READ-ONLY
---------
Two GETs. The Vault token only needs read on the role path (`auth/oidc/role/*`); the
Authentik token only needs read on providers. Nothing is written, and no secret is printed —
the tokens are never echoed, not even truncated.

Usage:
    ./scripts/check-oidc-redirect-agreement.py
    ./scripts/check-oidc-redirect-agreement.py --vault-role auth/oidc/role/operator \\
        --client-id vault --prom /path/to/textfile

`systemd/estate-checks.service` runs it beside the vault on the edge host, where
`VAULT_ADDR` is the loopback publish and the cerulean `.env` supplies the Authentik token.

Exit codes: 0 = the two lists agree, 1 = they disagree, 2 = a side could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, write_prom  # noqa: E402

CHECK = "oidc_redirect_agreement"

#: The mount this estate signs in through, and the provider name it does it with.
ROLE = "auth/oidc/role/operator"
IDP = "https://auth.cerulean.innotel.us"
CLIENT_ID = "vault"

#: Where the deployment keeps the Authentik token. Read for one key only, never printed.
CERULEAN_ENV = Path("/usr/src/projects/complete/1-primary/cerulean/.env")

USER_AGENT = "innotel-estate-check/oidc-redirect-agreement"


class Unreadable(RuntimeError):
    """One side of the comparison could not be read, and why."""


def _get(url: str, headers: dict[str, str], context=None, timeout: float = 20.0) -> dict:
    request = urllib.request.Request(url, headers={**headers, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as error:
        raise Unreadable(f"HTTP {error.code} from {url.split('?')[0]}") from error
    except Exception as error:  # noqa: BLE001 — transport, DNS, TLS, JSON: all unreadable
        raise Unreadable(f"{url.split('?')[0]}: {error}") from error


def read_role_uris(addr: str, token: str, role: str, timeout: float = 20.0) -> list[str]:
    """The role's `allowed_redirect_uris` in the order the vault reports them."""
    url = f"{addr.rstrip('/')}/v1/{role.lstrip('/')}"
    document = _get(url, {"X-Vault-Token": token}, timeout=timeout)
    data = (document or {}).get("data") or {}
    uris = data.get("allowed_redirect_uris")
    if uris is None:
        # A role with no explicit list falls back to the mount's default; say so rather than
        # pretending the list is empty.
        raise Unreadable(
            f"role {role} reports no allowed_redirect_uris — the mount's default_redirect_uri "
            "applies instead and cannot be compared")
    return [str(uri) for uri in uris]


def read_provider_uris(idp: str, token: str, client_id: str, timeout: float = 25.0) -> list[dict]:
    """The provider's redirect_uris as `[{matching_mode, url}]`, for the given client_id."""
    context = ssl.create_default_context()
    base = f"{idp.rstrip('/')}/api/v3/providers/oauth2/"
    document = _get(f"{base}?client_id={client_id}", {"Authorization": "Bearer " + token},
                    context=context, timeout=timeout)
    results = (document or {}).get("results") or []
    matches = [row for row in results if str(row.get("client_id")) == client_id]
    if not matches:
        raise Unreadable(f"no OAuth2 provider in Authentik holds client_id {client_id} "
                         f"({len(results)} provider(s) searched)")
    if len(matches) > 1:
        raise Unreadable(f"{len(matches)} providers share client_id {client_id} — the check "
                         "cannot choose between them")
    return [{"matching_mode": str(row.get("matching_mode") or "strict"), "url": str(row.get("url"))}
            for row in matches[0].get("redirect_uris") or []]


def compare(role_uris: list[str], provider_uris: list[dict]) -> Audit:
    """The rules, over two lists the caller already has. Pure, so the rules are the subject."""
    out = Audit()
    role = [uri for uri in role_uris if uri]
    provider = [row["url"] for row in provider_uris if row.get("url")]
    role_set, provider_set = set(role), set(provider)

    def duplicates(values: list[str]) -> list[str]:
        seen, repeated = set(), []
        for value in values:
            if value in seen and value not in repeated:
                repeated.append(value)
            seen.add(value)
        return repeated

    for uri in duplicates(role):
        out.findings.append(Finding(
            "role_duplicate", "warn",
            f"the role lists {uri} more than once — harmless to Vault, but it means the role "
            "was written twice and the next editor cannot tell which copy is authoritative"))
    for uri in duplicates(provider):
        out.findings.append(Finding(
            "provider_duplicate", "fail",
            f"the provider lists {uri} more than once"))

    for uri in sorted(role_set - provider_set):
        out.findings.append(Finding(
            "missing_at_provider", "fail",
            f"the role allows {uri} but the provider does not list it — a sign-in from that "
            "origin ends on Authentik's \"Redirect URI Error\" page before Vault is involved"))
    for uri in sorted(provider_set - role_set):
        out.findings.append(Finding(
            "missing_at_role", "fail",
            f"the provider allows {uri} but the role does not — Authentik would return the "
            "person to a callback Vault refuses, which reads in the UI as \"Missing auth_url\" "
            "and in Vault's log as `unauthorized redirect_uri`"))

    if not out.findings:
        out.findings.append(Finding(
            "agreed", "note",
            f"both sides allow the same {len(role_set)} redirect URI(s)"))
    return out


def _token_from_env_file(path: Path, key: str) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise Unreadable(f"{path}: {error}") from error
    for line in text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise Unreadable(f"{path} holds no {key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault-addr", default=os.environ.get("VAULT_ADDR", ""),
                        help="vault address (default $VAULT_ADDR)")
    parser.add_argument("--vault-token", default=os.environ.get("VAULT_TOKEN", ""),
                        help="vault token with read on the role path (default $VAULT_TOKEN)")
    parser.add_argument("--vault-token-file", default=os.environ.get("VAULT_TOKEN_FILE", ""),
                        help="file holding the vault token (default $VAULT_TOKEN_FILE)")
    parser.add_argument("--vault-role", default=ROLE, help=f"role path (default: {ROLE})")
    parser.add_argument("--idp", default=IDP, help=f"Authentik base URL (default: {IDP})")
    parser.add_argument("--client-id", default=CLIENT_ID, help=f"OAuth2 client_id (default: {CLIENT_ID})")
    parser.add_argument("--authentik-token", default=os.environ.get("AUTHENTIK_TOKEN", ""),
                        help="Authentik API token; read from --env-file when absent")
    parser.add_argument("--env-file", default=str(CERULEAN_ENV),
                        help=f"file holding AUTHENTIK_BOOTSTRAP_TOKEN (default: {CERULEAN_ENV})")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            write_prom(args.prom, CHECK, result, ran_ok=ran)

    def fail(message: str) -> int:
        emit(None, ran=False)
        print(f"check-oidc-redirect-agreement: {message}", file=sys.stderr)
        return 2

    token = args.vault_token
    if not token and args.vault_token_file:
        try:
            token = Path(args.vault_token_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            return fail(f"could not read {args.vault_token_file}: {error}")
    if not args.vault_addr or not token:
        return fail("VAULT_ADDR and a token (VAULT_TOKEN or VAULT_TOKEN_FILE) are required")

    authentik_token = args.authentik_token
    if not authentik_token:
        try:
            authentik_token = _token_from_env_file(Path(args.env_file), "AUTHENTIK_BOOTSTRAP_TOKEN")
        except Unreadable as error:
            return fail(f"no Authentik token: {error}")

    try:
        role_uris = read_role_uris(args.vault_addr, token, args.vault_role)
        provider_uris = read_provider_uris(args.idp, authentik_token, args.client_id)
    except Unreadable as error:
        return fail(f"could not read both lists — {error}")

    result = compare(role_uris, provider_uris)
    emit(result, ran=True)

    print(f"check-oidc-redirect-agreement: {args.vault_role} <-> {args.client_id} at {args.idp}")
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.code}: {finding.message}")
    print(f"check-oidc-redirect-agreement: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
