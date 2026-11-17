# Media apps died with SIGSEGV every few seconds — AppArmor, not memory

2026-11-17. Sonarr on `monarch` (i2) restarted 443 times, never once logging
`Application is shutting down`, so each incarnation was killed hard. Radarr,
Prowlarr and Lidarr were cycling more slowly in the same way; Bazarr, which is
Python, had been up 16 hours.

## What was ruled out

| Suspect | Why it was not the cause |
|---|---|
| Backup writing a corrupt zip | Pushing `<BackupInterval>` to 36 500 days reproduced the crash identically. The 98 invalid zips were a *symptom* of dying mid-run, not the trigger. |
| Stale `sonarr.pid` / single-instance guard | Real, and fixed separately (refusals went to 0), but restarts continued afterwards. |
| Memory pressure / OOM | `memory.events` for the container read `oom_kill 0`; RSS was ~120 MB against a 10 GiB instance. The instance counter (`oom_kill 5333`) was historical, not moving. |
| A managed .NET exception | `docker logs` held 443 `Application started` and zero shutdown lines; no exception was ever logged. |

The decisive evidence was s6's own death tally:

```
docker exec sonarr s6-svdt /run/service/service-sonarr
@400000006afcdeb42f2f05a6 signal SIGSEGV
@400000006afcded5342bbb67 signal SIGSEGV
... twelve in a row
```

and the **host** kernel audit ring, which the container could not see:

```
apparmor="DENIED" operation="signal" profile="docker-default"
  comm="Sonarr" requested_mask="send" denied_mask="send" signal=abrt
  peer="docker-default//&unconfined"
```

## The mechanism

`docker-default` grants `signal (send, receive) peer=docker-default`. Inside a
nested incus instance the peer label is *namespaced*
(`:incus-monarch_<var-lib-incus>:docker-default`, reached as
`docker-default//&unconfined`), so the peer match fails and the runtime's own
signal path — `createdump`, then `SIGABRT` — is denied. .NET dies by SIGSEGV
with its handler unable to run, which is also why no core dump was ever written
even though `/app/bin/createdump` is shipped.

## The isolation that proves it

Two probes on the same copy of `/config`, 150 s each, changing one thing:

| Probe | Change | Result |
|---|---|---|
| D | `--cap-add SYS_PTRACE` | 5 starts, 3 fresh `SIGSEGV` — still broken |
| E | `--security-opt apparmor=unconfined` | 1 start, up the full 150 s — clean |

The capability was irrelevant; the AppArmor relaxation was the whole difference.

## The fix

`security_opt: ["apparmor=unconfined"]` on the .NET media services in
`3-media/monarch/docker-compose.yml` (and mirrored into the generated
`groups/3-media.yml`). This is an explicit trade: those containers lose AppArmor
confinement because `docker-default` cannot match its own namespaced peer label
under incus. The alternative — a hand-written profile that allows
`signal peer=*` — would have to reproduce every other `docker-default` rule to
stay meaningful, so declaring the container unconfined is the honest form of the
same choice rather than a confinement that only looks enforced.

## The backup gap this exposed

Every Sonarr backup written on 2026-11-17 was rejected by `zipfile.ZipFile`
(98 files); the newest restorable backup was 2026-11-10. That writer could not
produce a good archive while the process was being killed mid-run, and it does
work again now: a triggered `Backup` command returned 201 and the new zip passes
`zipfile.testzip` with all 3 members intact. Disabling it was tried and does not
hold — Sonarr drops an unknown `<BackupInterval>` from `config.xml` when it
rewrites the file, so the interval lives elsewhere and the schedule is unchanged.

A backup nobody has ever verified is still not a backup, so
`scripts/backup-media-databases.py` adds an independent one: SQLite's online
backup API, `PRAGMA integrity_check` on the copy, gzip only after it passes, and
a keep-N prune, on `media-db-backup.timer` from i2.
