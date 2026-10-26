# OmniRoute image — build & publish (ghcr.io/innotelinc/omniroute)

The gateway runs an image **we** publish, not upstream's floating `:latest`, so a
release is a tag we control and can roll back.

- Upstream repo:  https://github.com/diegosouzapw/omniroute
- Our mirror:     https://github.com/innotelinc/omniroute  (builds happen here)
- Release tag:    `v3.8.51`  (commit `c1e30b76`)
- Built by:       `.github/workflows/publish-image.yml` on the mirror (GitHub Actions)
- Published as:   `ghcr.io/innotelinc/omniroute:3.8.51` and `:latest`
- Deployed now:   `ghcr.io/innotelinc/omniroute:3.8.53` (read off the running
  container 2026-10-26) — the first GHCR release, so CI is now the build path.
- Rollback target: `innotel/omniroute:3.8.51` on Docker Hub
  (digest `sha256:f95f0f7e099c907fe8a88824c2a773e89c49b06e86282e7f3f284db4154df977`) —
  the last image built by hand on `.31`. It stays pullable and is what to fall
  back to if the 3.8.53 tag is bad.

## Build (CI — GitHub Actions on the mirror)

The build used to run by hand on `.31` and push from `.46`. **Both hosts are gone**
(see *Decommissioned build hosts* below), so the image is now built by CI from the
mirror and published to GHCR. The workflow is
`.github/workflows/publish-image.yml` in `innotelinc/omniroute`; a copy is kept
beside this file as `publish-image.yml`.

Triggers:

| Trigger | Use |
|---|---|
| `workflow_dispatch` with `version` (e.g. `3.8.52`) | the normal release path |
| tag push `v*` | mirroring a tag publishes it too |
| `release: released` | publishing a GitHub release |

`:latest` only moves on an explicit request (`-f promote_latest=true`). A tag push
never moves it, so the rollback target cannot shift as a side effect of publishing
an unrelated version.

```bash
# 0) the mirror must actually have the tag — it lags upstream (see caveat below)
gh api repos/innotelinc/omniroute/git/ref/tags/v3.8.52 --jq '.ref'   # expect the ref

# 1) build the image
gh workflow run publish-image.yml -R innotelinc/omniroute -f version=3.8.52

# 2) watch it (the build is a full Next.js production build — tens of minutes)
gh run list -R innotelinc/omniroute --workflow publish-image.yml --limit 3
gh run watch -R innotelinc/omniroute "$(gh run list -R innotelinc/omniroute \
  --workflow publish-image.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

**Runner.** The workflow runs on `ubuntu-24.04` unless the repository variable
`ESTATE_RUNNER` names something bigger. Upstream documents that this tree does not
build on a standard hosted runner — *"Hosted ubuntu-24.04 is ~7 GB and dies
ResourceExhausted (#11976)"* — and their own amd64 leg runs on a 31 GB / 32-core
box. If a build dies early, that is the cause, not the tree:

```bash
gh variable set ESTATE_RUNNER -R innotelinc/omniroute \
  --body '["self-hosted","estate-build"]'      # or e.g. "ubuntu-24.04-4-cores"
```

**Why those build args** (learned the hard way, still what the workflow passes):

| Setting | Value | Why |
|---|---|---|
| `OMNIROUTE_BUILD_MEMORY_MB` | **12288** | 6144 OOMs the Next.js webpack worker (`Ineffective mark-compacts near heap limit` → SIGABRT). |
| `OMNIROUTE_USE_TURBOPACK` | **0** | Turbopack's Rust workers size to all cores and OOM-killed a 15 GB VM. |
| Target | `runner-base` | What upstream publishes and what the gateway runs (~500 MB, no browsers). |
| Platform | `linux/amd64` | The estate gateway host is amd64. Upstream's arm64 leg is for their public tag, not for us. |

Success signature: `✓ Compiled successfully`, then `Generating static pages using 1 worker (618/618)`.
One benign warning: `SecretsUsedInArgOrEnv` for `ENV "REQUIRE_API_KEY"` (Dockerfile line 250).

**No credentials are needed for this.** `innotelinc/omniroute` has zero Actions
secrets (checked 2026-10-18), so a Docker Hub push has no credential to use; GHCR
authenticates with the job's own `GITHUB_TOKEN` (`packages: write`), the same shape
as `innotelinc/cerulean`'s `release.yml`. The gateway host already holds a
`ghcr.io` login in `/root/.docker/config.json`, so it can pull the result.

> **Caveat — the mirror lags.** As of 2026-10-18 the mirror's newest tag is
> `v3.8.50` and its last commit is from 2026-09-09 (`#13072`), i.e. it is behind
> this gateway's `v3.8.51`. Sync the tag before dispatching a release build, or
> the job will fail on a missing `refs/tags/v<version>`.

## Deploy on the gateway host (.71)

`OMNIROUTE_IMAGE` in `.env.host` selects the image; the compose file interpolates
`\${OMNIROUTE_IMAGE:-diegosouzapw/omniroute:latest}`. Set it, then recreate **only**
the `omniroute` service — the `omniroute-data` volume (and every secret) is untouched,
and `gateway-sso` is left alone:

```bash
cd /usr/src/projects/complete/ips/extensions/llm
cp -a .env.host .env.host.bak.$(date +%Y%m%d-%H%M%S)
# .env.host:
#   OMNIROUTE_IMAGE=ghcr.io/innotelinc/omniroute:3.8.52
docker compose --project-name innotel-gateway --env-file .env.host -f docker-compose.host.yml pull omniroute
docker compose --project-name innotel-gateway --env-file .env.host -f docker-compose.host.yml up -d omniroute
```

If GHCR answers `unauthorized`, log in on this host first — the credential is
already there, it is not needed for a public package:

```bash
docker pull ghcr.io/innotelinc/omniroute:3.8.52
```

## Verify

```bash
docker inspect -f '{{.Config.Image}} {{.State.Status}} {{.State.Health.Status}}' omniroute
docker exec omniroute node -e 'console.log(require("/app/package.json").version)'   # expect the version you deployed
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20128/api/health          # 200 {"status":"ok"}
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20128/                    # 307 -> auth, healthy
curl -sS -o /dev/null -w '%{http_code}\n' https://gateway.innotel.us                 # 302 -> SSO
```

## Roll back

Point `OMNIROUTE_IMAGE` at the previous image and re-run the two `docker compose`
lines above. `omniroute-data` is a named volume and survives a recreate.

- previous GHCR release: `ghcr.io/innotelinc/omniroute:<previous-version>`
- the last hand-built release: `innotel/omniroute:3.8.51`
- upstream, if all else fails: `diegosouzapw/omniroute:3.8.51`

## Decommissioned build hosts (checked 2026-10-18)

Both hosts this document used to name are gone, from the gateway host:

| Host | Was | Now |
|---|---|---|
| `.31` | the build host ("the big machine", builder `omni4`) | answers ARP (`68:fc:ca:d8:e7:f8`) but **no open port** on 22/80/443/2375/2376/5000 — nothing is building there |
| `.46` | held the Docker Hub push credentials | **no reply** to ICMP |

Verified rather than assumed: `docker manifest inspect` / Hub API — `innotel/omniroute:3.8.52`
and `diegosouzapw/omniroute:3.8.52` both return 404, so as of this date **v3.8.52 has
not been released anywhere** (newest tag `v3.8.51`; upstream's `main` still reports
`"version": "3.8.51"`; npm `dist-tags.latest` is `3.8.51`).

## 2026-09-30 deployment

v3.8.51 deployed to .71: image `innotel/omniroute:3.8.51`, container healthy,
reporting version 3.8.51, gateway port 20128 answering.
