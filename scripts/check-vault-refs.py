#!/usr/bin/env python3
"""Fail when a `vault://` reference in the estate cannot be resolved.

WHY THIS EXISTS
---------------
Cerulean Vault is the platform's SecretOps store, and a `.env` value may be a
reference instead of a value::

    GITEA_DB_PASSWORD=vault://cerulean/atlas#GITEA_DB_PASSWORD

That is only better than a plaintext secret if the reference **resolves**. An
unresolvable one is the worst of both worlds: it looks configured in every review
and in every `grep`, and it fails later as an authentication error that names the
wrong service — the failure mode `atlas/scripts/vault-resolve.py` refuses to
allow, and the reason it exits non-zero rather than writing an empty credential.

The estate's convergence plan (Phase 2, "one identity + one secrets store")
recorded two host-local `.env` files that still held the retired `INFISICAL_*`
block. Measured on 2026-09-18, none did — but the same pass turned up the shape
this check exists for: Atlas's `cerulean/atlas` held **one** of the two keys its
repo declares Vault-owned (`GITEA_DB_PASSWORD` was only ever in `.env`), and the
`change-me` placeholder for the Authentik-issued secret was still in place, which
is a *wrong* value that looks like a working one. Neither is visible from the
reference side; both are visible from Vault's.

So this is a check, not a note, and it reads both sides: every reference in the
estate, and what the store actually holds.

THE RULES
---------
1. **The reference has the documented grammar.** `vault://<mount>/<path>#<key>`
   — the `#key` fragment is required, because a consumer that needs one value
   cannot guess which of a secret's keys it wanted.
2. **The product's own token is on the host that holds the file.** Cerulean mints
   a path-scoped token per product (`VAULT_PRODUCT_TOKENS`); the consumer holds a
   copy at `<group>/<repo>/data/vault/token/<product>.token`. No token means the
   reference cannot resolve on that host, whatever Vault holds.
3. **That token can read the path it is named in.** A 403 here is not a Vault
   outage — it is a reference naming a sibling's path, or a token minted for the
   wrong product. Products must not share the mount-wide token, so this is also
   the check that the scoping is real.
4. **The key exists and is not empty.** The store is the source of truth; a path
   that holds *some* keys is not a path that holds *this* one.
5. **`infisical://` is not a fallback.** Infisical is retired; a leftover
   reference in that grammar is a credential that was never moved, and it fails
   rather than passing through.

WHAT IT REPORTS BUT CANNOT DECIDE
---------------------------------
* **An unreachable Vault** is reported and skipped, not failed: this check runs
  on any host, and a host that cannot reach the store is a deployment fact, not a
  reference's problem. `--strict` turns the skip into a failure for a host that is
  supposed to reach it.
* **Resolution is not freshness.** A reference that resolves proves Vault holds
  the value; it does not prove the consumer was restarted since the reference was
  written. A container that boots holding a literal `vault://` string resolves
  here and is still broken — what catches that is the consumer's own resolver
  refusal at boot (Zeus logs `resolved N secret reference(s)`, and exits non-zero
  when one does not).

USAGE
-----
    ./scripts/check-vault-refs.py                    # the estate this lives in
    ./scripts/check-vault-refs.py --root /path       # repeatable; default: estate
    ./scripts/check-vault-refs.py --strict           # fail when Vault is unreachable
    ./scripts/check-vault-refs.py --json

Values are never printed — only key names, products and lengths.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REF_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=vault://([^#\s]+)#(\S+)$")
NO_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=vault://([^#\s]+)$")
LEGACY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=infisical://(\S+)$")

DEFAULT_MOUNT = "cerulean"
DEFAULT_ADDR = "http://127.0.0.1:8200"
TIMEOUT_SECONDS = 15


# ── Findings ──────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    """One reference, and what happened to it."""

    file: str
    line: int
    key: str
    ref: str
    product: str = ""
    status: str = "ok"          # ok | missing | no-token | forbidden | denied
    detail: str = ""
    resolved_chars: int = 0

    @property
    def is_violation(self) -> bool:
        return self.status != "ok"

    def render(self) -> str:
        where = f"{self.file}:{self.line}"
        if self.status == "ok":
            return f"ok        {self.ref}  ({self.resolved_chars} chars)  <- {where}"
        if self.status == "forbidden":
            return f"HTTP 403  {self.ref}  (product {self.product})  <- {where}"
        return f"{self.status.upper():<9} {self.ref}  {self.detail}  <- {where}"


# ── Scanning ──────────────────────────────────────────────────────────────────

def estate_root(start: Path | None = None) -> Path:
    """The estate directory this repo sits in — `ips/`'s parent, by layout."""
    here = (start or Path(__file__).resolve().parent).resolve()   # .../ips/scripts
    return here.parent.parent                                     # .../complete


def env_files(root: Path):
    """Every `.env` in the estate: one per repo, plus a group's own if it has one."""
    for pattern in ("*/*/.env", "*/.env", "mesh/.env"):
        for path in sorted(root.glob(pattern)):
            yield path


def token_files(root: Path) -> dict[str, Path]:
    """Product → the token the estate holds for it (`<product>.token`)."""
    tokens: dict[str, Path] = {}
    for pattern in ("*/data/vault/token/*.token", "*/*/data/vault/token/*.token"):
        for path in sorted(root.glob(pattern)):
            product = path.name[: -len(".token")]
            tokens.setdefault(product, path)
    return tokens


def parse_ref(value: str) -> tuple[str, str, str] | None:
    """`vault://<mount>/<path>#<key>` → (mount, path, key), or None."""
    if not value.startswith("vault://"):
        return None
    body = value[len("vault://") :]
    if "#" not in body:
        return None
    path, _, key = body.partition("#")
    parts = path.strip("/").split("/")
    if not parts or not parts[0] or not key:
        return None
    return parts[0], "/".join(parts[1:]) or parts[0], key


def relative(path: Path, roots: list[Path]) -> str:
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


def scan_env(path: Path, line_no: int, line: str) -> list[Finding]:
    """The findings a single line produces, before any call to Vault."""
    text = line.strip()
    if not text or text.startswith("#"):
        return []

    legacy = LEGACY_RE.match(text)
    if legacy:
        return [
            Finding(
                file=str(path),
                line=line_no,
                key=legacy.group(1),
                ref=f"infisical://{legacy.group(2)}",
                status="denied",
                detail="retired store — move it with vault-migrate.py",
            )
        ]

    ref = REF_RE.match(text)
    if ref:
        key, body = ref.group(1), ref.group(2)
        mount, path_, refkey = parse_ref(f"vault://{body}#{ref.group(3)}")
        return [
            Finding(
                file=str(path),
                line=line_no,
                key=key,
                ref=f"vault://{body}#{refkey}",
                product=path_.split("/")[0],
                status="pending",
            )
        ]

    nokey = NO_KEY_RE.match(text)
    if nokey:
        return [
            Finding(
                file=str(path),
                line=line_no,
                key=nokey.group(1),
                ref=f"vault://{nokey.group(2)}",
                status="missing",
                detail="no `#key` fragment — the grammar requires one",
            )
        ]
    return []


# ── Vault ─────────────────────────────────────────────────────────────────────

@dataclass
class Vault:
    """A read-only KV v2 client, and the only place a socket is opened."""

    addr: str = DEFAULT_ADDR
    namespace: str = ""
    skip_verify: bool = False
    cacert: str = ""
    timeout: int = TIMEOUT_SECONDS
    reachable: bool | None = None
    _ctx: ssl.SSLContext | None = field(default=None, repr=False)

    def _context(self) -> ssl.SSLContext:
        if self._ctx is None:
            if self.skip_verify:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._ctx = ctx
            else:
                self._ctx = ssl.create_default_context(cafile=self.cacert or None)
        return self._ctx

    def read(self, mount: str, path: str, token: str) -> tuple[str, dict]:
        """Read `<mount>/data/<path>`; returns (status, data). Never raises."""
        url = f"{self.addr.rstrip('/')}/v1/{mount}/data/{urllib.parse.quote(path)}"
        req = urllib.request.Request(url)
        req.add_header("X-Vault-Token", token)
        if self.namespace:
            req.add_header("X-Vault-Namespace", self.namespace)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._context()) as resp:
                self.reachable = True
                payload = json.load(resp)
            return "ok", payload.get("data", {}).get("data", {}) or {}
        except urllib.error.HTTPError as err:
            self.reachable = True
            if err.code in (403, 404):
                return "forbidden", {}
            return f"http-{err.code}", {}
        except Exception:  # noqa: BLE001 — unreachable, unsealed, DNS, TLS: all "cannot ask"
            if self.reachable is None:
                self.reachable = False
            return "unreachable", {}


def vault_from_env(environ: dict[str, str] | None = None) -> Vault:
    """Same variable names as every other resolver in the estate."""
    env = environ if environ is not None else os.environ
    return Vault(
        addr=(env.get("VAULT_ADDR") or DEFAULT_ADDR).strip(),
        namespace=(env.get("VAULT_NAMESPACE") or "").strip(),
        skip_verify=(env.get("VAULT_SKIP_VERIFY") or "").strip() in ("1", "true", "yes"),
        cacert=(env.get("VAULT_CACERT") or "").strip(),
    )


def resolve(findings: list[Finding], root: Path, tokens: dict[str, Path], vault: Vault,
            mount: str = DEFAULT_MOUNT) -> list[Finding]:
    """Ask Vault about every pending finding, in place."""
    for finding in findings:
        if finding.status != "pending":
            continue
        token_path = tokens.get(finding.product)
        if token_path is None:
            finding.status = "no-token"
            finding.detail = f"no data/vault/token/{finding.product}.token on this host"
            continue
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            finding.status = "no-token"
            finding.detail = f"{token_path.name} unreadable ({exc.strerror})"
            continue
        _, path_, key = parse_ref(finding.ref) or ("", "", "")
        status, data = vault.read(mount, path_, token)
        if status == "unreachable":
            finding.status = "unreachable"
            finding.detail = f"cannot reach {vault.addr}"
        elif status == "forbidden":
            finding.status = "forbidden"
        elif status != "ok":
            finding.status = status
            finding.detail = f"unexpected answer from {vault.addr}"
        elif not str(data.get(key, "")).strip():
            finding.status = "missing"
            finding.detail = f"path holds {sorted(data) or 'nothing'}"
        else:
            finding.status = "ok"
            finding.resolved_chars = len(str(data[key]))
    return findings


def check_root(root: Path, vault: Vault, mount: str = DEFAULT_MOUNT) -> list[Finding]:
    tokens = token_files(root)
    findings: list[Finding] = []
    for path in env_files(root):
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            findings.extend(scan_env(path, line_no, line))
    findings = resolve(findings, root, tokens, vault, mount)
    for finding in findings:
        finding.file = relative(Path(finding.file), [root])
    return findings


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a vault:// reference in the estate cannot be resolved."
    )
    parser.add_argument("--root", action="append", default=None,
                        help="estate root to inspect (repeatable); default: the one this lives in")
    parser.add_argument("--strict", action="store_true",
                        help="fail when Vault is unreachable instead of skipping those files")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    roots = [Path(r).resolve() for r in (args.root or [estate_root()])]
    present = [r for r in roots if r.is_dir()]
    if not present:
        print("vault-refs: this checkout stands alone — no estate to read, nothing to check")
        return 0

    vault = vault_from_env()
    results = {str(root): check_root(root, vault) for root in present}
    findings = [f for group in results.values() for f in group]

    if args.json:
        print(json.dumps(
            {
                "vault": vault.addr,
                "reachable": vault.reachable,
                "roots": [str(r) for r in present],
                "findings": [finding.__dict__ for finding in findings],
                "violations": sum(1 for f in findings if f.is_violation),
            },
            indent=2,
        ))
        return 1 if any(f.is_violation for f in findings) else 0

    for root in present:
        group = results[str(root)]
        if not group:
            print(f"{root}: no vault:// references")
            continue
        print(f"--- {root}")
        for finding in sorted(group, key=lambda f: (f.file, f.line)):
            if finding.is_violation or not args.quiet:
                print(f"  {finding.render()}")

    violations = [f for f in findings if f.is_violation]
    unreachable = [f for f in violations if f.status == "unreachable"]

    if unreachable and not args.strict:
        print(f"\n{len(unreachable)} reference(s) could not be checked against "
              f"{vault.addr} — cannot reach it from here; re-run on a host that can")
        violations = [f for f in violations if f.status != "unreachable"]

    if violations:
        print(f"\n{len(violations)} reference(s) do not resolve")
        return 1
    if not findings:
        print("ok: no vault:// references in the estate")
        return 0
    resolved = sum(1 for f in findings if f.status == "ok")
    print(f"\nok: {resolved} reference(s) resolve against {vault.addr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
