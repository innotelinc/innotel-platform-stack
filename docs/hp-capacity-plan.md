# HP box (`i1`) — disk and layout plan

Measured on **2026-11-12** over SSH while `atheniq` and `mail` were being moved onto
this host. The question this page answers: `i1` is now the single box for **LMS
(`atheniq`), NAS and mail**, plus the media container and the edge tenants it already
ran — what has to change so that combination does not stall?

Everything below is measured, not assumed. Where a number is missing it says so.

## What the host is

| | |
|---|---|
| CPU | Intel Core i3-8100 @ 3.60 GHz — **4 cores, 4 threads** |
| RAM | **15 279 MiB**, of which 13 078 used, 2 201 available at measurement; **swap 4 095 MiB, 2 915 used** |
| Disk | **one** `ST1000DM003-1SB1`, 931.5 G, **rotational (`ROTA=1`)** |
| Layout | `sda1` 1 G vfat `/boot/efi`, `sda2` 100 G ext4 `/`, `sda3` 2 G ext4 `/boot`, `sda4` 828.5 G `zfs_member` = pool `tank` |
| Spare | `sdc` 465.8 G (`ASM1153E` USB bridge), ext4, **not mounted, unused** |

There is **no second spindle**: the operating system, every container, every Docker
overlay, the Incus database and the media library all live on the same 7200 rpm disk.

## What fills it

```
tank                     796G used / 25.1G avail     93% capacity, 54% fragmented
  tank/incus             696G   (containers 671G)
    monarch              534G   <-- 67% of the entire pool, media
    workloads             40.8G
    ontrak                29.9G
    atheniq               25.3G  (LMS/CMS + Docker)
    atlas                 25.3G
    mail                   9.43G (Stalwart + webmail)
    nas                   586M
    others                ~6G
  tank/evac               31.7G  (/srv/evac — three of its directories are live binds)
  tank/vault              21.6G  (Vaultwarden/Linkwarden/Meilisearch)
```

`zpool list tank`: 824 G size, 773 G allocated, 50.6 G free, **frag 54 %, cap 93 %**,
compressratio 1.13×, `ONLINE`, last scrub 2026-11-08 clean.

### What the 534 G actually is (checked 2026-11-12)

`zfs list -o used,usedbydataset,usedbysnapshots,usedbychildren` on
`tank/incus/containers/monarch`: **534 G used, 534 G `usedbydataset`, 0 B
`usedbysnapshots`, 0 B `usedbychildren`, zero snapshots.** There is no snapshot pinning
this space and no nested dataset holding it — it is live files inside `monarch`'s rootfs.

`tank/media-downloads` is a **separate dataset of 160 K** that `monarch`'s `downloads`
device mounts at `/mnt/downloads`. It is empty, so the tempting assumption that the
media library is already split off the container pool is wrong: the split was created
and never used.

Consequence: reclaiming it means **copying 534 G to a target with 534 G free**. No such
target exists here today — `tank` has 24.8 G available (**31.2 G** on 2026-11-13 evening,
see the drift note), and `sdc` is 465.8 G, smaller than the library. That is why step 2 below is a purchase, not a maintenance task.

## Why it stalls — the measured evidence

This is not a theory about spinning disks. During the migration the host reported:

- **PSI `io` pressure `some avg10 = 99 %`, `full avg10 = 75 %`** — essentially every
  task was stalled on IO, three quarters of wall time with *no* progress at all.
- **`incusd` could not complete database transactions**: repeated
  `cowsql: Failed to get current raft members … i/o timeout`, and at the worst point
  `incus exec` failed outright with `Lost connection to the event listener … i/o timeout`.
  The control plane of the box becomes unusable while it is busy.
- **Container starts are divided into minutes, not seconds**: Docker inside a freshly
  started container sat in `activating` for over two minutes, and its twelve containers
  took roughly another two minutes to report healthy.
- **2.9 G of the 4 G swap is in use** with only 2.2 G RAM available.

A single spindle at **93 % capacity and 54 % fragmentation**, carrying ~700 000 small
files across fourteen containers plus Docker overlay layers, is the cause. At 93 % ZFS
can no longer allocate contiguously: every write becomes a seek, and the small-file
random IO (dqlite/raft, Docker overlay, package caches, mail indexes) starves the
sequential readers that media serving needs.

## Recommendation

The split that matters is **small-file random IO vs. bulk sequential IO**. Put them on
different devices and both get faster; leaving them together means every mail delivery
and every LMS request competes with a 534 G media library.

### Target layout

| Device | Role | Why |
|---|---|---|
| `sda` existing 1 TB HDD | `/` (100 G, unchanged) + **media pool** holding `monarch`'s library and NAS shares | Media is large, sequential and cold. A single spindle serves it well — once it is not also carrying 14 container roots. |
| **new 1 TB SATA SSD** | ZFS pool `tank` for **all containers** (`atheniq`, `mail`, `ontrak`, `atlas`, `workloads`, `nas`, `vault`, edge) | Container roots, Docker overlays, the dqlite database and mail indexes are exactly the workload a single HDD handles worst. ~170 G of container data fits on 1 TB with ~80 % free. |
| `sdc` 465.8 G USB | Backup / archive target only | Do not put live ZFS on a USB bridge: no SMART passthrough, and a cable nudge is a pool fault. |
| RAM | **32 GB** (2 × 16 GB DDR4; the i3-8100 supports 64 GB) | 2.2 G available with 2.9 G swapped out is a host that is already out of memory, not out of CPU (4 cores idle at the time). |

### Sequence, cheapest first

1. **`tank/evac` is not reclaimable as a whole — measured, not assumed.** The running
   `workloads` container bind-mounts three of its directories:
   `/srv/evac/dev-critical` → `/mnt/archive`, `/srv/evac/copilotkit-fs` →
   `/mnt/archive2`, `/srv/evac/copilotkit-docker` → `/mnt/archive3`. Destroying the
   dataset deletes live bind sources. Only `dev-fs`, `dev-extra` and `dev-incus` have
   no Incus consumer. All six directories are now sized — see the measurement table below.

   What can be said about them, from a metadata-only listing:

   | Directory | Depth-1 entries | Newest entry | Shape |
   |---|---|---|---|
   | `dev-critical` | 7 (`etc`, `root`, `opt`, `volumes`, `srv`, `home`, `data`) | **2026-11-12** | rootfs copy of a live host — the freshest thing under `/srv/evac` |
   | `copilotkit-fs` | 17 (`bin`, `lib`, `lib64`, `mnt`, `snap`, …) | 2026-08-26 | an April-era rootfs dump |
   | `dev-fs` | 19 (`bin`, `lib`, `lib64`, `mnt`, `media`, `lost+found`) | 2026-10-26 | an April-era rootfs dump — **measured: 118 MiB** |
   | `dev-extra` | 1 (`local`) | 2026-08-26 | leftover mount point |
   | `dev-incus` | 1 (`incus`) | 2026-11-12 | leftover mount point |

   **Measured 2026-11-13 — every directory, `du -sx --block-size=1` at `nice 19` /
   `ionice -c3`:**

   | Directory | Size | Incus consumer |
   |---|---|---|
   | `dev-critical` | **16.61 GiB** (17 834 354 176 B) | `workloads` → `/mnt/archive` |
   | `dev-incus` | **8.27 GiB** (8 879 727 616 B) | none — **retirement started 2026-11-13** |
   | `copilotkit-fs` | **5.06 GiB** (5 428 507 648 B) | `workloads` → `/mnt/archive2` |
   | `copilotkit-docker` | **1.76 GiB** (1 894 828 544 B), 120 482 files | `workloads` → `/mnt/archive3` |
   | `dev-fs` | **118 MiB** (123 891 200 B) | none |
   | `dev-extra` | **93.8 MiB** (98 385 408 B) | none |
   | **total** | **31.9 GiB** | — |

   The three directories with no consumer — `dev-incus`, `dev-fs`, `dev-extra` — total
   **8.5 GiB**. The three that are live bind sources total **23.4 GiB**, and none of that
   is reclaimable while `workloads` still mounts them. The total sits slightly above
   `tank/evac`'s 31.7 G because separate walks count shared inodes more than once.

   **`dev-incus` is being retired (2026-11-13).** It was a copy of an Incus var
   directory: its `containers/` entries were symlinks pointing at the *live*
   `/var/lib/incus` paths, and its storage pool held rootfs copies of `aidev`, `dev-smoke`
   and `omarchy-apple` — none of which exist as live containers. Checked before deleting:
   no container config referenced it (`workloads` has three evac binds, none of them this
   one), nothing in `/etc/fstab`, systemd or `/etc/incus`, and no process cwd or root
   pointed at it. A manifest of its top three levels is kept at
   `i1:/root/dev-incus-manifest.txt`.

   Removal runs detached (`setsid nohup rm -rf`) at `nice 19` / `ionice -c3`, because it
   is hundreds of thousands of small files on a 93 %-full spindle: it freed roughly 0.4 G
   in the first quarter hour, so `tank/evac` will fall gradually rather than at once. Watch
   it with `pgrep -f 'rm -rf /srv/evac/dev-incus'` and `zfs list tank/evac`; when it
   finishes, `tank/evac` should settle near **23.4 G**.

   Why the first attempts reported nothing: `du` had to fault cold metadata off a spindle
   already at 79–82 % IO pressure and was killed at the 300 s budget. The warmer second
   pass finished `copilotkit-docker` in **1 second** and `copilotkit-fs` in 22 s — the
   directories were never too large to measure, only too cold to measure quickly. A
   single `du -s /srv/evac` still exceeds 240 s because the parent re-walks all 120 482
   files of `copilotkit-docker` plus everything under `dev-critical`; measure this tree one
   directory at a time.

   **Space drift, same evening.** `tank` showed 20.4 G available at the start of this
   measurement and **31.2 G** by the end of it: the pool *gained* space during the day
   rather than losing it. `tank/incus/containers/workloads` fell from 44.1 G to 30.4 G
   between two readings, and nothing on the host explains that locally — there is no prune
   timer or cron, `journalctl` since midnight holds no prune or reclaim record, and the
   only file over 100 M written since 2026-11-12 is a 141 M Incus image. Read the day's
   swings in available space as a busy pool being measured at different moments, not as
   consumption that needs chasing.

   **Nothing here is safe to delete on the strength of this audit.** `dev-critical` was
   written the day of the migration: it is an evacuation copy of a host that was still
   being drained, and `dev-incus` was touched the same day. Only the owner can say
   whether those copies are the backup of record.
2. **Move `monarch`'s 534 G media library onto its own pool** on the existing HDD.
   This is the single biggest win: it returns 534 G to the container pool *and* stops
   media streaming from colliding with mail/LMS IO. **It cannot be done on this host
   today** for the space reason above; it needs the destination first.
3. **Add the 1 TB SSD for `tank`** and migrate the containers onto it. Verify with the
   numbers below rather than by feel: PSI `io full` should sit near zero at idle, and a
   stopped container should start in seconds.
4. **32 GB of RAM** once the disk work is done, sized from the post-move measurement.

### If only one purchase is possible

Buy the **1 TB SSD** and leave media where it is. Container starts, the Incus database,
mail indexing and LMS request latency all stop competing with the media library, which
is the failure that actually takes the box down (`incusd` unable to open a transaction).
The pool stays tight at 93 % until media moves, and `tank/evac` cannot fund that move:
its largest consumers are live (`workloads`' three archive binds), so the 31.7 G stays
where it is until those archives are retired.

Against a `special` vdev instead of a separate pool: a single unmirrored special vdev
loses the whole pool when that SSD dies, and it does not fix capacity (still 93 %).

## Acceptance — what "fixed" looks like

Re-measure these three after the change; do not accept the migration on the basis that
it copied:

```bash
cat /proc/pressure/io                 # io some/full avg10 near 0 at idle
zpool list tank                       # cap below 80 %, frag below 30 %
incus start <stopped-container> && time incus info <name> | grep Status
```

The estate convention applies: an unreachable host or an unreadable metric is exit 2,
never a pass.

## Not changed by this page

This is a recommendation. No disk has been purchased, no pool resized, no container
moved off `i1`. The moves that *were* made on 2026-11-12 (`atheniq` and `mail` onto this
host) are recorded in `container-placement.md`.
