# Incident: the Cerulean Vault has no usable unseal key (2026-11-17)

**Status:** open, contained. The running instance is healthy; it cannot be started again.
**Severity:** high — the loss is latent until the next restart or reboot.

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

## What that means

* The root token in `init/init.json` still works (`policies: ["root"]`, `ttl: 0`), so
  data is readable and the instance keeps serving.
* `sys/rekey` and `sys/generate-root` both require an existing unseal key to authorise,
  and there is none. **Unseal capability cannot be restored on this instance.**
* `cerulean-vault` runs with `restart=unless-stopped`, started 2026-11-15 23:57Z. A
  restart, container recreation, `docker compose down`, or a reboot of the docker host
  leaves the vault sealed, with no path back.
* The leaked share is inert — it cannot unwrap the barrier either — so the original
  disclosure is neutralised, at the cost of having no key at all.

## Why a rebuild is viable

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

## Guardrails until that is done

1. Do not restart `cerulean-vault`, do not recreate the compose project, and avoid
   rebooting the docker host that carries it.
2. Keep `init/init.json` and every `init/init.json.pre-rekey-*` — the root token is the
   only working credential.
3. `init/init.json` still holds the inert share. Treat them as historical, not as keys.

## Verification recipe (repeat before any future rekey is called done)

Copy the storage, `chown -R 100:100`, run a throwaway `hashicorp/vault:1.17` on a spare
port against it, and unseal **that copy** with the share that was just stored. A rekey
that has not been proven this way is not a rekey.
