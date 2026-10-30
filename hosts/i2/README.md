# i2 host configuration

`i2` (`192.168.1.52`) is the Incus host that carries `capstone` (`.30`, the PBX and
n8n stack), `atheniq` (moved to `ord-atheniq` on 2026-10-27, so the container here
is stopped), `proxy` (`.71`: NPM, Authentik, Vault, Technitium, metrics),
`blueprint` and `clearpath`. Placement and CPU/memory caps live in
`docs/container-placement.md`.

This directory carries the one kernel setting a service on this host explicitly
asks for, and it is here for the same reason as `hosts/i1`'s: it is a **host**
parameter that no container can set.

## Files

| File | Installed to |
|---|---|
| `modules-load.d/br_netfilter.conf` | `/etc/modules-load.d/br_netfilter.conf` |
| `sysctl.d/99-bridge-nf-off.conf` | `/etc/sysctl.d/99-bridge-nf-off.conf` |
| `fix-br-netfilter.sh` | run in place (`/root/i2-br-netfilter-fix.sh` on the host) |

## Install / re-apply

```sh
install -m 0644 modules-load.d/br_netfilter.conf /etc/modules-load.d/br_netfilter.conf
install -m 0644 sysctl.d/99-bridge-nf-off.conf   /etc/sysctl.d/99-bridge-nf-off.conf
modprobe br_netfilter                              # no-op when already loaded
sysctl -p /etc/sysctl.d/99-bridge-nf-off.conf
lsmod | grep '^br_netfilter'                       # must list the module
sysctl -n net.bridge.bridge-nf-call-iptables       # must print 0
```

`fix-br-netfilter.sh` does the same and then verifies the bridges, the containers
and the runner — it is the version to run when the host has to be repaired rather
than rebuilt, and it never unloads the module. It lives on i2 as
`/root/i2-br-netfilter-fix.sh`; copy this file over from the repo, do not edit the
host copy.

## Why the module is loaded, and why the traversal is off

n8n's sandbox runner (`n8n-sandbox-runner-1` inside `capstone`) refuses to start
unless `/proc/sys/net/bridge/bridge-nf-call-iptables` **exists**, i.e. unless
`br_netfilter` is loaded:

```
{"level":"ERROR","msg":"create runtime","runtime":"docker",
 "error":"bridge netfilter: /proc/sys/net/bridge/bridge-nf-call-iptables does not
          exist: load the br_netfilter kernel module on the host"}
```

On 2026-10-30 it had restarted **1094 times** on that error: `n8n-sandbox-api` was
healthy, the dashboard was up, and every sandbox execution failed, so the symptom
looked like n8n rather than the kernel. The runner's `DOCKER_IGNORE_BR_NETFILTER_ERROR`
(groups/2-voice.yml) covers only the nested dockerd inside a sandbox — it cannot
satisfy the check above, which runs before any sandbox exists.

The module is not free on this host. `br0` carries the containers' **own** LAN
addresses (`capstone` = `192.168.1.30`), and `ontrak0` carries ontrak's, so the
paths that matter here are bridged, not routed. Loading `br_netfilter` with
`bridge-nf-call-iptables=1` sends every bridged frame through the `FORWARD` chain
as well; that is the shape that cut the L2 stretch on 2026-10-27, and the rollback
(`modprobe -r`) then deleted every bridge on the host and took the containers'
network with it.

The runner only needs the file to **exist**, and `0` leaves it present. So the
posture is both: module loaded, host-side bridge traversal off. `systemd-sysctl`
runs after `systemd-modules-load`, so the value is applied to the loaded module at
boot without ordering tricks.

## Rollback

Set the three sysctls back to `1` — never unload the module. Removing this
directory's files only changes what happens at the next boot; the running host
keeps the module until it reboots.
