# `regcache` — the Docker Hub credential in the registry

Checked **2026-11-13**. `regcache` is an Incus container on `i1` (`192.168.1.79`) running a
single Docker container, `registry:2`, as a pull-through cache of `registry-1.docker.io`.

## What is running

| | |
|---|---|
| image | `registry:2` |
| restart policy | `always` |
| network | `bridge`, `5000/tcp` published as `5000` |
| storage | `/srv/registry` inside the container, bound to `/var/lib/registry` |
| address | `http://192.168.1.79:5000/v2/` answers **200** |
| upstream | `REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io` |
| credentials | `REGISTRY_PROXY_USERNAME=innotel`, `REGISTRY_PROXY_PASSWORD=<personal access token>` |
| config on disk | **none** — no compose file, no `.env`, no unit; the whole spec is the container |

That last row is the problem. The token lives in the container's `Config.Env`, so it is
readable by anything that can run `docker inspect` in that container, and it comes back
unchanged on every restart because it is part of the container definition.

## Why it was not rotated into Vault on 2026-11-13

Two blockers, both outside the estate's reach that day.

1. **Rotation needs the Docker Hub account.** A PAT cannot mint its successor, and the
   `/v2/users/login` route needs the account password. No copy of that password exists on
   the host: a search of `/srv`, `/root` and `/etc` finds only scripts that name the
   variable, not a value.
2. **No token on the estate may create the secret path.** (Fixed on 2026-11-14 — see the
   update below.) Vault holds fifteen product
   tokens under `/vault/token/*.token` (mode 0600, read by `sage-deploy.sh` and
   `sage-vault-fix.sh`). All fifteen return **`deny`** for
   `vault token capabilities secret/data/cerulean/regcache`, including `cerulean` — whose
   policy does cover paths that already exist (`vault kv list cerulean/` succeeds, listing
   `technitium`, `gateway`, `magnate/`, `pki/` and others), but not a new one.
   `vault secrets list` is itself 403, so the mount table cannot be read.
   Nothing under `cerulean/` already holds a Hub credential: `cerulean/registry`,
   `cerulean/dockerhub`, `cerulean/regcache` and `cerulean/docker` all return
   "No value found".

The `cerulean` token is healthy — policy `[cerulean default]`, expires 2026-12-15 — it is
simply not permitted to invent a path. That is still true; what changed is that a token
which *is* permitted now exists.

## Update, 2026-11-14 — the Vault half is runnable

Blocker 2 is gone. Vault carries a dedicated policy and a token scoped to exactly one path:

| | |
|---|---|
| policy | `regcache-writer`: `create`/`update`/`read` on `secret/data/cerulean/regcache`, `read` on its metadata |
| token | `/vault/token/regcache.token` inside `cerulean-vault` (mode 600); copy at `/root/vault.token` on **i1** (mode 600); TTL 768 h, renewable |
| address from i1 | `http://192.168.1.71:8200` — Cerulean Vault runs in `cerulean-vault` inside the `proxy` container on i2 |

Created once with the instance root token kept in `/vault/init/init.json` (mode 600, inside
the container; it was read there and never copied off the host). The grant was then proven
from **i1**, the host that must run the rotation, not from inside Vault:

```bash
curl -s -H "X-Vault-Token: $T" -X POST \
  -d '{"paths":["secret/data/cerulean/regcache"]}' \
  http://192.168.1.71:8200/v1/sys/capabilities-self
# {"secret/data/cerulean/regcache":["create","read","update"], ...}
```

The script no longer requires a `vault` CLI — **i1 has none**, and that alone made the Vault
step impossible there. Capability check and write now go through the HTTP API with `curl`,
so the Vault half is one flag away:

```bash
bash platform-stack/scripts/rotate-regcache-pat.sh \
  --pat-file /root/new-hub-pat \
  --vault-token-file /root/vault.token
```

A dry run on i1 (`--dry-run --skip-hub-check`) passes the capability gate and stops before
writing anything to Vault or to the registry.

**The only remaining blocker is the Docker Hub PAT itself**, which needs the `innotel`
account. Nothing on this estate can mint it, and no amount of Vault work substitutes for it.

## Rotating it: one command

`scripts/rotate-regcache-pat.sh` is the whole rotation, and it is written to fail closed:
it proves the new token against the Hub *before* touching anything, keeps the old
environment in a 0600 file, and puts the old container back if verification fails.

```bash
# 1. create a replacement PAT for "innotel" in the Docker Hub UI (read scope is enough),
#    then write it to a file on i1 that only root can read -- never on the command line
install -m 600 /dev/null /root/new-hub-pat && vi /root/new-hub-pat

# 2. rotate, verify, and keep the Vault step for when a write-capable token exists
bash platform-stack/scripts/rotate-regcache-pat.sh --pat-file /root/new-hub-pat
```

What it does, in order:

1. rejects an empty or malformed file, and refuses tokens containing whitespace or
   characters it cannot substitute safely;
2. `curl -u innotel:<token> https://registry-1.docker.io/v2/` and **dies on anything but
   200** — a bad token never reaches the container (`--skip-hub-check` overrides, offline);
3. if `--vault-token-file` is given, checks
   `vault token capabilities secret/data/cerulean/regcache` for `create`/`update` first and
   stops with exit 2 if the token cannot write it;
4. reads the live spec (`image`, restart policy, network mode, ports, binds, every env
   variable), rewrites the environment with only `REGISTRY_PROXY_PASSWORD` replaced, pushes
   it to `/root/registry.env` at mode 0600 inside `regcache`;
5. removes and recreates `registry` with `--env-file` instead of `-e`;
6. verifies the new container's `Config.Env` no longer contains the password, the file is
   0600, `/v2/` answers 200, and `library/alpine` resolves through the Hub (401 means the
   token cannot pull);
7. logs that the **old token is still valid until it is revoked in the Hub UI**.

Then revoke the old token. Do not delete `/srv/registry` during the swap — it is the cached
layer store, and the recreated container reuses it so the cache does not go cold.

Full flow with Vault, once a token with write access to the path exists:

```bash
bash platform-stack/scripts/rotate-regcache-pat.sh \
  --pat-file /root/new-hub-pat \
  --vault-token-file /root/vault.token
```

Until the Hub account is available, the exposed token stays valid. That part cannot be
fixed from this host, and no amount of Vault work substitutes for it.
