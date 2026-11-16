# christ staging — bring the recovered stack up with telephony off

Runbook for the stack whose data was recovered from `i2`'s reinstall
(`docs/christ-restore-2026-11-16.md`). The point of this directory is one rule:

> **Nothing here may register to the trunk.** DID `14132642700` is live on `i4`'s
> `telno`. A second registration on the same trunk breaks the working line, and that
> failure looks like a carrier fault, not like our mistake.

## What is in here

| File | Role |
|---|---|
| `compose.staging.yml` | overlay applied on top of the upstream `christ/compose.yml`; re-homes `gateway`, `telno`, `fax-engine` to a profile nothing activates, and moves the edge to 8080/8443 |
| `.env.staging.example` | every variable the enabled profiles need, plus an explicit list of trunk variables that must stay absent |

The upstream compose is **not** vendored here. Take it from the `jesus` repository
(`christ/compose.yml`, 31 070 bytes, 35 services) — a copy would drift.

## Prerequisites

- Docker on the target host. **Not `i2`**: it has no Docker, and this is not a reason to
  install one there.
- Three secrets (the upstream fails closed on them):
  ```bash
  openssl rand -hex 32   # CANTUS_SECRET
  openssl rand -hex 32   # CANTUS_CONTROL_TOKEN
  openssl rand -hex 32   # DOGRAH_JWT_SECRET
  ```
  Note that `CANTUS_CONTROL_TOKEN` and `CANTUS_SECRET` were the blockers for restoring
  the *original* instance, because that instance has an identity to preserve. A staging
  instance does not: it can issue its own, which is why staging is unblocked while the
  production restore is not.
- ~40 GB free. The `agents` profile pulls `dograh-db`, `dograh-redis` and `dograh-minio`,
  and `speech` pulls the TTS voice models.

## Bring-up order

Groups are separate `up` calls so a failure is attributable to one group.

```bash
cd /opt/christ
cp .env.staging.example .env.staging && chmod 600 .env.staging
$EDITOR .env.staging

COMPOSE="-p christ-staging --env-file .env.staging \
  -f /opt/jesus/christ/compose.yml -f compose.staging.yml"

# 0. Prove the trunk guard before starting anything.
docker compose $COMPOSE --profile core --profile console config --services | sort
#    must NOT contain: gateway, telno, fax-engine

# 1. Front door and voice plane (no trunk involved)
docker compose $COMPOSE --profile core up -d

# 2. Console
docker compose $COMPOSE --profile console up -d

# 3. Agents (db, redis, minio, api, ui)
docker compose $COMPOSE --profile agents up -d

# 4. Speech, then capstone
docker compose $COMPOSE --profile speech up -d
docker compose $COMPOSE --profile capstone up -d
```

## Verification — not "the container is Up"

```bash
docker compose $COMPOSE ps --format 'table {{.Name}}\t{{.Status}}'
curl -sf localhost:${SWITCH_PORT}/health          # voice plane answers
curl -sf localhost:${CHRIST_PORT}/health          # front door answers
curl -sf localhost:${DOGRAH_PORT}/health          # agents answer
ss -lunp | grep -E '5060|5062'                    # MUST be empty: no SIP border
```

That last line is the real acceptance test. If anything is listening on 5060 or 5062, the
staging stack has a SIP border and it must be stopped before it registers.

## Recovered data — when, and when not

`i2:/srv/recovered` holds the original instance's volumes. **Leave them alone for this
build.** They are only meaningful to a `telno` and a Postgres cluster that this staging
stack deliberately does not run. Restoring them is Step 2 and Step 3 of
`docs/christ-restore-2026-11-16.md`, and Step 3 is gated on the trunk decision.

If a copy of the recovered data is wanted for inspection, copy it — do not bind-mount
`/srv/recovered` into a container that might write to it, because it is the only copy.

## Teardown

```bash
docker compose $COMPOSE --profile core --profile console --profile agents \
  --profile speech --profile capstone down
docker volume ls | grep christ-staging      # named volumes survive `down` unless -v
```

`down -v` deletes the staging volumes. Nothing here is the recovered original, so that is
safe — but check the project name first.
