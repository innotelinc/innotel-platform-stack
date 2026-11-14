# `monarch` — keeping the media library honest

Two faults in one week of 2026-11-13/14 produced this page: folders that were recreated
with the wrong owner, and media files that no database claimed. Both were fixed, and both
have a guard or a rule now, because either can come back the next time someone deletes
something inside the container.

Measured on 2026-11-14. The move itself is `monarch-media-move.md`; capacity is
`hp-capacity-plan.md`.

## 1. The torrent folders must stay writable by uid 1000

Transmission runs as **PUID 1000** inside the `transmission` container, but the seeding
step recreates `/data/torrents/<category>` as **`root:root 755`** whenever the folders are
missing. The daemon then cannot write, and the WebUI reports `Permission denied (13)`
while the torrents sit at `0.00%`.

The fix is `chown -R 1000:1000` with mode **2775** (setgid, so new files inherit the
group). The guard keeps it that way:

| piece | path |
|---|---|
| script | `scripts/monarch-torrent-perms.sh` |
| service | `systemd/monarch-torrent-perms.service` (oneshot) |
| timer | `systemd/monarch-torrent-perms.timer` (`OnBootSec=2min`, then every 5 min) |

It runs the check **through `incus exec monarch`**, never against the dataset path: a
running container's rootfs is not reliably visible at
`tank/incus/containers/monarch/rootfs/...`, and the first version of the script assumed it
was and refused to report success. That refusal is the behaviour to keep.

Exit codes follow the estate convention: **0** every folder already correct, **1** repairs
were needed but did not stick, **2** could not run (container or tree absent). Only entries
whose owner or mode is wrong are touched, so a healthy run is a metadata walk and no writes.

Checked after installation: a deliberately broken folder (`root:root 2755`) was repaired to
`ubuntu:ubuntu 2775` by one run, `ExecMainStatus=0`, timer active.

## 2. Files that no database claims

Sonarr reported two `unmappedFolders` — `American Hostage` and `Lanterns` — and the media
tree held files for them that no series or movie record pointed at. On 2026-11-14 those
**15 files (37.11 G)** were moved out of the library, not deleted, into
`/data/stale-quarantine` inside the container — deliberately **outside `/data/media`**, so
they were excluded from the library copy and did not consume the destination.

They were purged on 2026-11-14 at the owner's instruction. `monarch`'s dataset went
**58.0 G → 41.5 G**; the space is now free on `tank`.

Rules that made this safe, and should hold next time:

- **Quarantine before deleting.** A move is reversible; `rm` is not, and this library has
  already lost ≈140 G to in-place deletion with no snapshots to recover from.
- **Only act on a database's own opinion.** Sonarr's `unmappedFolders` is evidence. A
  failed API read is not: Whisparr's `/api/v3` returns unparseable output on this estate, so
  its library was left alone — "0 files" there was a broken read, not an empty library.
  Music was excluded for the same reason.
- **Check for duplicates first.** There were none: every episode file had exactly one
  series, so nothing was removed as a duplicate.
- **There is no recycle bin to empty.** No Sonarr/Radarr/Whisparr instance on the estate has
  a recycle bin configured, so "trash" was already empty. Do not invent one to report
  progress against.

## 3. What the library looks like now

`/data/media` is served by `tank/media-downloads` (121 G, mode 2775, `1000:1000`), with
`movies`, `music`, `temp`, `tv` and `xxx` at the top level. `temp` is the only directory
that came from the old skeleton rather than the library copy.

The stack writes to it continuously, so the file count moves: it read **259** at cutover,
**264** twenty minutes later and **265** after the purge, because the *arr apps import as
they run. A count that grows is the permission fix working, not drift.

## The orphan watcher

`scripts/media-orphans.sh` asks **all four** owners what they claim and names everything
in the library that none of them does. It runs inside `monarch` — where the library is
`/data/media` and the APIs listen on 127.0.0.1 — driven from i1 by
`media-orphans.timer`: daily at 04:20 and 20 minutes after boot.

| library | owner | endpoint |
|---|---|---|
| `tv` | Sonarr | `/api/v3/series` |
| `movies` | Radarr | `/api/v3/movie` |
| `music` | Lidarr | `/api/v1/artist` — Lidarr never left API v1 |
| `xxx` | Whisparr | `/api/v3/series` |

Whisparr is the trap. `ghcr.io/hotio/whisparr:v2-develop` (2.2.0.404) is built on
**Sonarr**, not Radarr: it answers `series`/`episode` and returns **404 for
`/api/v3/movie`**, which is why its library was previously written off as "unparseable"
and left alone. `/api/v3/movie` still 404s today; `/api/v3/series` returns its three
records with `path` values under `/data/media/xxx`.

```bash
systemctl list-timers media-orphans.timer
journalctl -u media-orphans.service -n 20 --no-pager
```

Exit 0 means every entry holding media is claimed, 1 names unclaimed media, 2 means an
API could not be read. That last case is never reported as clean: a mistyped key or a
dead Sonarr shows up as `CANNOT RUN`, not as a tidy library. Verified by breaking
Radarr's key on purpose and watching it exit 2 instead of 0.

Only media counts as an orphan. A folder left behind by a purge usually still holds
`.nfo`, `.srt` or poster files, which Sonarr deliberately does not claim; those are
reported as `debris (no media, unclaimed)` and do not fail the check. Today that is
`American Hostage` (3 `.nfo`) and `Lanterns` (2 `.nfo`) — five files, 7 KB, left by the
quarantine purge. Removing them is optional and was not done here.

A failure is announced: `OnFailure=estate-notify@%n` puts the unit's verdict and the tail
of its journal on the estate's ntfy topic (`docs/estate-alerts-ntfy.md`). That claim is
new as of 2026-11-14 — the earlier text here said there was no channel at all, which was
wrong: ntfy runs in `proxy` on **i2** at `192.168.1.71:2586`, and the probe that "proved"
otherwise had asked `:8080`, where nginx answers.

What the widened run found on 2026-11-14, first time all four owners were asked:

| | |
|---|---|
| claimed paths | **60** — sonarr 24, radarr 23, lidarr 10, whisparr 3 |
| ORPHAN | `/data/media/music/Drake` — **1.4 G**, 43 files, no Lidarr artist claims it |
| ORPHAN | `/data/media/xxx/Brown Bunnies` — **16 G**, 16 files, no Whisparr series claims it |
| debris | `American Hostage` (3 `.nfo`), `Lanterns` (2 `.nfo`) — unchanged, 7 KB |

Both were real: exact-path matching against what the apps report. Neither was deleted.

### The decision: give each one an owner (2026-11-14)

Both folders were adoptable. Two assumptions had to be dropped first:

- Whisparr v2 models a **studio as a series**, so `Brown Bunnies` is a series record
  (`tvdbId 794`, network Bang Bros) and its files are already named
  `Brown Bunnies - <date> - <title> [WEBDL-1080p]`. The studio disambiguation that v3
  added is not required to claim these files.
- The earlier note here that `Brown Bunnies` was configured as a Whisparr **root folder**
  did not reproduce: the root-folder list reports one entry, `/data/media/xxx`. Nothing
  had to be removed; a root folder would not have been a claim anyway.

| folder | owner given | settings |
|---|---|---|
| `/data/media/music/Drake` | Lidarr artist **id 6**, `foreignArtistId 9fff2f8a-21e6-47de-a2b8-7f449929d43f` | `monitored: false`, `addOptions.monitor: none`, no missing-album search — three albums (HABIBTI, ICEMAN, MAID OF HONOUR) were already on disk as FLAC |
| `/data/media/xxx/Brown Bunnies` | Whisparr series **id 10** (`tvdbId 794`) | `monitored: false`, `addOptions.monitor: none`, no missing-episode search, then `RescanSeries` — 8 scene files, 16 G |

Nothing was renamed, moved or searched for; the folder path the app reports is the folder
that was already there, which is the only reason the claim matches exactly.

The re-run is the evidence that the library is honest now:

```
sonarr: 24   radarr: 23   lidarr: 11   whisparr: 4
claimed paths: 62
result: all 33 entries holding media are claimed; 2 leftover item(s) with no media
exit 0
```

The rule this leaves behind: an unclaimed folder gets an **owner**, and deletion is only
for debris that holds no media. Adopting costs one API call per folder and is reversible
(delete the artist/series record; the files are untouched). Deleting 17 G to make a check
green is not reversible.
