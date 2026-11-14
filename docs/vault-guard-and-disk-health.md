# The vault's path, and the disk behind it

Two guards added on **2026-11-14**, after the vault outage in
`vault-replica-restore.md`: one that makes an absent drive *audible* instead of looking
like an empty directory, and one that watches the disk's own health.

## 1. A sentinel, because `mountpoint` cannot tell "absent" from "fine"

The outage's core failure was not the USB drive. It was that `/srv/vault` kept existing
after the drive left: `srv-vault.mount` went inactive, the directory stayed, `vault-ro`
had already bound it, nginx answered `200` on `:8080`, and the NFS export was still
listed. `ls /srv/vault` printed nothing and exited 0. Every answer was true and every
answer was empty.

`nofail` in fstab is right — a boot must not block on a USB disk — and it is exactly
what creates this shape. So the estate asks a question a path cannot fake:

| Piece | What it does |
|---|---|
| `/srv/vault/.vault-id` | a one-line file **on the filesystem** (`innotel-vault-v1`). Present only when the vault is mounted. |
| `scripts/vault-ro-mount.sh` | refuses to publish `/srv/vault-ro` when the mount lacks the sentinel, and removes a stale ro view when the vault is gone. |
| `scripts/check-vault-mount.py` | half-hourly estate check: is the path a mount, is it *our* filesystem, is the ro view a mount, is it read-only. |

The ro view is not cosmetic: `ontrak`'s `vault` device points at it, so publishing an
empty directory under that name is how guests silently read zero files.

The check is wired into `estate-checks.service` (after `check-host-disk`, same
`TEXTFILE_DIR` contract, `check="vault_mount"`), so the existing
`EstateCheckCouldNotRun` and failure alerts cover it with no new rule.

### Verification

| Case | Result |
|---|---|
| live estate | `ok /srv/vault on /dev/sdc (ext4, 10 entries); /srv/vault-ro read-only`, rc 0 |
| textfile | `innotel_estate_check_last_status{check="vault_mount"} 1` |
| full unit after wiring | `Result=success`, `ExecMainStatus=0` |
| scratch bind, **no** sentinel | `ERROR: … carries no 'innotel-vault-v1' sentinel`, refuses the ro view, **rc 1** |
| same bind, sentinel added | `read-only view ready: /tmp/vt/ro (ro,nosuid,nodev,…)`, **rc 0** |
| real `/srv/vault-ro` afterwards | still `/dev/sdc ro,relatime` — the test never touched it |

The guard was exercised on a scratch bind mount rather than by unmounting the real
vault, because the second option is the outage.

## 2. The disk's own health

`smartctl` was not installed on `i1`, so the only evidence about the drive was ext4's.
It is installed now (`smartmontools 7.5`), and `scripts/vault-disk-health.sh` reads it
hourly-equivalent: daily at 06:20 and ten minutes after boot, with `OnFailure` wired to
`estate-notify@%n` like the other host timers.

It addresses the disk by **`LABEL=VAULT`**, never `/dev/sdX`: the kernel's letter for
this drive changed during the outage, which is the whole lesson. It can exit 2 — and
does — when `smartctl` is missing or the label is absent, so "cannot judge" never reads
as a pass.

What it fails on: overall health not `PASSED`; any reallocated, pending, offline
uncorrectable or reported-uncorrectable sector; a normalized attribute already past the
vendor's threshold. What it warns on: `UDMA_CRC_Error_Count` (the cable or the bridge,
not the platters) and temperature above `TEMP_WARN` (50 °C; critical at 55 °C).
Thresholds are environment-overridable so each branch can be exercised — the forced run
(`TEMP_WARN=30 TEMP_CRIT=40`) reports the same disk and exits 1.

### What the disk says

| Reading | Value |
|---|---|
| model / serial | `ST500LT012-1DG142` / `S3P00014` (500 GB, 5400 rpm, 2.5") |
| enclosure | `ASM1153E` SATA bridge over USB (`-d sat`) |
| overall health | **PASSED** |
| power-on hours / cycles | 5510 h / 67 |
| reallocated / pending / uncorrectable | **0 / 0 / 0** |
| UDMA CRC errors | **0** |
| temperature | 35 °C now; **worst 43 °C** against a 45 °C threshold (attribute 190 shows `In_the_past`) |

So the platters are not what failed, and the SATA link through the enclosure has never
recorded a CRC error. The disk has none of the SMART evidence that precedes a silent
death, and the drop on 2026-11-14 remains a **USB/enclosure or power event** — which
SMART cannot see and this check therefore does not claim to.

That 43 °C reading is worth keeping: a 2.5" drive that has already sat two degrees under
its own threshold in a fanless enclosure will do it again in summer. It is a warning
line, not a failure, and the log keeps it.
