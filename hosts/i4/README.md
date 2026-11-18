# i4 host configuration

`i4` (`192.168.1.54`) is the fourth Incus host. It carries `atheniq` `.59`,
`mail` `.15`, `vault` `.73` and `vpn` `.43`. That is the 2026-10-09 placement:
`proxy` (the estate edge) and `terminal` went back to `i1`, and `atheniq` and
`mail` came here — every one of them keeping its address, which is pinned inside
the container and travels with its rootfs. See
`docs/container-placement.md` §*Changes applied 2026-10-09*.

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
constraint, so the containers here use `nictype: bridged, parent: br-lan` and sit
directly on the LAN, each still addressed from inside the container:

| Instance | Address | In-container config |
|---|---|---|
| `atheniq` | 192.168.1.59 | `/etc/netplan/10-lxc.yaml` — **DHCP**, with the MAC pinned |
| `mail` | 192.168.1.15 | `/etc/netplan/99-static.yaml` |
| `vault` | 192.168.1.73 | `/etc/systemd/network/eth0.network` |
| `vpn` | 192.168.1.43 | `/etc/network/interfaces` |

`atheniq` is the exception to "pinned in its own manager": it takes `.59` over
DHCP, and what keeps the address is the **MAC** — the Incus `hwaddr` is set
explicitly and its netplan says `dhcp-identifier: mac`, so the lease follows the
container rather than the interface. That survives a move but not a lease change,
which is why `check-container-addresses.py` reports it under rule 3 and
`docs/container-placement.md` §*Open items* carries the conversion.

Their default gateway is the **LAN router `192.168.1.1`**. Under the routed
design it was the host's link-local routed gateway `169.254.0.1`, which no longer
exists once the routed NICs are gone — every instance had to be repointed, not
just the two whose committed templates suggested it. The in-container comments in
`vault` and `vpn` still say "this host has no LAN bridge", and `proxy`'s and
`terminal`'s still name the routed hop; the *directives* are all correct, the
comments are from the 2026-10-02 era and predate the uplink change.

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
| `incus-rebalance.json` | `/etc/incus-rebalance.json` (the memory policy the rebalancer reads) |
| `incus-rebalance.logrotate` | `/etc/logrotate.d/incus-rebalance` |
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

## The off-edge liveness watcher runs here

`/opt/innotel/edge-liveness/check-edge-liveness.py` with
`/etc/systemd/system/edge-liveness.{service,timer}` and
`/etc/innotel/edge-liveness.env` (the mail recipient), installed from
`scripts/` + `systemd/` in this repo. It TCP-connects to the edge's doors every five
minutes and mails on failure, and it is here because **this host must not carry
`proxy`**: a watcher that shares a host with the edge cannot report that host being
gone. It moved here on 2026-10-09 from `i1`, where the 2026-10-09 placement change had
just put the edge back. A future placement change checks the pair.

## The host-side units, and where each is enabled

Four units that until 2026-11-18 existed only on hosts are in `systemd/` and `scripts/` now:
`estate-edge`, `estate-sample`, `estate-state` and `incus-rebalance`. **All four are
installed on both `i1` and `i4`** — what differs is which timers are enabled, and that
difference is the design, not drift:

| unit | i4 | i1 | what it does |
|---|---|---|---|
| `estate-edge.{service,timer}` | **enabled** | installed, disabled | probes every upstream the edge proxies to, every five minutes, and separates `DOWN` from `down-on-purpose` by reading the instance's own `user.estate.*` note. It belongs on a host that carries the edge without being it: a probe inside `proxy` could not report `proxy` gone — the same reason `edge-liveness` is here. On `i1` it has never run (no log, timer disabled). |
| `estate-sample.{service,timer}` | **enabled** | **enabled** | one line per instance per minute into `/var/lib/estate-samples/<host>.tsv`, trimmed to a day. Per host by nature: it reads that host's cgroups, because `incus exec` into an instance fails exactly when the boot ramp is worth sampling. `i1.tsv` and `i4.tsv` both exist and are current. |
| `estate-state.{service,timer}` | **enabled** | **enabled** | records why an instance is stopped, in the instance's own `user.estate.*` config, and reports on Monday the ones stopped over a week with no note. Also per host: the report is about the instances it can see, and `estate-edge` reads the notes it writes. |
| `incus-rebalance.{service,timer}` | **enabled** | installed, disabled | fits each container's memory cap to its measured use every five minutes (`--apply`). **One writer for the estate**: this host's pass reaches `i1` and `i2` over their Incus remotes, which is why `i1` has the unit and no timer — a second enabled copy would write the same caps twice. `i1` also has no `/etc/incus-rebalance.json`, so enabling it there would refuse rather than apply. |

`incus-rebalance` is the one to be careful with: the timer runs `--apply`, so the policy file
is a live control. It reads `/etc/incus-rebalance.json`; `hosts/i4/incus-rebalance.json` is
that file as deployed on 2026-11-18 (the `/root/incus-rebalance/policy.json` beside the old
README carries the pre-2026-11-15 placements and must not be installed over it — it was the
only copy before this change, which is what `docs/root-paths.md` is about). Run
`incus-rebalance` without `--apply` to see a pass and change nothing.

## Why `estate-checks` has no timer here

`estate-checks.timer` was enabled here and wrote ~40 `estate-check-*.prom` files into
`/var/lib/node_exporter/textfile` every half hour. Nothing scrapes them: this host runs no
`node-exporter`, and the estate's metrics stack — in `monarch` on `i2` since 2026-11-15 —
scrapes **one** publisher for those series on purpose. `docs/estate-rebalance-2026-11-15.md`
§16 drops `innotel_estate_check_.*` from the edge's target so a failing check raises one
alert rather than two with different `instance` labels. A third copy publishing into a
directory with no collector is CPU spent on an answer nobody reads, so the timer is disabled
and its stale textfiles are removed.

The unit and the scripts stay installed, for two reasons: with the edge container down,
`systemctl start estate-checks.service` here is the estate's own view of itself, and
`check-unit-drift.py` keeps comparing those files with the revision.

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
2026-10-09, and the same day the containers here changed: `proxy` and `terminal`
left for `i1`, and `atheniq` and `mail` arrived from it. `incus copy` with an
explicit `br-lan` device is what does that move (see the host's
`/root/RECOVERY-net-bridge.md`, which is the console-side rollback for all of
it).
