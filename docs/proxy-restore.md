# Rebuilding `proxy` — the edge restore point

`proxy` (192.168.1.71, on i2) is the one container in the estate whose loss is an
estate event rather than a service outage. It carries NPM's ingress for every
vhost, Authentik, Technitium DNS, Vault, Prometheus/Grafana and the units that
keep `innotel.us` up. Everything else can be rebuilt from a repo; this one cannot,
because three of its most important directories are **host paths**, not part of
its rootfs.

That is the assumption this page closes. `incus export` alone does not restore
the edge: a container rebuilt from the tarball comes up with an empty `/data`,
NPM starts with no `proxy_host/*.conf`, no `custom_ssl` and no `keys.json`, and
every symptom looks like an NPM fault rather than a restore fault. A restore point
here is all of it, verified, in one timestamped directory.

## What a restore point holds

`scripts/backup-proxy.sh` writes one directory per run, default
`/var/lib/incus/backups/proxy/<UTC stamp>/`:

| File | What it is |
|---|---|
| `proxy.tar.zst` | `incus export` of the container: rootfs, config, devices. Taken from a snapshot clone, so it is one consistent point in time |
| `data.tar.zst` | `/usr/src/proxy/data` — NPM `nginx/proxy_host/*.conf`, `custom_ssl`, `keys.json`, `database.sqlite`, `access`, `logs` |
| `deploy.tar.zst` | `/usr/src/innotel-deploy` (compose + units + reconciler) and `/usr/src/innotel-site` (site content) |
| `units.tar.zst` | `/etc/systemd/system` from inside proxy — the units live in the rootfs, and nobody unpacks 18 G to read one |
| `manifest.txt` | written last, so its absence means the directory is incomplete |
| `config.yaml` | `incus config show --expanded`, for a rebuild without the tarball |
| `inventory.txt` | addresses, `docker ps`, NPM host-conf count, the unit state at capture time |
| `manifest.txt` | sizes, sha256 of every artifact, and the counts the script checked |

The export is taken from a **snapshot clone**, not from the live filesystem. The
snapshot is copy-on-write and instant, the clone is exported and deleted, and the
live edge is never stopped. What lands in the tarball is one point in time rather
than 18 G of files read while Docker writes to them.

## Taking one

```sh
# on i2, as root
bash /root/backup-proxy.sh --offsite root@192.168.1.51:/var/backups/estate/proxy
```

The script checks its own work before claiming it has any: `zstd -t` on all four
archives, archived NPM host-conf count equal to the live count, `docker-compose.yml`
and the site content present in the deploy archive, and the `innotel-site` units
present in the unit archive. It keeps the newest 3 locally and the newest 3
offsite, and it fails loudly on anything it cannot verify.

Offsite matters: the local restore point sits on the `fast` pool, which is where
the container itself lives. `--offsite` copies it to i1 (`/var/backups/estate/proxy`),
so a lost pool is not a lost edge. i2 holds a root key for i1 for this, added
2026-11-02 and marked `backup@i2` in i1's `authorized_keys`.

## Taking one on a schedule

`hosts/i2/proxy-backup/proxy-backup.{service,timer}` run the same command weekly:
Sunday 03:30 with up to 45 minutes of jitter, `Persistent=true` so a missed
weekend is caught up at the next boot rather than skipped in silence.

```sh
systemctl list-timers proxy-backup.timer
journalctl -u proxy-backup.service -n 40
```

### Why the rotation is bounded by space, not just by count

`/var/lib/incus/backups` is its own ZFS dataset, `fast/incus-backups` — separate
from the pool root that holds `proxy`, `capstone` and `atheniq`, so a full backup
dataset cannot starve a container. It had **no quota** when this was scheduled
(`zfs get refquota` returned `none`, and the 40 G that `df` printed was just
used + pool-free), so backups could grow into the pool. It now carries
`refquota=40G` (`zfs set refquota=40G fast/incus-backups`, 2026-11-02). Each
point is about 11 G, so three fit in 33 G and a fourth does not.

That makes `--keep 3` necessary but not sufficient. Count-based retention prunes
after a run has finished, and the run that finds three points already there has
7 G to write 11 G into. So `backup-proxy.sh` decides space first:

1. It measures what a point costs — the newest point's size + 10 % + 2 G, or an
explicit `--min-free-gib`.
2. It frees that by deleting the **oldest** point, logging each one it gives up.
3. If the space still is not there, it exits with the numbers and writes nothing.

An old restore point is worth less than a failed write on top of it. Failures
also clean up after themselves: the directory is only complete when `manifest.txt`
exists, and a run that dies partway removes what it wrote.

## Restoring

```sh
# on i2, as root
bash /root/restore-proxy.sh /var/lib/incus/backups/proxy/<stamp> --replace
```

It refuses to touch an existing `proxy` without `--replace`, verifies the
manifest's checksums **before** deleting anything, then:

1. `incus import` the rootfs tarball (config and the three disk devices come back
   with it).
2. Restore `/usr/src/proxy/data`, `/usr/src/innotel-deploy`, `/usr/src/innotel-site`.
3. `chown -R 1000000:1000000` on all three — the idmap base read from the restore
   point's own config, not assumed. A restore that leaves them owned by host root
   comes up with an unwritable `/data`.
4. Re-assert the three disk devices (`raw-mount` → `/data`, `site`, `sitedeploy`)
   so a hand-built restore point cannot skip them.
5. Start, then check: address `192.168.1.71`, NPM host-conf count equal to the
   manifest, the container's own `docker ps` count at least the manifest's,
   `innotel-site.service` enabled, the reconcile timer active, and
   `https://innotel.us/` returning 200 through the restored NPM.

The unit files are inside the rootfs, so the import restores them and their
`*.wants` symlinks; the check above is what proves it rather than assuming it.

## What this does not cover

- **Other containers.** This is the edge, not the estate. `mail`, `capstone`,
  `onyx`, `atheniq`, `monitor` and the rest are rebuilt from their repos and their
  own `/data`; that is a separate procedure and the reason `--offsite` targets a
  directory named `proxy`.
- **Snapshots.** None are kept. The clone exists only for the duration of the
  export, and the snapshot is deleted once the tarball is written.
- **Filling the backup dataset.** `fast/incus-backups` is capped at 40 G, and the
  script refuses to start a write it cannot finish, but a `--keep` above 3 would
  simply fail every time. Raise the refquota before raising `--keep`.
- **Point-in-time consistency of `/data`.** Docker is still running while it is
  archived. NPM rewrites `proxy_host/*.conf` on config changes, not continuously,
  so this is a good point in time; it is not a filesystem snapshot.
- **The rest of the host.** i2's own configuration (the bridge-netfilter units,
  for example) is in `hosts/i2/` in this repo, not in the restore point.
