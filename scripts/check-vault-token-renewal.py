#!/usr/bin/env python3
"""Renew the estate's own vault token, so the check that needs it keeps working.

**A check that writes, and why.** Every other check here only reads. This one renews a
credential, because `check-credential-age.py` reads the vault with a **periodic** token
(`-period=720h`) and nothing in the estate renews it: a periodic token stops being usable
when its period runs out, and the only symptom is `credential_age` turning into
`EstateCheckCouldNotRun` — a red estate check caused entirely by the estate's own
credential expiring. Measured 2026-11-17: the token was minted to unblock that check, and
without a renewal line its first expiry would take the check down again about 30 days
later. A cron of one shell command would do the same thing and be invisible; here the
renewal publishes the same telemetry as every other check, so a renewal that stops
working is a metric before it is an outage.

**What it can do.** `auth/token/renew-self` with the token itself. It cannot mint, revoke
or look up another token, and it reads no secret — the endpoint does not consult the
credential's policy (`cerulean/metadata/*` read+list), so this is confined to extending
its own lease.

**What it will not do.** Re-mint a token that has stopped renewing. Minting needs a root or
a token with `auth/token/create`, which is a much larger credential than a check should
hold; that is a person's job, and this says so as a failure instead of going quiet.

Exit codes are the estate's: 0 = renewed, 1 = the renewal did not hold, 2 = no credential
to renew (nothing was attempted).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from estate_check import Audit, Finding, write_prom  # noqa: E402

CHECK = "vault_token_renewal"
# A lease shorter than this means the next renewal is already urgent — the timer runs every
# 30 minutes, so a week is slack for a weekend of failed runs rather than a threshold.
DEFAULT_MIN_DAYS = 7.0


class VaultRefused(RuntimeError):
    """The vault did not renew the token, and why."""


def renew_url(addr: str) -> str:
    return f"{addr.rstrip('/')}/v1/auth/token/renew-self"


def _post(url: str, token: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"X-Vault-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(64_000))


def renew(addr: str, token: str, timeout: float = 10.0, transport=None) -> dict:
    """Ask the vault to extend this token's own lease; return the `auth` block.

    `transport` is injectable so the tests can answer with a refusal without a vault.
    """
    transport = transport or _post
    try:
        document = transport(renew_url(addr), token, {"increment": ""}, timeout)
    except urllib.error.HTTPError as error:
        raise VaultRefused(f"HTTP {error.code} from renew-self") from error
    except Exception as error:  # noqa: BLE001 — transport, DNS, timeout, bad JSON: all refusals
        raise VaultRefused(str(error) or "no response") from error
    auth = (document or {}).get("auth") or {}
    if not auth:
        raise VaultRefused("renew-self returned no auth block")
    return auth


def audit(
    addr: str,
    token: str,
    *,
    timeout: float = 10.0,
    min_days: float = DEFAULT_MIN_DAYS,
    renewer=renew,
) -> Audit:
    """One finding: the lease it got, or why it got none."""
    result = Audit()
    try:
        auth = renewer(addr, token, timeout=timeout)
    except VaultRefused as error:
        result.findings.append(Finding(
            "renew-refused", "fail",
            f"the vault refused to renew the estate's token ({error}) — credential_age stops "
            "working the moment this lease ends, and re-minting needs a person"))
        return result

    days = float(auth.get("lease_duration") or 0) / 86_400
    if not auth.get("renewable", False):
        result.findings.append(Finding(
            "not-renewable", "fail",
            "the token came back non-renewable, so this check cannot keep it alive; re-mint it "
            "and replace VAULT_TOKEN in /etc/innotel/estate-checks.env"))
        return result
    if days < min_days:
        result.findings.append(Finding(
            "short-lease", "warn",
            f"the renewal returned {days:.1f} days, under the {min_days:.0f}d this check asks "
            "for — the token is probably nearing a cap the vault will not extend past"))
    else:
        result.findings.append(Finding(
            "renewed", "note", f"renewed the estate's vault token for {days:.1f} days"))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--addr", default=os.environ.get("VAULT_ADDR", ""),
                        help="vault address (default $VAULT_ADDR)")
    parser.add_argument("--token", default=os.environ.get("VAULT_TOKEN", ""),
                        help="the token to renew (default $VAULT_TOKEN)")
    parser.add_argument("--min-days", type=float, default=DEFAULT_MIN_DAYS,
                        help="warn when the new lease is shorter than this")
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
            print(f"check-vault-token-renewal: could not write {args.prom}: {error}",
                  file=sys.stderr)

    if not args.addr or not args.token:
        emit(None, ran=False)
        print("check-vault-token-renewal: VAULT_ADDR/VAULT_TOKEN are not set — there is no "
              "token to renew", file=sys.stderr)
        return 2

    result = audit(args.addr, args.token, timeout=args.timeout, min_days=args.min_days)
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
    print(f"check-vault-token-renewal: {len(result.failures)} failure(s), "
          f"{len(result.warnings)} warning(s), {len(result.notes)} note(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
