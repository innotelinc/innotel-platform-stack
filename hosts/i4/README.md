# i4 host configuration

`i4` (`192.168.1.54`) is the fourth Incus host. Unlike `i2`/`i3` it has **no
Ethernet NIC** — its only uplink is Wi-Fi (`wlp1s0`, the `MSK-ORBI` AP) plus a
cellular `wwan0`. A Wi-Fi station interface cannot be bridged, so i4's
LAN-addressed containers use an incus **`routed` NIC** (`nictype: routed`,
`parent: wlp1s0`) with host **proxy ARP** rather than a bridged `br0`. The
first such container is `lantest` (profile `lanrouted`, LAN IP
`192.168.1.214`).

That mode has two pieces of state that do **not** survive a reboot on their
own, because incus does not manage them for routed NICs and Docker owns the
`FORWARD` chain (policy `DROP`):

| State | Where it actually lives | Persisted by |
|---|---|---|
| `net.ipv4.ip_forward`, `*.proxy_arp`, `*.rp_filter` | `/etc/sysctl.d/99-incus-routed.conf` | `systemd-sysctl` (base) **and** the unit below (re-asserts after the uplink is up) |
| `FORWARD` ACCEPT `wlp1s0 ↔ veth+` | live iptables only | **`incus-routed-firewall.service`** |

`systemd-sysctl` runs in early boot *before* `wlp1s0` exists, so the
interface-scoped keys (`net.ipv4.conf.wlp1s0.proxy_arp`) are skipped at that
point; the unit re-applies the whole set once the network is online, which is
why it also covers the sysctls.

## Files

| File | Installed to |
|---|---|
| `incus-routed-firewall` | `/usr/local/sbin/incus-routed-firewall` (mode `0755`) |
| `incus-routed-firewall.service` | `/etc/systemd/system/incus-routed-firewall.service` |
| `/etc/sysctl.d/99-incus-routed.conf` | created on the host (see below) |

The script is idempotent (`iptables -C` before `-I`), so it can be re-run at
any time — e.g. after `systemctl restart docker`, which re-inserts its chains
and re-sets the `FORWARD` policy to `DROP`.

## Install / re-apply

```sh
install -m 0755 incus-routed-firewall              /usr/local/sbin/incus-routed-firewall
install -m 0644 incus-routed-firewall.service      /etc/systemd/system/incus-routed-firewall.service
install -d -m 0755 /etc/sysctl.d
cat >/etc/sysctl.d/99-incus-routed.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv4.conf.all.proxy_arp=1
net.ipv4.conf.wlp1s0.proxy_arp=1
net.ipv4.conf.br0.proxy_arp=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
EOF
systemctl daemon-reload
systemctl enable --now incus-routed-firewall.service
incus-routed-firewall status
```

Override `UPLINK` (default `wlp1s0`) in the unit via
`/etc/default/incus-routed-firewall` if the uplink ever changes.

## Notes

- `br0` remains a *managed NAT* bridge (`10.20.0.1/24`) for internal-only
  containers; the routed NIC is additive. A wired NIC on i4 would be the
  durable fix and would let `br0` become a normal LAN bridge.
- The doc `docs/container-placement.md` § "i4" carries the architecture
  context; this directory is the host-side implementation.
