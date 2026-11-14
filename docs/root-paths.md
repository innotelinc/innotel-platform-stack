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

## Still in `/root`, deliberately

| path | why it stays |
|---|---|
| `/root/cerulean-backup/authentik/` — `TEXTFILE_DIR` in `systemd/estate-checks.service`, the metrics textfile mount in `extensions/monitoring/docker-compose.host.yml`, and the default `OMNIROUTE_SELFHEAL_METRICS` in `scripts/omniroute-selfheal` | it is the directory node-exporter's textfile collector is *mounted* on, on the host, so the three files are one fact. Moving it in the repo alone would stop every series from being scraped and nothing would say so. It moves as one change: the env file, the compose mount, and a re-apply on the host. Until then the path is a dependency, not a preference — a `/root` cleanup that hits it blinds the estate's own metrics. |
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
