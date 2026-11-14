# `regcache` — the Docker Hub credential in the registry

Checked **2026-11-13**. `regcache` is an Incus container on `i1` (`192.168.1.79`) running a
single Docker container, `registry:2`, as a pull-through cache of `registry-1.docker.io`.

## What is running

| | |
|---|---|
| image | `registry:2`, `Up 15 hours` at the check |
| port | `5000`, published on the container |
| storage | `/srv/registry` inside the container, bound to `/var/lib/registry` |
| upstream | `REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io` |
| credentials | `REGISTRY_PROXY_USERNAME=innotel`, `REGISTRY_PROXY_PASSWORD=<personal access token>` |
| how it starts | no compose file and no `.env` anywhere — the credential lives in the container's `Config.Env` |

That last row is the problem. A Docker Hub personal access token sits in plaintext in the
container environment, readable by anything that can reach `docker inspect` inside
`regcache`, and it survives restarts because it is baked into the container definition.

## Why it was not rotated into Vault

Two blockers, both outside the estate's reach tonight.

1. **Rotation needs the Docker Hub account.** A PAT cannot mint its successor, and the
   `/v2/users/login` route needs the account password. No copy of that password exists on
   the host: a search of `/srv`, `/root` and `/etc` finds only scripts that name the
   variable, not a value.
2. **No token on the estate may create the secret path.** Vault holds fifteen product
   tokens under `/vault/token/*.token` (mode 0600, read by `sage-deploy.sh` and
   `sage-vault-fix.sh`). All fifteen return **`deny`** for
   `vault token capabilities secret/data/cerulean/regcache`, including `cerulean` — whose
   policy does cover paths that already exist (`vault kv list cerulean/` succeeds, and it
   lists `technitium`, `gateway`, `magnate/`, `pki/` and others), but not a new one.
   `vault secrets list` is 403, so the mount table cannot even be read.
   Nothing under `cerulean/` already holds a Hub credential: `cerulean/registry`,
   `cerulean/dockerhub`, `cerulean/regcache` and `cerulean/docker` all return
   "No value found".

The `cerulean` token itself is healthy — policy `[cerulean default]`, 764 h of TTL left,
expires 2026-12-15 — it simply is not permitted to invent a secret path.

## Finishing it, once the account is available

```bash
# 1. in the Docker Hub UI, create a replacement PAT for "innotel"
#    (do not reuse the exposed one, and note its scope: read-only is enough for a cache)

# 2. store it -- needs a token whose policy allows writes under cerulean/*,
#    or a root token, neither of which exists on this host today
incus exec proxy -- docker exec cerulean-vault \
  sh -c 'VAULT_TOKEN=<token> vault kv put cerulean/regcache username=innotel password=<new-pat>'

# 3. replace the container so the secret comes from a 0600 file, not the container env.
#    The file must carry EVERY variable the container runs with today --
#    the two REGISTRY_PROXY_* values, REGISTRY_HTTP_ADDR and
#    REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY -- with only the password changed.
incus exec regcache -- sh -c 'umask 077; cp /dev/null /srv/registry/registry.env'
incus exec regcache -- docker rm -f registry
incus exec regcache -- docker run -d --name registry --restart unless-stopped \
  --env-file /srv/registry/registry.env \
  -p 5000:5000 -v /srv/registry:/var/lib/registry registry:2

# 4. revoke the old PAT in the Hub UI, then prove the cache still serves
incus exec regcache -- sh -c 'curl -s -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:5000/v2/library/alpine/manifests/latest'
```

Do not delete `/srv/registry` during the swap: it is the cached layer store, and the
replacement container reuses it so the cache does not go cold. Until step 1 happens, the
exposed token stays valid — that part cannot be fixed from this host.
