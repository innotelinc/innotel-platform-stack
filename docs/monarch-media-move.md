# Moving `monarch`'s media library off the container pool

Written 2026-11-12/13, after a direct inspection of the stopped container's dataset.
This page exists because the obvious move — "the media is already its own dataset, just
repoint the device" — is **wrong on this host**, and acting on it would lose the library.

## Measured state

| Fact | Value | How it was established |
|---|---|---|
| `tank/incus/containers/monarch` used | **534 G** | `zfs list -o used` |
| of which `usedbydataset` | **534 G** | `zfs list -o usedbydataset` |
| of which `usedbysnapshots` | **0 B** | `zfs list -o usedbysnapshots` |
| child datasets | **none** | `zfs list -r -d 1` |
| snapshots | **0** | `zfs list -t snapshot -r` |
| the library lives at | **`/data/media` inside the rootfs** | read-only mount of the dataset |
| `/data/tv` | 2.0 K (empty) | `du -sh` |
| `/data/downloads` | 1.0 K (empty) | `du -sh` |
| `/mnt/downloads` → `/tank/media-downloads` | **160 K, empty** | device config + `du -sh` |

The one disk device `monarch` has is the downloads split:

```yaml
devices:
  downloads:
    path: /mnt/downloads
    source: /tank/media-downloads
    type: disk
```

So the split exists, points at an empty dataset, and the 534 G library is **not** in it.
`/data/media` is an ordinary directory in the container's rootfs.

### Consequence

There is no dataset to `zfs rename` and no snapshot to send: the 534 G is plain files
inside a dataset that also contains the container's OS. Any move is a **data copy**, and
it needs a destination with **534 G free plus headroom** (call it 600 G).

Today no such destination exists on the host: `tank` has **24.8 G** available and `sdc`
is 465.8 G — smaller than the library and reserved as a backup target. **Do not start a
copy before the destination exists**; filling `tank` to 100 % takes mail and the LMS down
with it.

## Pre-flight (all must be true)

1. Destination dataset exists on a disk that is **not** `tank`, with ≥ 600 G free:
   `zfs list -o name,avail <pool>/media`.
2. `monarch` is stopped (`boot.autostart=false` is already set on this host).
3. Consumers inside `monarch` (media server, *arr apps) reference `/data/media`; confirm
   the exact paths before changing anything:
   `incus exec monarch -- grep -rn "/data/media" /etc 2>/dev/null | head`.
4. A free-space floor on `tank` of ~15 % is agreed: the copy must not be run while
   `tank` is at 93 % — copy *from* the saturated pool *to* the new disk, never the reverse.
5. Run the copy under `tmux`/`screen`: at HDD speeds 534 G is a multi-hour operation, and
   an interrupted `rsync` is resumed, not restarted.

## Procedure

Names used below: new pool `mediapool`, dataset `mediapool/media`, temporary mount
`/mnt/media-new`. Adjust the pool name to the disk actually installed.

```bash
# 1. Stop the consumer, so nothing writes during the copy.
incus stop monarch

# 2. Create the destination and attach it to the (stopped) container as a staging mount.
zfs create mediapool/media
incus config device add monarch media_stage disk \
  source=mediapool/media path=/mnt/media-new

# 3. Start it and copy. -aH preserves modes, owners and hardlinks; --info=progress2
#    gives one live progress line. Run inside tmux.
incus start monarch
incus exec monarch -- tmux new -d -s move \
  'rsync -aH --info=progress2 /data/media/ /mnt/media-new/ > /root/media-move.log 2>&1'

# 4. Watch it (this is the only honest progress signal).
incus exec monarch -- tail -f /root/media-move.log
```

### Verify before deleting anything

```bash
# Byte totals must match exactly.
incus exec monarch -- sh -c 'du -sb /data/media | cut -f1'
incus exec monarch -- sh -c 'du -sb /mnt/media-new | cut -f1'

# File counts must match.
incus exec monarch -- sh -c 'find /data/media -type f | wc -l'
incus exec monarch -- sh -c 'find /mnt/media-new -type f | wc -l'

# Spot-check a sample of large files by checksum.
incus exec monarch -- sh -c \
  'cd /data/media && find . -type f -size +100M | head -20 | xargs -r md5sum' > /tmp/src.md5
incus exec monarch -- sh -c \
  'cd /mnt/media-new && md5sum -c /tmp/src.md5'
```

Only when all three agree: proceed.

### Cut over

```bash
# 5. Stop. Delete the ORIGINALS while they are still visible at their real path
#    (the staging bind is elsewhere, so /data/media is still the rootfs copy here).
incus stop monarch
incus start monarch
incus exec monarch -- rm -rf /data/media/*
incus exec monarch -- sh -c 'ls -A /data/media | wc -l'   # must be 0
incus stop monarch

# 6. Repoint the device at the real path and drop the staging mount.
incus config device remove monarch media_stage
incus config device add monarch media disk source=mediapool/media path=/data/media

# 7. Start and confirm the library is served from the new disk.
incus start monarch
incus exec monarch -- sh -c 'find /data/media -type f | wc -l'
```

Reclaim is confirmed outside the container:

```bash
zfs list -o name,used,usedbydataset tank/incus/containers/monarch   # expect ~ OS only
zpool list tank                                                     # expect ~559 G free
```

## Rollback

Every step after the copy is configuration, not data:

- **Before deleting the originals**: `incus config device remove monarch media` and
  `zfs destroy mediapool/media` — the container is untouched and still boots on the
  old path.
- **After deleting, before verification of the served library**: re-add the device with
  `path=/mnt/media-new`, which still holds the verified copy.
- **Do not** delete `mediapool/media` while it is the only copy.

If the copy is aborted mid-way, nothing needs undoing: `tank` was only read, and
`mediapool/media` is a partial copy that a re-run of the same `rsync` resumes.

## Expected result

| | Before | After |
|---|---|---|
| `tank/incus/containers/monarch` | 534 G | ~ OS + Docker only |
| `tank` available | 24.8 G (93 % cap) | ~559 G (~28 % cap) |
| media IO path | same spindle as mail/LMS/Docker | dedicated disk |

That is the change that stops media streaming from colliding with mail and LMS IO; the
capacity plan's disk purchase (`hp-capacity-plan.md`) is the precondition for it.
