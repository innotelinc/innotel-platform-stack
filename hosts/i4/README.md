# i4 host configuration

`i4` (`192.168.1.54`) is the fourth Incus host. It carries the estate edge,
`terminal`, `vault` and `vpn`.

## Uplink

A **USB Ethernet NIC** (Realtek RTL8153, `enx00051b940a40`) is i4's LAN uplink,
bridged into **`br-lan`**. The host's address lives on the bridge, and the
bridge is pinned to the NIC's MAC (`00:05:1b:94:0a:40`) so Cerulean's DHCP
reservation still hands out `192.168.1.54`.

**Wi-Fi is disabled.** `wlp1s0`'s netplan stanza (`MSK-ORBI`) was removed in the
same change, which is what turns the radio off — the PSK lived only in that
stanza. `wpa_supplicant.service` stays enabled (it runs in D-Bus mode with no
interface and no credentials, so it never associates), and removing the config is
the whole of the disable.

> **There is no fallback link.** If the bridge or the cable fails, i4 has no
> network. Recovery steps live on the host at `/root/RECOVERY-net-bridge.md`.

## Containers are bridged, not routed

Containers here used to be on `nictype: routed` over the Wi-Fi uplink: a
Wi-Fi station interface cannot be bridged, so the host held a `/32` route to each
container and answered ARP for it (proxy ARP). Wired uplink removes that
constraint, so all four now use `nictype: bridged, parent: br-lan` and sit
directly on the LAN, each still addressed statically from inside the container:

| Instance | Address | In-container config |
|---|---|---|
| `terminal` | 192.168.1.22 | `/etc/netplan/99-static.yaml` |
| `vpn` | 192.168.1.43 | `/etc/network/interfaces` |
| `proxy` | 192.168.1.71 | `/etc/netplan/10-lxc.yaml` |
| `vault` | 192.168.1.73 | `/etc/systemd/network/eth0.network` |

Their default gateway is the **LAN router `192.168.1.1`**. Under the routed
design it was the host's link-local routed gateway `169.254.0.1`, which no longer
exists once the routed NICs are gone — every instance had to be repointed, not
just the two whose committed templates suggested it.

The old routed design also made container traffic asymmetric once a second
uplink appeared: egress left on the USB NIC while replies arrived over Wi-Fi,
because the router had the container addresses resolved to the Wi-Fi MAC. It
worked only because `rp_filter` is 0. Bridging removes the asymmetry entirely.

## Files

| File | Installed to |
|---|---|
| `netplan/60-lan-bridge.yaml` | `/etc/netplan/60-lan-bridge.yaml` (mode `0600`) |
| `sysctl.d/90-network-tuning.conf` | `/etc/sysctl.d/90-network-tuning.conf` |
| `sysctl.d/99-incus-routed.conf` | `/etc/sysctl.d/99-incus-routed.conf` (base forwarding/`rp_filter`; see below) |
| `/etc/systemd/network/10-netplan-br-lan.network.d/50-optional.conf` | created on the host (see below) |

## Install / re-apply

```sh
install -m 0600 netplan/60-lan-bridge.yaml      /etc/netplan/60-lan-bridge.yaml
install -m 0644 sysctl.d/90-network-tuning.conf /etc/sysctl.d/90-network-tuning.conf
install -m 0644 sysctl.d/99-incus-routed.conf   /etc/sysctl.d/99-incus-routed.conf

# netplan 1.2 accepts `optional: true` but does not emit RequiredForOnline=no
# for the networkd renderer, so the bridge needs a drop-in to keep an unplugged
# cable from stalling systemd-networkd-wait-online.
install -d -m 0755 /etc/systemd/network/10-netplan-br-lan.network.d
printf '[Link]\nRequiredForOnline=no\n' \
  > /etc/systemd/network/10-netplan-br-lan.network.d/50-optional.conf

chmod 600 /etc/netplan/*.yaml
netplan generate && netplan apply
```

`sysctl.d/99-incus-routed.conf` keeps `ip_forward`, `proxy_arp` (in case a
routed NIC is used again) and `rp_filter=0`; it no longer names an uplink. The
interface-scoped keys were only ever needed because `systemd-sysctl` runs before
the uplink exists — the unit that re-asserted them is retired alongside the
routed NICs.

## Retired

- **`incus-routed-firewall` + `.service`** — the FORWARD ACCEPT rules that let
  routed containers cross between an uplink and their veth. The Docker `FORWARD`
  policy question they solved does not arise for bridged traffic, and
  `br_netfilter` is not loaded, so bridged frames never reach the `FORWARD`
  chain. The unit is disabled on the host.
- **`wlp1s0`'s netplan stanza** — the Wi-Fi uplink itself.
- **`systemd/wifi-powersave-off.service`** — disabled on i4 (it targets a radio
  that is no longer configured). The unit is kept in the repo: the latency
  checker, the Prometheus rules and a unit test all still reference it.

## History

i4 was Wi-Fi-only when it took the edge, which is why the routed-NIC design and
its proxy-ARP plumbing exist at all. The estate audit of 2026-10-01
(`docs/estate-audit-2026-10-01.md`) and the placement notes in
`docs/container-placement.md` describe that era; the uplink changed on
2026-10-09.
