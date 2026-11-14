# What still lives in `/root`, and what may not

On **2026-10-25** a `/root` cleanup deleted `vault-replica-sync.sh` and its log. The unit
that exec'd it did not fail at *load*, it failed at **exec** — every night for three weeks,
`status=203/EXEC` — while the timer, the unit and the directory all still existed. Nothing
was lost (the replica held the last good copy) but the mirror had silently stopped
(`vault-replica-restore.md`).

The rule that came out of it: **anything a unit execs, or a timer depends on, lives in
`/usr/local/sbin`, `/usr/local/bin`, or this repository — never in `/root`.**
`/root` is for one-off operator state and for dated evidence, which a cleanup is allowed to
take and which nothing schedules itself against.

## Moved out

| before | after | why |
|---|---|---|
| `/root/vault-replica-sync.sh` | `/usr/local/sbin/vault-replica-sync` | executed nightly by `vault-replica-sync.service` |
| `/root/vault-replica-sync.log` | `/var/log/vault-replica-sync.log` | the log of a scheduled job belongs where logs are read |
| `/root/backup-proxy.sh` | `/usr/local/sbin/backup-proxy.sh` | executed weekly by `proxy-backup.service` on i2 |
| `/root/restore-proxy.sh` | `/usr/local/sbin/restore-proxy.sh` | the other half of that pair; run by hand, installed beside it |
| `/root/stalwart-oidc-{redirect.sh,discovery-proxy.py}` and the two units | installed from the copy's own directory | `scripts/stalwart-oidc-discovery-setup.sh` installed from `/root`, so the pieces it installs had to be *put* there first — it now resolves them beside itself or in the repo's `systemd/`, and refuses to start half-armed |
| `/root/move-proxy-i4-to-i2.log` | `/var/log/move-proxy-i4-to-i2.log` | a run log, not operator state |
| `/root/cerulean-backup/authentik/` — `TEXTFILE_DIR` in `systemd/estate-checks.service`, the metrics textfile mount in `extensions/monitoring/docker-compose.host.yml`, the default `OMNIROUTE_SELFHEAL_METRICS` in `scripts/omniroute-selfheal` | `/var/lib/node_exporter/textfile` | the metrics directory every estate check writes into; it only sat under `/root` because the Authentik dump beside it did |

### The textfile directory: why it moved last, and the order of the rest

The directory was in `/root` because the *dump* was: the Authentik database backup writes
`status.prom` into the same place, and node-exporter's textfile collector mounts one
directory, so the checks and the dump had to agree. Moving it in this repository alone
would have left the writer pointing at `/root` and the reader at `/var/lib`, and the
failure is the silent one: `backup.yml` keys on the *presence* of
`cerulean_authentik_backup_*`, so those series simply stop and no rule fires, while every
`estate-check-*.prom` lands where nothing scrapes it and `EstateCheckAbsent` fires for all
of them a day later.

So it is one change in four places, and the writer comes **first**:

1. **Cerulean's dump** (`1-primary/cerulean/scripts/authentik-db-backup.sh`) writes
   `status.prom` into `/var/lib/node_exporter/textfile` on `.71`.
2. **The repo default**, which is now `/var/lib/node_exporter/textfile` in
   `systemd/estate-checks.service` and in `scripts/omniroute-selfheal`.
3. **The mount**: `TEXTFILE_DIR` in the host's `.env.host` for the metrics stack. Leaving
   it unset is correct now — the `volumes:` line falls back to the same path — and setting
   it to the old value is how the split above comes back.
4. **The host**: `install -d -m 0755 /var/lib/node_exporter/textfile`, move any `.prom`
   still in `/root/cerulean-backup/authentik` across, then `docker compose -p
   innotel-metrics -f docker-compose.host.yml --env-file .env.host up -d node-exporter`.

What proves it is not a directory listing — a moved directory and a lost one look the
same in `ls`. It is `scripts/textfile-series-snapshot.sh`, which takes the series (name and
every label) before and after and fails if any of them stopped existing:

```bash
./scripts/textfile-series-snapshot.sh --snapshot /tmp/before.txt      # before step 4
./scripts/textfile-series-snapshot.sh --snapshot /tmp/after.txt
./scripts/textfile-series-snapshot.sh --diff /tmp/before.txt /tmp/after.txt
```

The ordered apply on `.71`, including the dump writer that is not in this repository and
the two rule sets that would go quiet, is `docs/textfile-dir-move.md`.

Until step 1 is applied on `.71`, a host that takes this default runs its checks into a
directory nothing shows — which is why the unit's comment names the dependency instead of
assuming it.

## Still in `/root`, deliberately

| path | why it stays |
|---|---|
| `/root/.ssh/config`, `/root/.ssh/container-address` (on the edge) | root's ssh config is root's. `scripts/trust-estate-hosts.py` installs the marked block and the key and its `--check` reports drift; `estate-checks` cannot reach a host that is not named there. |
| `/root/registry.env` — inside **`regcache`**, read by `scripts/regcache-credential-check.sh` and written by `scripts/rotate-regcache-pat.sh` | it is the *container's* root, not the host's: `/root` there is `/docker/appdata/regcache`, a docker-managed path. No host cleanup touches it. |
| `/root/bulwark-src-build-1.18.0`, `/root/loomix-build`, `/root/mail-backups` and the one-shot build helpers that name them (`update-mail-stack.sh`, `show-bulwark-conflicts.sh`, `probe-bulwark-rebase.sh`, `port-bulwark-admin-pages.sh`, `resolve-bulwark-conflicts.py`) | build scratch and pre-upgrade backups, rebuildable from the repos and printed in each script's own failure messages. Moving them would orphan whatever sits there today for no gain; they are not exec'd by a unit. |
| dated evidence written by an incident: `incident-20260927-git/`, `authentik-backup-20261102.json`, the provider dumps in `mail-sso-directory-2026-11-06.md`, `RECOVERY-net-bridge.md` on i4, `ips-pre-git-20261026.tar.gz` | evidence should outlive the change that produced it, and every one of these is named in the doc that cites it. Nothing schedules itself against them. |
| `hosts/i2/fix-br-netfilter.sh`'s host copy | the file is a **run-in-place** repair (`/root/i2-br-netfilter-fix.sh`), and the repo copy is authoritative — `hosts/i2/README.md` says to copy it over, never to edit the host copy. |

## Checking

```bash
# what units would exec from /root, or document out of it (should print nothing)
grep -rnE 'Exec(Start|Stop)=.*/root/|Documentation=file:///root/' systemd hosts

# every /root reference the repo still carries, with context
grep -rn '/root/' --include='*.service' --include='*.timer' --include='*.sh' .
```

The first grep is the invariant. The second is an inventory, and a hit is only a problem if
the thing it names is executed by a unit — everything else belongs in the table above, with
its reason.
