# `christ` and `scif` on `i2` — what the reinstall destroyed, what survives, and how to bring it back

Measured **2026-11-16**. Answers two questions: were the `christ` and `scif` Docker
stacks lost with `i2`'s reinstall, and what can be restored.

## Verdict

Both stacks are **gone from `i2`**, and neither can be brought back *on `i2` as it
stands* — but their data and configuration survive, and the recoverable parts are now
copied onto `i2` under `/srv/recovered` (50 MB, verified on the receiving side).
**Nothing has been started.**

`i2` is `Ubuntu 26.04.1 LTS`, booted `2026-11-16T04:56:32Z`, with **no `/var/lib/docker`**,
no Docker volumes, no compose files, and **zero `christ` or `scif` traces**. The only
Docker-era remains are four empty `ontrak_*` directories of 4 KB each. The reinstall took
the disk; it was not a failed stack that could be restarted.

One reading that looks alarming and is not: `i2`'s used space rose from about 12 G to 69 G
in the same window, which resembles reinstall churn. It is `monarch`'s media migration
landing — `/tank/media-downloads` on `i2` held **57 G across 185 files** at 02:08.

## What survives, and where it is now

The evacuation snapshot of that Docker host is on `i1` under `tank/evac` (23.3 G). Nine
paths were relevant; everything recoverable from them is copied to `i2:/srv/recovered`,
with file counts and sizes confirmed after extraction.

| Recovered | Size | Contents |
|---|---|---|
| `config/christ/compose.yml` | 31 070 B | **35 services, 22 `christ-*` containers** |
| `srv/evac/dev-critical/volumes/christ_telno-data` | 1.4 M, **21 files** | `telno.db`, `session.secret`, `api-key.pepper`, `cantus/`, `recordings/`, `prompts/`, `backups/` |
| `srv/evac/dev-critical/volumes/christ_switch-data` | 20 K, **0 files** | empty — the switch kept no state |
| `srv/evac/copilotkit-docker/volumes/scif_scif-data` | 47 M, **1 284 files** | a complete **Postgres data directory** (`pg_ident.conf`, `base/`, `global/`) |
| `srv/evac/copilotkit-fs/root/scif` | 128 K | the `scif` application tree |
| `srv/evac/copilotkit-fs/root/scif.tgz` | small | deployment artifact, same era |
| `srv/evac/copilotkit-fs/root/scif-deploy.log` | small | the deployment log |

The 22 containers `christ/compose.yml` defines:

```
christ-switch   christ-edge     christ          christ-gateway  christ-telno
christ-fax      christ-console  christ-dograh-db christ-dograh-redis
christ-dograh-minio  christ-dograh-api  christ-dograh-ui
christ-tts-voices    christ-tts-shim    christ-omniroute
christ-webmail  christ-capstone-api  christ-capstone  christ-capstone-studio
christ-n8n      christ-grist    christ-ontrak
```

## Why it cannot simply be started

1. **The secrets are not in any snapshot.** Only `.env.example` survives — 690 B in the
   evac tree, 3 095 B in the `jesus` working copy on `i4`. The deployed `.env` is absent,
   and `christ-telno` requires `CANTUS_CONTROL_TOKEN` with the `:?` form, so `docker
   compose up` fails closed rather than starting half-configured. The same missing file
   holds the database passwords, the trunk credentials and the mail-provider keys.
2. **It would collide with the live phone line.** `estate.json` points the trunk at
   `voipms@gateway` for DID **`14132642700`**, and `telno` is **running on `i4`** right
   now. Bringing up `christ-gateway` or `christ-switch` puts a second registration on the
   same trunk — the fastest way to make the live line flap. This is an owner decision,
   not an inference.
3. **`i2` has no Docker at all.** Installing it is a deliberate step, not a side effect
   of recovery.

`scif` is a special case: it is **not in the `jesus` repository at all** (`grep` finds
nothing). It is a CopilotKit-era stack, which is why only its volume and application
tarball exist — there is no compose file for it anywhere to restore.

## Restore sequence

### Step 0 — prerequisites, both blocked on the owner

- The deployed `christ` `.env`, or a decision to re-issue: `CANTUS_CONTROL_TOKEN`, the
  Postgres passwords, the Resend/Mailjet keys, and the VoIP.ms trunk credentials.
- An answer on the SIP collision: either stop `telno` on `i4` for the cut-over window,
  or configure the restored stack against a **different DID/trunk** so both can run.

### Step 1 — stage on a host that has Docker

Do **not** install Docker on `i2` just for this. `i2` is a 468 G NVMe app host with no
Docker today, and the rebuilt stack wants Postgres, MinIO, Redis and 35 services. Use a
host that already runs Docker, or the replacement mini-PC if it is being bought anyway.

Copy the recovered artifacts to the staging host:

```bash
rsync -a root@192.168.1.52:/srv/recovered/config/christ/ /opt/christ/
rsync -a root@192.168.1.52:/srv/recovered/srv/evac/dev-critical/volumes/christ_telno-data/_data/ /srv/telno-data/
rsync -a root@192.168.1.52:/srv/recovered/srv/evac/copilotkit-docker/volumes/scif_scif-data/_data/ /srv/scif-data/
```

The `_data` level matters: these were Docker volume directories, so the payload is one
level below the volume name, not at it.

### Step 2 — `scif`'s database first, because it is the only irreplaceable part

The 1 284 files are a Postgres cluster, not a dump. Two ways in, in order of preference:

```bash
# Preferred: start the matching Postgres major version against a copy of the data dir.
docker run --rm -v /srv/scif-data:/var/lib/postgresql/data postgres:<major>
pg_dumpall -U postgres > /srv/scif-$(date +%F).sql     # take a logical dump immediately

# If the major version is unknown, read it out of the cluster before starting anything:
grep -a 'PG_VERSION\|server_version' /srv/scif-data/PG_VERSION /srv/scif-data/postgresql.conf
```

Take the logical dump **before** attempting any upgrade. A cluster restored onto the
wrong major version is a one-way door; a dump is not.

### Step 3 — `telno`, only after the SIP question is answered

`/srv/telno-data` already contains `telno.db`, `session.secret` and `api-key.pepper`, so
the instance's identity and its API key survive. Start it with
`CANTUS_CONTROL_TOKEN` supplied and **with the trunk disabled or pointed elsewhere**,
confirm the console answers locally, and only then decide when the DID moves.

### Step 4 — the rest of `christ`

Bring up the non-telephony services first (`christ-console`, `christ-webmail`,
`christ-grist`, `christ-n8n`, `christ-omniroute`, `christ-capstone*`), then
`christ-dograh-*` (db, redis, minio), and telephony **last**. `docker compose up -d
<service>` per group keeps a failure attributable to one service instead of to 22.

### Verification — what "restored" has to mean

```bash
docker compose -f /opt/christ/compose.yml ps --format '{{.Name}}\t{{.State}}'   # every service up
curl -sf localhost:<telno>/health                                               # not just "container running"
psql -U postgres -c 'select count(*) from information_schema.tables'            # scif data present
```

A container in `Up` state is not a restore. The test is that telno answers with the
recovered database and `scif` returns rows from its own schema.

### Rollback

Nothing here writes to `/srv/recovered`; it stays the untouched reference copy. A failed
attempt costs only the staging host's containers, which is why Step 1 insists on a host
that is not holding the only copy.

## Open questions for the owner

1. Was `christ` in production, or a staging stack? The evac tree is a rootfs copy of a
   *live* host, which argues production, but nothing on `i2` since the reinstall
   referenced it.
2. Does the DID stay on `i4`'s `telno`, or move to the restored stack? Duplicate
   registration is the one failure mode that breaks the working line.
3. Is `scif` retired? Its data is preserved either way, but if it is retired, the 47 MB
   Postgres cluster is an archive rather than a restore target.
