# Vault tokens and the day they all expire

Cerulean Vault keeps one service token per product under `/vault/token/*.token` (mode
600, inside `cerulean-vault`, which runs in the `proxy` container on i2). Sixteen files:
the fifteen product tokens plus `regcache.token`, the one minted for the registry
rotation.

## They are one cohort, and they all die together

Measured on 2026-11-14: every token valid, TTL ≈ 766 h, expiring **2026-12-16** between
06:42 and 11:58 EST. Nothing is renewing them.

They were created at their maximum TTL, and Vault reports `renewable: false` for them:
a token already at its ceiling has nothing left to renew to. That is not a fault, but it
decides the procedure — you cannot renew your way out, you have to **replace** them.

## Checking

`scripts/vault-token-audit.sh` runs inside the Vault container, because that is the only
place the instance root token (`/vault/init/init.json`, mode 600) and every product
token exist together. From i2, with the script on that host:

```bash
incus exec proxy -- docker exec -i cerulean-vault sh -s -- < scripts/vault-token-audit.sh
```

The table gives each token's remaining days, raw TTL, expiry, whether Vault would renew
it, and what happened. Exit 0 means nothing is inside the 14-day window, 1 means
something is lapsing or invalid, 2 means the audit could not run (no root token, sealed
Vault, bad arguments). `--only NAME` narrows it to one token; `--warn-days N` moves the
window.

## Saving them

```bash
# renew where Vault allows it (it will not, for this cohort)
... sh -s -- --warn-days 14 --renew   < scripts/vault-token-audit.sh

# replace: new token, same non-default policies, same TTL, same file, atomic rename
... sh -s -- --warn-days 14 --replace < scripts/vault-token-audit.sh
```

`--replace` mints a successor with the token's own policies and creation TTL, writes it
to `<token>.new`, renames it over the old file, and leaves the old token alive until its
own expiry so nothing that already holds it breaks mid-flight. It reports the successor's
new TTL and expiry on its own row.

**2026-12-01 is the date to run it with `--replace`.** On 2026-12-16 every product token
dies at once, and so does every consumer that reads its token file and expects it to
still work.

## Why this is manual

A timer could run the audit, but there is no notification channel: ntfy is not running
anywhere on the estate, so a failure would be silent anyway. Replacement also invalidates
whatever a consumer has cached, which is a change worth making deliberately rather than
at 03:00. Run it monthly, and read the table.
