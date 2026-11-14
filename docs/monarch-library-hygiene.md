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
