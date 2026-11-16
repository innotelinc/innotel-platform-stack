# i1 — the concrete fix: 1 TB SATA SSD for `tank`, and 32 GB RAM

Measured **2026-11-16** on `192.168.1.51`. This is the buy list and the install
sequence for the two changes `hp-capacity-plan.md` identified but left unpriced and
unprocedured. Everything in "What the host is" is read from the machine, not assumed.

## What the host is

| | |
|---|---|
| Chassis | **HP Slim Desktop 290-p0xxx**, board `843F`, serial `2MO01144CH` |
| CPU | Intel Core i3-8100, 4C/4T, 3.6 GHz |
| RAM | `dmidecode -t 16`: **Maximum Capacity 32 GB**, **Number Of Devices 2** — both slots populated, DDR4, reported at 2133 MT/s |
| Storage | `sda` **ST1000DM003-1SB1** 931.5 G SATA **rotational**; `sdb` 465.8 G **USB** (ASM1153E); `sr0` **hp HLDS DVDRW** on SATA |
| Slots | **no NVMe device and no NVMe slot** — `lspci` shows only `Intel Cannon Lake PCH SATA AHCI Controller`, 6 controller ports |
| NIC | **Realtek RTL8111/8168** 1 GbE |
| Pool | `tank` 824 G, **386 G alloc, 438 G free, 46 % cap, 38 % frag** |
| Swap | `/swap.img` 4 G, 576 MB used |

Two facts drive the whole plan:

1. **There is no M.2 slot.** `hp-capacity-plan.md` says "new 1 TB SATA SSD", which is
   right for the wrong-of-two reasons — SATA is the only option here, not a preference.
2. **A DVDRW is occupying a SATA port and its power connector.** The 290 Slim has one
   3.5" bay and one slim ODD bay. Removing the optical drive frees both, which is where
   the SSD goes. No adapter, no new cable.

## Buy list

| # | Part | Why this one | Nov 2026 price |
|---|---|---|---|
| 1 | **1 TB 2.5" SATA SSD with DRAM cache** — Crucial MX500 1TB or Samsung 870 EVO 1TB | DRAM matters here: `tank` carries ~700 000 small files and Docker overlays, exactly the workload a DRAM-less drive stalls on once full. | **$195–320** new (`BX500 1TB` ≈ $195, `870 EVO 1TB` ≈ $320 on 2026-11-07); cheapest new non-DRAM `Kingston A400 960GB` ≈ **$134** |
| 2 | **2 × 16 GB DDR4 UDIMM, 288-pin, 2400 or 2666** | `dmidecode` caps the board at **32 GB across 2 slots**, so this is the ceiling, not a choice. 2666 is fine — the board downclocks to what the i3-8100 runs. | **$150–300 per module**; DDR4 rose sharply through 2026, and one tracked 16 GB UDIMM listing quotes $582 on back-order, so price this on the day and treat anything above ~$300/module as a bad deal |
| — | *(not needed)* 2.5"→3.5" adapter | The ODD bay takes a 2.5" drive directly. | $0 |

**Total ≈ $350–900**, dominated by the RAM, and the RAM is the volatile half. Verify both
prices before ordering; the SSD line item has moved 2–4 % in a month while DDR4 has moved
by multiples since 2025.

### Budget reality check

`hp-capacity-plan.md` says: *"If only one purchase is possible, buy the 1 TB SSD."* That
still holds, and it is the cheaper half. The SSD fixes the failure that actually takes the
box down — `incusd` unable to open a transaction, container starts measured in minutes —
because it moves **small-file random I/O** off the spindle. The RAM halves the ARC
pressure (see `estate-rebalance-2026-11-15.md` on the 2→4 GiB cap) but cannot fix storage.

## Install sequence

### 0. Schedule it after the media migration finishes

`/var/log/media-sync.log` on `i1` must contain `SYNC_EXIT=0`. The attach below resilvers
the pool while that copy is still reading the same spindle; running both turns a 20-minute
resilver into an hour and re-creates the stall this work just removed.

```bash
grep SYNC_EXIT /var/log/media-sync.log      # must be SYNC_EXIT=0
```

### 1. Fit the SSD

Power off. Remove the slim ODD (one screw, slide out). Fit the 2.5" SSD in the ODD bay,
reuse the ODD's SATA data cable and power connector. Boot.

```bash
lsblk -o NAME,SIZE,TRAN,ROTA,MODEL          # new disk, TRAN=sata, ROTA=0
ls -l /dev/disk/by-id/ | grep -i <model>    # capture the by-id path — use it below
```

### 2. Partition it to match `sda4` exactly

`sda4` is 828.5 G. The mirror must not be smaller, and making it larger gains nothing —
the pool cannot exceed the vdev it mirrors.

```bash
SSD=/dev/disk/by-id/<new-ssd>
sgdisk --zap-all "$SSD"
sgdisk -n 1:0:+828.5G -t 1:bf00 "$SSD"      # bf00 = Solaris/ZFS
partprobe "$SSD"; lsblk "$SSD"
```

### 3. Attach as a mirror, not a copy

This is the whole trick: `attach` streams the existing pool onto the SSD **while it keeps
serving**, and there is no moment where the data exists in only one place. No `send/recv`,
no downtime, no window where a failed step loses the pool.

```bash
SSD_PART=${SSD}-part1
zpool attach tank /dev/sda4 "$SSD_PART"
watch -n 5 'zpool status tank'              # wait for "resilvered ... with 0 errors"
```

While it resilvers, expect PSI to rise. That is the copy, and it is bounded.

### 4. Verify before removing the HDD

```bash
zpool status -x                             # "all pools are healthy"
zfs list tank                               # 386 G still allocated, 438 G free
zpool scrub tank && zpool status tank       # a clean scrub is the acceptance test
incus list --format csv -c n,s              # all 15 present, same states as before
```

Do not skip the scrub. A mirror that has not been read back end-to-end has not been proven.

### 5. Detach the HDD, then give it to media

```bash
zpool detach tank /dev/sda4                 # pool now lives on the SSD alone
zpool create -o ashift=12 mediapool /dev/disk/by-id/<hdd>
```

`tank` is now single-device on the SSD — the same redundancy level it had before
(none), with none of the seek latency. If redundancy is wanted later, a second SSD can be
attached as a mirror.

Then move the library and repoint the device, following `monarch-media-move.md` —
verify by byte total, file count and two checksums before deleting the originals.

### 6. RAM

Power off, fit the two 16 GB modules, boot.

```bash
dmidecode -t 17 | grep -E 'Size|Speed'      # expect 2 × 16 GB
```

Only then raise the ARC cap again — 4 GiB was sized for 15 GB of RAM, and on 32 GB the
useful figure is 8 GiB:

```bash
echo 'options zfs zfs_arc_max=8589934592' > /etc/modprobe.d/zfs.conf
echo 8589934592 > /sys/module/zfs/parameters/zfs_arc_max   # runtime, no reboot
```

## Acceptance

The same three measurements `hp-capacity-plan.md` demanded, taken **after** the change and
compared to these readings from before it:

| Metric | Before (2026-11-16 01:58) | Target |
|---|---|---|
| `io some avg10` at idle | 36.5 % | **< 5 %** |
| `io full avg10` at idle | 34.2 % | **< 2 %** |
| load (1 min) | 1.5 (idle, migration running) | **< 1.0** |
| `zpool list tank` frag | 38 % | **< 30 %** |
| stopped container start | minutes under load | **seconds** |

An unreadable metric is exit 2, never a pass.

## Not changed by this page

Nothing has been purchased and nothing has been installed. The pool is still on the
spindle at 46 % capacity, the ARC is still 4 GiB, and the media migration is still
running.
