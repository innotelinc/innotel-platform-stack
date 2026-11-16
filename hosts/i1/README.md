# i1 host configuration

`i1` (`192.168.1.51`) is the bare-metal Incus host: 15.3 GiB RAM, four vCPU, the
`incus` ZFS pool. It carries the edge — `proxy` (`.71`: NPM, Authentik, Vault,
Technitium, metrics) — plus `ontrak` (`.20`) and `monarch` (`.56`, the media
stack). Placement and CPU/memory caps live in `docs/container-placement.md`.

Unlike `i4` (see `hosts/i4/README.md`), this directory does **not** yet carry
i1's netplan or network drop-ins — only the kernel settings a service explicitly
asks for, which is currently one file.

## Files

| File | Installed to |
|---|---|
| `sysctl.d/60-redis-overcommit.conf` | `/etc/sysctl.d/60-redis-overcommit.conf` |
| `systemd/swap-ceiling-check.service.d/10-notify.conf` | `/etc/systemd/system/swap-ceiling-check.service.d/10-notify.conf` |
| `systemd/vault-replica-sync.service.d/10-notify.conf` | `/etc/systemd/system/vault-replica-sync.service.d/10-notify.conf` |

Existing host files that predate this directory stay where they are:
`/etc/sysctl.d/10-incus-inotify.conf` and `/etc/sysctl.d/30-lxc-inotify.conf`.

## Install / re-apply

```sh
install -m 0644 sysctl.d/60-redis-overcommit.conf /etc/sysctl.d/60-redis-overcommit.conf
sysctl -p /etc/sysctl.d/60-redis-overcommit.conf
sysctl -n vm.overcommit_memory      # must print 1

install -D -m 0644 systemd/swap-ceiling-check.service.d/10-notify.conf \
  /etc/systemd/system/swap-ceiling-check.service.d/10-notify.conf
systemctl daemon-reload
systemctl cat swap-ceiling-check.service | grep OnFailure   # estate-notify@%n
```

## Why the notify drop-ins live here

`systemd/swap-ceiling-check.service` deliberately carries no `OnFailure=`: the check exits 1
on a *standing* condition and its timer runs every ten minutes, so wiring it would notify once
per run for as long as the condition lasts. This host wanted the notification anyway. The
`vault-replica-sync.service` is the opposite case: its own header says a failed run *has* to
say so, and the notification it depends on lived only in `/etc/systemd/system` too.

Both drop-ins were invisible to anyone reading the repository until 2026-11-15, so both are
kept here now and compared by `scripts/check-unit-drift.py`, which reads
`hosts/<host>/systemd/` for the host it is checking. A third override added on a host without
a copy here is a `LOCAL` finding, not a mystery.

## Why overcommit lives here and not in the container

`monarch` is an Incus container, and `vm.overcommit_memory` is a **global** kernel
parameter — it is not namespaced, so a container cannot change it. Running
`sysctl -w vm.overcommit_memory=1` inside `monarch` fails with *"permission
denied on key"*, and a `/etc/sysctl.d` file written there is inert. Redis runs
inside the `monarch` container (Homarr ships it and starts it on `:6379`), so the
setting has to be made on the host that runs that container.

The value is shared by every container on i1; 1 means the kernel never refuses an
allocation for exceeding the commit limit, which is what Redis asks for so its
background saves cannot fail under memory pressure.
