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
| address from i1 | `https://secrets.cerulean.innotel.us` — Cerulean Vault runs in `cerulean-vault` inside the `proxy` container on i2, and its port is published on that host's loopback only (2026-11-20: it used to answer over plain http on the LAN, which is how an OIDC sign-in ended up asking for an http callback) |

Created once with the instance root token kept in `/vault/init/init.json` (mode 600, inside
the container; it was read there and never copied off the host). The grant was then proven
from **i1**, the host that must run the rotation, not from inside Vault:

```bash
curl -s -H "X-Vault-Token: $T" -X POST \
  -d '{"paths":["secret/data/cerulean/regcache"]}' \
  https://secrets.cerulean.innotel.us/v1/sys/capabilities-self
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

## Cache health, 2026-11-14

Measured, not guessed:

| | |
|---|---|
| build | `registry:2` (distribution 2.8.3) on `192.168.1.79:5000` |
| storage | `/srv/registry`, **57 MB**, 131 blobs, on a 430 G volume with 700 MB used (1 %) |
| upstream TTL | 168 h — a cached tag is re-fetched after a week |
| requests | 2 846 success, 123 `500`, 91 `404` over the logged period |
| cache fills | 791 upstream fetches |
| hit share | ≈ **72 %** of successful requests served without an upstream fetch, i.e. (2846 − 791) / 2846 |
| live check | `prom/prometheus:v3.13.3` 200 in 0.39 s, `grafana/grafana:13.2.2` 200 in 0.20 s, unknown tag 404 |

The 123 `500`s are real, and the earlier explanation for them on this page — "the
password is the expired PAT" — **is not supported by measurement on 2026-11-14**:

| test | result |
|---|---|
| token request with the stored credential | **200**, token claims `pull_limit: 200`/6 h and `username: innotel` — i.e. an **authenticated** token |
| the same request with a deliberately wrong secret | **401** `incorrect username or password` |
| the same request with no credentials | **200**, but `pull_limit: 100` — the anonymous tier |
| `401`/`authorization failed` anywhere in the registry log | **0** |

Two facts sit together and do not fully explain each other:

- **Now** the stored credential yields an authenticated token (`pull_limit: 200`), no
  request has ever been refused upstream, and there were **no** rate-limit errors in the
  last 24 h.
- **Then** every one of the 123 errors names the *unauthenticated* limit, and all of them
  predate the container's current start: `registry` was created 2026-10-20 and started
  **2026-11-13 11:51**; the last error is **2026-11-12 04:22:11**.

So the anonymous behaviour belongs to the previous incarnation. Distribution reaches the
Hub through a token exchange that falls back to anonymous when the configured credential
is not accepted, and logs no 401 for it — consistent with what is here, and offered as
the likely mechanism rather than proof, because the log cannot show which token the old
container held.

Shapes, so the next occurrence is recognisable: **~100 errors in 45 minutes on
2026-11-04** (04:15–05:00, one heavy pull of uncached images), then **2 a day at 04:22**
on 11-09 through 11-12 (a scheduled puller, most likely Watchtower on `monarch`), plus
single hits on 10-25, 10-26, 10-28, 10-30 and 11-01.

The lesson is not which token expired. It is that **a restart repaired it silently**, and
nothing recorded that the cache had been serving some pulls anonymously for three weeks —
hence the check below.

Hit share is a log-derived estimate: every upstream fetch logs `Adding new scheduler
entry`, and that is the only miss signal the registry exposes at info level.

## Watching the credential, 2026-11-14

An expired or unapplied token does not take the cache down — cached images keep serving —
so nothing in the estate noticed 123 failures. `scripts/regcache-credential-check.sh` is
the unit that notices:

| piece | path |
|---|---|
| script | `scripts/regcache-credential-check.sh` → `/usr/local/sbin/` |
| service | `systemd/regcache-credential.service`, `OnFailure=estate-notify@%n` |
| timer | `systemd/regcache-credential.timer` — daily 06:10, 30 min after boot |

It reads the credential from `/root/registry.env` in `regcache` when that exists and from
the container's `Config.Env` when it does not (which is the current state), asks
`auth.docker.io` for a `library/alpine` token, and **decodes the token's own claims**
before judging: `pull_limit: 200` means the credential was applied, `100` means the Hub
handed out an anonymous token. A plain `200` is not enough — the same endpoint answers
`200` to a request with no credentials at all, which is exactly how a review of this
check first "proved" a dead token was alive.

Exit **0** credential applied and no `toomanyrequests` in 24 h; **1** dead token, an
anonymous token, or uncached pulls already failing on the limit — which pages through
`estate-notify`; **2** could not run (no `incus`, no container, no readable credential),
never a pass. It pulls no layers and writes nothing.

```bash
systemctl start regcache-credential.service   # run now
journalctl -u regcache-credential.service -n 10 --no-pager
```

Run on 2026-11-14 with the tightened test: `HTTP 200, pull_limit 200/6h`, 0 anonymous
rate-limit errors in 24 h, exit 0.
