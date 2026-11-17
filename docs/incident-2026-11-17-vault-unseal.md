# Incident: the Cerulean Vault has no usable unseal key (2026-11-17)

**Status:** resolved 2026-11-17 10:2xZ by rebuild — the replacement shares a key that was
proven on a copy before any data was written (see Resolution).
**Severity:** was high — the loss was latent until the next restart or reboot.

## What happened

Vault's unseal key was rotated on 2026-11-17 01:01 to retire a share that had leaked
into a terminal transcript. The rekey call returned `complete=true` and the returned
share was written to `init/init.json`. That response was taken as proof.

It is not proof. The share in `init.json` does not unwrap the barrier, and neither does
the pre-rekey share that was kept as a fallback:

```
PUT /v1/sys/rekey/init          -> 200, nonce issued
PUT /v1/sys/rekey/update {key}  -> 500
  failed to read root key: invalid key: failed to decrypt keys from storage:
  error decrypting seal wrapped value / cipher: message authentication failed
```

That is against the **live** instance, not a copy, so it is not an artefact of an
inconsistent snapshot. A throwaway Vault (`hashicorp/vault:1.17`) started against a
byte copy of the storage reports `initialized: true, sealed: true, t: 1, n: 1` and
rejects every candidate the same way — `unseal_keys_b64`, `unseal_keys_hex`, new and
pre-rekey — which is what a `_shamir-kek` wrapped by an unknown root key looks like.

The barrier files `core/_master`, `core/_keyring`, `core/_shamir-kek` and
`core/_seal-config` all carry the rekey's timestamp, so the storage is the rekeyed one
and the copy is complete. `sys/seal-status` reports `type: shamir`,
`recovery_seal: false`, `migration: false`, so this is not auto-unseal where unseal
keys would be recovery keys.

## What that meant

* The root token in `init/init.json` still works (`policies: ["root"]`, `ttl: 0`), so
  data is readable and the instance keeps serving.
* `sys/rekey` and `sys/generate-root` both require an existing unseal key to authorise,
  and there is none. **Unseal capability could not be restored on that instance.**
* `cerulean-vault` runs with `restart=unless-stopped`, started 2026-11-15 23:57Z. A
  restart, container recreation, `docker compose down`, or a reboot of the docker host
  would have left the vault sealed, with no path back.
* The leaked share is inert — it cannot unwrap the barrier either — so the original
  disclosure is neutralised, at the cost of having no key at all.

## Why a rebuild was viable

The only secret engine mounted is KV:

```
MOUNTS  cerulean/  (kv)   cubbyhole/  identity/  sys/
AUDIT   none configured
```

There is no `transit`, `pki`, `database` or `ssh` engine, so no key material exists
that can only live inside this barrier. A replacement instance can therefore be
initialised with a share that is verified before anything is written to it, and
populated from `cerulean/` through the root token. Policies (21) and the OIDC auth
method are recreated from `sys/policies` and `sys/auth`.

## Verification recipe (repeat before any future rekey is called done)

Copy the storage, `chown -R 100:100`, run a throwaway `hashicorp/vault:1.17` on a spare
port against it, and unseal **that copy** with the share that was just stored. A rekey
that has not been proven this way is not a rekey.

## Resolution

The rebuild this page called viable was carried out the same day.

1. **Exported** the whole instance through the working root token into
   `/root/vault-export-20261117T100732Z` on the docker host: 108 KV leaves with their
   paths preserved, all 21 policies, the 16 product tokens with their policy, period and
   TTL metadata, the OIDC config and its roles, and the single AppRole (`mailjet`) with
   its role-id. A 12 MB tar of `file/`, `init/` and `token/` went alongside.
2. **Set the old barrier aside rather than deleting it**:
   `data/vault/inert-20261117T100732Z/` holds `file/`, the rekeyed `init.json` and the
   pre-rekey copy.
3. **Re-initialised** through `scripts/vault-entrypoint.sh` — wiped `file/` and
   `init.json`, restarted the container — which minted a fresh 1-share/1-threshold key,
   unsealed with it, enabled KV v2 at `cerulean/` and wrote `cerulean.token`.
4. **Proved the new key before trusting it**, using the recipe above on a copy: the
   throwaway instance came back `sealed: false`. This is the step whose absence caused
   the incident, and it is now the gate any rebuild or rekey has to pass.
5. **Restored** 108/108 secrets, the 20 policy files (`root` is the 21st), 15 product
   tokens re-minted from their recorded policies and 768 h periods, the OIDC config with
   its `operator` role, and `approle/mailjet` with its **original role-id**.
6. **Proof the incident is over**: `docker restart cerulean-vault` now comes back
   `sealed=false` on its own, with all 30 top-level paths, 21 policies,
   `approle/, oidc/, token/` and a valid `cerulean.token` (period 2 764 800 s).

### What the rebuild did not restore

An AppRole **secret-id** outstanding when the barrier was replaced cannot be carried
over, because secret-ids live inside the barrier. `mailjet` kept its role-id, so a
consumer needs only a new secret-id
(`vault write -f auth/approle/role/mailjet/secret-id`); no consumer of that role was
found in the Cerulean tree, and the product tokens are files rather than AppRole logins.
Both keys in the set-aside directory are inert — the working pair is `init/init.json`.
