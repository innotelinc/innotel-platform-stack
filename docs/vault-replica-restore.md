# The vault mirror that was not running, and the drive that vanished

Two unrelated failures made the artifact vault look healthy on 2026-11-14 while it was
being served from a storage stack that had partly fallen over.

## 1. The mirror had not run since 2026-10-24

`vault-replica-sync.timer` fired nightly and `vault-replica-sync.service` failed on every
run, with:

```
vault-replica-sync.service: Unable to locate executable '/root/vault-replica-sync.sh'
status=203/EXEC
```

First failure **2026-10-28 03:21**, last successful mirror **2026-10-24 03:25**
(`rsync rc=0; replica now 22G; vault used 28351780K`). Root cause: the script lived in
`/root`, and a `/root` cleanup on 2026-10-25 removed it along with its log. The unit still
existed, the timer still existed, and systemd dutifully failed every night — but fail-fast
at exec looks identical to "nothing to do" when nobody reads the journal, and at that point
the estate had no notification channel either (see `estate-alerts-ntfy.md`).

Nothing was lost. The replica on `tank/vault` (`/srv/vault-replica`, 22 G) kept the last
good copy; the nightly job simply never touched it again.

Fixes, both about location rather than logic:

| before | after |
|---|---|
| `/root/vault-replica-sync.sh` | `/usr/local/sbin/vault-replica-sync` (installed from `scripts/vault-replica-sync.sh`) |
| `/root/vault-replica-sync.log` | `/var/log/vault-replica-sync.log` |
| unit only on the host | `systemd/vault-replica-sync.{service,timer}` in this repo |

## 2. The USB drive dropped, came back as a different device, and left a dead mount

`/srv/vault` was an empty directory: `srv-vault.mount` was **inactive**, so the nginx index
and the NFS export both pointed at nothing. `mount` still listed

```
/dev/sdb on /srv/vault-ro type ext4 (ro,relatime,shutdown)
```

and `ls /srv/vault-ro` returned `Input/output error`. The `shutdown` flag is ext4's
emergency state after I/O errors, and `/dev/sdb` no longer existed in `lsblk`: the drive had
re-enumerated as **`/dev/sdc`** (same `LABEL=VAULT`, 465.8 G). The dead mount was a leftover
of the old device node.

This is why nothing may pin `/dev/sdX` for the vault — `fstab` goes through
`LABEL=VAULT`, and the recovery below is just mounting that label again.

Recovery, in order, with the checks that were actually run:

| step | result |
|---|---|
| `umount -l /srv/vault-ro` (dead mount) | rc 0 |
| `mount -t ext4 -o ro,noload /dev/sdc /mnt/vaultcheck` | read ok — `catalog golden isos lost+found media secrets state templates virtio`, 28 G |
| `fsck.ext4 -n /dev/sdc` (never modifies) | no corruption: 1916/30531584 inodes, 9285281/122096646 blocks; only cosmetic `extent tree could be narrower` and a clean `orphan_present` note |
| `systemctl start srv-vault.mount` (LABEL, runs fsck) | active, `mountpoint: yes`, `/dev/sdc 458G 28G used 431G free` |
| `systemctl start vault-ro.service` | active; `touch /srv/vault-ro/.write-test` → `Read-only file system` |
| `/usr/local/sbin/vault-replica-sync` | **rc 0**, replica 22 G, vault used 28351788K |
| file counts | vault **1359** = replica **1359** |
| nginx index on `:8080` | `http=200` (the vault is being served again) |
| `exportfs -v` | `/srv/vault` exported rw to 192.168.1.52 as before |
| timer | active, next run 2026-11-15 03:23 |

Verification of the guard is unchanged and still holds: with `/srv/vault` unmounted the
script logs `SKIP: /srv/vault is not a mountpoint (drive absent) — replica untouched` and
exits 0. That guard was never exercised during the outage — the script was not there to
run — which is precisely the argument for keeping the code in `/usr/local/sbin` and in git,
where a `/root` cleanup cannot reach it.

## What this says about the estate

Three separate signals were green while the vault was half-broken: the timer existed, the
unit was "loaded", and `/srv/vault` was a directory that existed. Only asking the *mount*
and the *exec result* — not the unit's existence — showed the truth. Both are now covered by
`OnFailure=estate-notify@%n` on the unit, so the next 203/EXEC reaches a topic instead of a
journal nobody reads.
