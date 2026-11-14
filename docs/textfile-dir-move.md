# Moving node-exporter's textfile directory on `.71`

The repository's default moved on 2026-11-14 (`docs/root-paths.md`): `TEXTFILE_DIR` is
`/var/lib/node_exporter/textfile`, not `/root/cerulean-backup/authentik`. This is the apply
for the host that runs both writers, which is `.71` — the `proxy` container's host, where
the per-host metrics stack (`extensions/monitoring/docker-compose.host.yml`) and the
Cerulean Authentik dump live.

It is ordered, and the order is the whole document. Two writers publish into one directory
that one collector mounts, and the failure of getting it wrong is silence in both
directions:

- move the collector and not the **dump**, and `cerulean_authentik_backup_*` stops
  existing. `extensions/monitoring/prometheus/rules/backup.yml` keys on those series being
  *present*, so the backup alerts — the ones that exist because a backup that stopped is
  otherwise invisible — stop evaluating and fire nothing.
- move the **checks** and not the collector, and every `estate-check-*.prom` lands where
  nothing scrapes it. All of them go absent together, and `EstateCheckAbsent` takes about a
  day to say so.

## Before you start

```bash
# the host, and the two directories that must end up as one
hostname -I
ls -l /root/cerulean-backup/authentik/            # what is there now
systemctl show -p Result -p ExecMainStatus estate-checks.service

# take the proof of "before" — this is the only artefact that can tell a move from a loss
cd /usr/src/projects/complete/platform-stack
./scripts/textfile-series-snapshot.sh --snapshot /tmp/textfile-before.txt
```

`--snapshot` exits 2 if it cannot read Prometheus or gets an empty answer, and an empty
answer is not a pass here: two empty snapshots diff to "nothing disappeared" and prove
exactly nothing. Expect a few dozen series.

## 1. The dump's writer — the one that is not in this repository

The Authentik database backup lives in the Cerulean repo and writes `status.prom` into the
directory. **This step is first**, because until it is done any step after it splits the
two writers apart.

```bash
# inside the container that runs it (the dump is Cerulean's, on this host)
incus exec cerulean -- sh -c 'grep -rn "cerulean-backup/authentik\|TEXTFILE_DIR\|status.prom" /usr/src/projects/complete/1-primary/cerulean/scripts/ 2>/dev/null'
```

Change the path it writes to `/var/lib/node_exporter/textfile/status.prom`, keep a dated
copy of the file beside itself the way that repo already does (`cp x.sh x.sh.bak-20261114`),
and leave the *metric names* alone — the rules key on `cerulean_authentik_backup_*`, not on
the directory.

## 2. The directory itself

```bash
install -d -m 0755 /var/lib/node_exporter/textfile
# move what is already there, so the first scrape after the switch is not empty
mv /root/cerulean-backup/authentik/*.prom /var/lib/node_exporter/textfile/ 2>/dev/null
chmod 0644 /var/lib/node_exporter/textfile/*.prom
```

Mode matters twice: node-exporter reads as an unprivileged user, and systemd's
`NamedTemporaryFile` alternative (`estate_check.write_prom`) writes 0600 then chmods to
0644 — a directory that is 0700 silently produces no series at all.

## 3. The collector's mount

`TEXTFILE_DIR` in the metrics stack's `.env.host` on this host. The compose file's default
is now the new path, so **unset** is correct; a value still naming the old directory is how
the split comes back:

```bash
cd /usr/src/projects/complete/ips/extensions/monitoring
grep -n 'TEXTFILE_DIR' .env.host || echo 'unset — correct, the compose default is the new path'
docker compose -p innotel-metrics -f docker-compose.host.yml --env-file .env.host up -d node-exporter
```

## 4. The checks

The unit ships the new default. A host that has an `estate-checks.env` naming the old path
wins over nothing — `Environment=` in the unit is applied after `EnvironmentFile=`, so the
unit's value is what runs. Reload and re-run once rather than waiting for the half hour:

```bash
systemctl daemon-reload
systemctl start estate-checks.service
systemctl show -p Result -p ExecMainStatus estate-checks.service
ls -l /var/lib/node_exporter/textfile/estate-check-*.prom | head -3
```

## 5. The proof it moved rather than vanished

```bash
cd /usr/src/projects/complete/platform-stack
./scripts/textfile-series-snapshot.sh --snapshot /tmp/textfile-after.txt
./scripts/textfile-series-snapshot.sh --diff /tmp/textfile-before.txt /tmp/textfile-after.txt
```

Exit 0 prints anything new and says nothing disappeared. Exit 1 names every series that
existed before and does not now, which is the only answer that means the move lost data —
and the two rule sets that should have caught it are the two that would have gone quiet,
so this is not a redundant check.

Then confirm the collector is reading the new directory, not an empty mount:

```bash
curl -s 127.0.0.1:9100/metrics | grep -c 'innotel_estate_check_last_run_timestamp'
curl -s 127.0.0.1:9100/metrics | grep 'cerulean_authentik_backup_last_status'
```

## Rollback

Put `TEXTFILE_DIR=/root/cerulean-backup/authentik` back in `.env.host`, move the `.prom`
files back, reverse step 1, recreate `node-exporter`, `daemon-reload` and `systemctl start
estate-checks.service`. The series return; the windows in between are missing scrapes, not
missing history, because Prometheus keeps what it already stored.

## What would have caught this, without being asked

Nothing, and that is worth writing down. `EstateCheckAbsent` catches the checks' half after
about a day, and `backup.yml` catches neither half: it asks whether the last dump was good,
and a dump that is writing to a directory nobody scrapes keeps on being good. The snapshot
above is a one-off proof for the day of the move; the standing proof is that
`innotel_estate_check_last_run_timestamp` has a series per check, which the dashboard's
"Checks reporting" panel counts against the unit's own `ExecStart` list.
