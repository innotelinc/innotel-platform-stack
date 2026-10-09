# Incus container placement — i1 / i2 / i3 / i4

Surveyed 2026-09-27; refreshed 2026-10-01 (a morning survey and an evening
rebalance), 2026-10-02 (the edge moved onto `i4`) and 2026-10-09 (the edge came back
to `i1` and `i4`'s uplink changed — §*Changes applied 2026-10-09*). Four Incus hosts
run every estate container: `i4` was the test bench, and on 2026-10-02 it took four
containers off `i1` and became a host (see §*The i4 host*). This page records what each
host is, what currently sits on it, and where it should sit.

The 2026-10-01 refresh matters because the estate's *shape* changed, not just its
numbers: the pre-migration containers on `.46` (`atheniq`, `cloud`, `voice`,
`docs`, `ansible`, `slack`, `olympus`/`olympus-gw`) are gone, the `ontrak` address
incident was fixed by pinning addresses instead of trusting the router, and the
evening pass moved the edge services back onto i1, right-sized i1's CPU caps, and
put the address invariant on a schedule with an alert. The tables below are the
estate as it is, not as it was.

## Changes applied 2026-10-01

- **`ontrak` renumbered `.21` → `.20` and the sync stack died.** The container was
  recreated with a new MAC, the router's DHCP reservation no longer matched it, and
  it came up on `.20`. Everything that binds a specific LAN address failed at once:
  `ontrak-sync-api` and `ontrak-sync-web` were `Exited (255)` because `.env` says
  `ONTRAK_API_BIND=192.168.1.21` and the box did not have that address; the family
  stack (Genie, Sentinel, Tix, Training) stayed *running* only because it binds
  `0.0.0.0` — up on an address nothing dials, which is down with extra steps. Fixed
  by making the address static (below), not by trusting DHCP again.
- **`.21`, `.56` and `.71` are now static, not DHCP.** `/etc/netplan/10-lxc.yaml`
  on `ontrak`, `monarch` and `proxy` is rewritten to `dhcp4: false` with
  `addresses: [192.168.1.<n>/24]`, a default route via `192.168.1.1` and DNS
  `192.168.1.1` (backup kept beside it). The file is `chmod 600`. This is the fix
  that survives a container recreation, which a router reservation did not.
- **The invariant now has a check.** `scripts/check-container-addresses.py` reads
  each host over ssh and fails when a running container is not at the address every
  A record and every published port names — including the `Exited (255)` case,
  caught by name rather than by symptom. It also reports (warn, not fail) any
  container whose address still comes from DHCP: that is the state that produced
  this outage. `--json`, `--hosts`, and `SSHPASS`/keyless ssh are supported; an
  unreachable host exits **2**, never silently a pass.
- **Every running container is now pinned — the other ten too.** The 2026-10-01
  survey found ten containers (i2 `capstone`/`terminal`/`vault`/`www`; i3
  `distro`/`mail`/`onyx`/`pi`/`signara`/`vpn`) still taking their address from DHCP.
  Each is now static in the manager that actually holds it, because the estate has
  three: netplan (six of them, a `99-static.yaml` alongside `10-lxc.yaml`), PVE's
  systemd-networkd (`vault`/`www`/`pi`, their `eth0.network` rewritten), and
  ifupdown (`vpn`, `/etc/network/interfaces`). The check now reads all three, so a
  container pinned outside netplan is no longer called DHCP. Backups of every file
  replaced are kept beside it as `*.bak-<timestamp>`.
- **`vault` (`.73`) is not HashiCorp Vault.** It runs Vaultwarden (healthy) plus
  Linkwarden and Meilisearch; the earlier role label was stale. Nothing listens on
  `:8200`, which is why a LAN health probe returns nothing — not a consequence of
  pinning.
- **Memory caps set** on the containers that had none, so a single runaway cannot
  take a host down (values are ceilings, not reservations): i1 `monarch` 6 GiB /
  `cpu=2`, `ontrak` 2 GiB / `cpu=2`, `proxy` 5 GiB / `cpu=4`; i2 `atlas` 1 GiB,
  `capstone` 6 GiB, `dev` 4 GiB / `cpu=4`, `genesis` 1024 MiB / `cpu=2`,
  `rizzaura` 512 MiB, `terminal` 1024 MiB, `vault` 2048 MiB / `cpu=4`, `www`
  1536 MiB / `cpu=4`; i3 `pi` 1536 MiB / `cpu=4`, `vpn` 1024 MiB / `cpu=4`, and
  1 GiB on each of the rest.
- **`genesis` moved i3 `.65` → i2 `.66`** (see the 2026-10-01 action list below).
- **The router reservations are no longer the mechanism.** With three containers
  static by netplan the address stops being a lease; the remaining DHCP containers
  are listed as findings so the same drift is visible rather than latent.

### Changes applied 2026-10-01 (evening rebalance)

- **The edge services moved back onto i1.** `terminal` `.22` and `vault` `.73`
  came off i2, and `acme` `.49`, `mail` `.15` and `vpn` `.43` came off i3, so the
  documented topology — i1 owns the edge — is deployed again. Each move was an
  `incus copy <remote>:<name> <name> --storage incus` from i1, over the existing
  `i2move`/`i3move` TLS remotes, followed by deleting the source. **Every container
  kept its address**, because the address is pinned *inside* the container and
  travels with its rootfs: a cross-host copy is exactly the recreation that used to
  renumber, and pinning is what makes the move address-preserving. i1's root pool
  is `incus`, not `tank`, so the copy remaps the root disk (that is what `--storage
  incus` is doing).
- **i1's CPU caps were right-sized.** They summed to **24 vCPU on a 4-vCPU host**
  (`proxy` 4, `vault` 4, `vpn` 4, `mail` 4 inherited from its `medium` profile, and
  2 each on the rest) — a cap at or above the host total protects nobody. `proxy`
  is now 2 and every other i1 container is 1, except `monarch` which keeps 2
  because media transcoding is the one real CPU consumer. **No container can now
  claim the whole host, and a runaway anywhere leaves at least two vCPU for the
  rest.** Incus applies `limits.cpu` **live**, and it is implemented as a cpuset
  pin rather than a quota, so the caps are in force with no restart. Verified the
  same evening: every i1 container's `cpuset.cpus.effective` holds exactly its
  `limits.cpu` count (`proxy` `0-1`, `monarch` `2-3`, one CPU each on the rest).
  (The 2026-09-27 note below that `limits.cpu` waits for a restart is wrong, and is
  corrected here.)
- **The estate's host invariants are scheduled and alerting.**
  `systemd/estate-checks.{service,timer}` runs seven checks every 30 minutes *on the
  Cerulean edge* and writes each one's textfile into node-exporter's directory:
  `check-container-addresses.py` (every address an A record names),
  `check-container-limits.py` (a declared `limits.cpu` is the cpuset actually pinned),
  `check-estate-inventory.py` (no host or storage pool this page does not know), and
  `check-address-latency.py` (every address the estate dials — each host **and** every
  container in the address table — answers at all, and answers in milliseconds rather
  than hundreds; the reachability half closes the outage one layer below a renumber, and
  the latency half is the `i4` WiFi power-save regression, measured rather than felt), and
  `check-host-disk.py` (each host's root filesystem has room — 80 % warns, 90 % fails),
  and `check-alert-delivery.py` (the estate's one Alertmanager actually *delivers*: it
  injects a short-lived probe alert through the receiver's own v2 API and reads the mail
  server for the delivery — the one thing `amtool check-config` cannot tell you), and
  `check-mail-auth.py` (each mail domain's SPF, DKIM and DMARC records resolve in the
  zone the public reads, with the DKIM selectors versioned in the check — Stalwart now
  publishes a rotation itself, and this reads what the public zone actually serves —
  see §*Open items*).
  They share one metric family — `innotel_estate_check`, one series per check via a
  `check` label — so `prometheus/rules/estate-checks.yml` alerts on all seven
  (`EstateCheckFailing`, `...CouldNotRun`, `...Stale`, `...NotPinned`), and the
  provisioned `grafana/dashboards/estate-checks.json` shows them (a green stat, a
  per-check table, whether the receiver delivered, and the per-address latency chart). The edge reaches
  the hosts with a dedicated ssh key, `/root/.ssh/container-address`, authorized
  `from="192.168.1.71"` only, so the scheduled run needs no password on disk. A check
  that cannot reach a host publishes status 0 rather than nothing, because "no data"
  is how the outage stayed invisible. Verified running 2026-10-01.
  The two host-side files key-only ssh needs — each host's `authorized_keys` and the
  edge's `/root/.ssh/config` — are installed by `scripts/trust-estate-hosts.py`
  (idempotent, with a `--check` mode), so the trust is reproducible rather than a hand
  fix. See §*The i4 host* for why that file exists.

### Changes applied 2026-10-02

- **The edge and three services moved i1 → i4.** `proxy` `.71`, `terminal` `.22`,
  `vault` `.73` and `vpn` `.43` were copied onto `i4` with
  `incus copy i1move:<name> <name> --storage tank --instance-only` and the i1 sources
  were then deleted. Each kept its address because the address is pinned *inside* the
  container and travels with its rootfs — the same property the 2026-10-01 moves
  relied on. (The `i1move` remote is a TLS certificate trusted on i1; the older OIDC
  `i1` remote's device token had expired, which is why the copy used a fresh one.)
- **The cost of that, stated plainly — and mostly a setting, not the hop.** `i4` is a
  2-vCPU box whose uplink is WiFi, so the edge — every address the estate dials, plus
  DNS on `:53` — sits behind a wireless hop. The first measurement looked bad: an
  *idle* container answered in 100–900 ms while the busy edge was fine. The cause was
  WiFi **power save**, not the hop — the radio held frames until the next DTIM beacon
  (~312 ms) whenever its own traffic did not keep it awake. With it off
  (`systemd/wifi-powersave-off.service`, deployed 2026-10-02) every `i4` container
  answers in **~11–14 ms** with ~3 ms jitter, and DNS on `:53` in ~30 ms, the same as
  the router. A wired NIC is therefore not needed for latency; see §The i4 host.
- **The bench container `lantest` was retired.** Its purpose — proving `routed`
  networking works — is served by four production containers on the same host, and a
  container the address table does not carry is exactly the `unexpected` warning that
  would otherwise fire on every run. It was removed, not merely stopped: a stopped one
  still has to be reasoned about on every run, and the routed pattern it existed to
  prove is now carried by the four containers the estate dials.
- **Two retired, stopped containers were removed: `acme` (i1 `.49`) and `patchmon`
  (i3 `.108`).** Both were `STOPPED` with `boot.autostart=false`, so nothing dialled
  them and there was no address to be wrong. With no purpose left they were taken off
  the record — out of `check-container-addresses.py` and out of this page — rather than
  left for every run to explain. `olympus-archived-20260930` (i3) stays as the one
  archived container.
- **`i4`'s disk was reclaimed.** The four copies are stored uncompressed in a `dir`
  pool, so they took tens of GB and `i4`'s root reached 51 %. A `docker image prune` +
  `docker builder prune` on the edge freed **6.8 GB** of dangling images and build
  cache, taking it to **45 %**. The remaining tagged-but-unreferenced images (a previous
  `npm-edge:2.15.1`, the original `diegosouzapw/omniroute:latest`, plus an old
  `nginx-proxy-manager` and a stray `backup-ui`) were then removed with
  `docker image prune -af`, the rule `scripts/docker-cleanup.sh` already applies nightly
  (an image no container references is rebuildable or re-pullable), freeing **5.5 GB**
  more and taking `i4`'s root to **39 %**. Nothing rolls back through an image here: the
  one parked rollback is a stopped *container* (`gateway-sso-pre-version-20261001`),
  which still references — and therefore kept — its `oauth2-proxy:7.7.1-alpine`.
- **Every dialled address is now probed for reachability, and the power-save number is
  scheduled.** `check-address-latency.py` pings each estate host **and** every container in
  the address table (read from `check-container-addresses.py`, so the two cannot drift),
  one ping per sample after an idle gap. No reply is a failure — that is the outage one
  layer below a renumber: a container with the *right* address the host no longer routes.
  A 200 ms median is a warning: ~25× the healthy ~7 ms, and the shape the WiFi power save
  left behind. It joins `innotel_estate_check` and the alert rules as `address_latency`.
- **The checks' trust is a script, not a memory.** `scripts/trust-estate-hosts.py`
  installs the check key into every estate host's `authorized_keys` and owns a marked
  block in the edge's `/root/.ssh/config` naming every host by address; `--check` reports
  drift without a password. The edge's config was reduced to that one block on 2026-10-02
  (the hand-fix it replaced left a second, identical `Host` entry), and `--check` reads 0.
- **Root-filesystem headroom is watched now, not noted.** The page has said "i2 is the
  one to watch" since the `.46` migration, and nothing looked between surveys.
  `check-host-disk.py` reads `df -P /` on every host each run: 80 % is a warning, 90 % a
  failure, and it publishes the percentage and free bytes per host. Its first run
  corrected this page's own assumption: the host to watch is **i3 at 68 %**, not i2 —
  i1 13 %, i2 47 %, i3 68 %, i4 39 %. All are under the 80 % line, so all are green; i3
  is the one with the least room, and it is now a number rather than a memory.
- **Mail authentication is now checked, not assumed.** `check-mail-auth.py` reads the
  SPF, DKIM and DMARC records back from the **authoritative** zone the public sees
  (Technitium on the edge, `.71` — not the LAN resolver and not the older BIND copy), one
  `dig` per record, and fails on a `v=spf1` / `v=DMARC1` / `v=DKIM1` record that does not
  resolve. A zone that cannot be read at all is exit 2, never a domain that looks
  unauthenticated. The DKIM selectors are a versioned table in the check (`v1-ed25519-…`
  and  `v1-rsa-…` for `innotel.us` and `signara.innotel.us`): a rotation edits the zone
  *and* that table. Stalwart publishes DKIM to Technitium itself now (see §*Open
  items*), and this check is the proof it did — it reads the public zone, not the mail
  server. Verified live 2026-10-03: 8/8 records resolved.
- **Receiving alerts is now checked, not assumed.** The receiver's own history is the
  argument for `check-alert-delivery.py`: it passed `amtool check-config` with `SUCCESS`
  while delivering nowhere, and later while the relay refused its `EHLO` and its
  certificate — every state looked healthy and said nothing. The check makes the
  receiver *talk*: it adds one probe alert (`EstateDeliveryProbe<epoch>`) through
  Alertmanager's v2 API, then reads the mail server's delivery log for a delivery from
  the receiver's address to its recipient within the window. No delivery is a **failure**
  naming the receiver's own last notify error; a host it cannot read is exit 2, never
  silence. It needs `i3` (Alertmanager) and `i1` (Stalwart), so it keeps its own two-host
  table rather than joining the four-host coverage guard. Verified live 2026-10-02: the
  probe alert was seen delivered to `admin@innotel.us`.
- **The edge is watched from off the edge, and the report goes out by mail.**
  `scripts/check-edge-liveness.py` runs on `i1` under `systemd/edge-liveness.{service,timer}`
  every five minutes and TCP-connects to the edge's doors; two attempts per endpoint, so
  one dropped packet on the WiFi link does not page. On failure it exits non-zero (the unit
  shows failed) and mails `EDGE_LIVENESS_MAIL_TO` through the estate's own mail server —
  not through the estate's Alertmanager, which is loopback-only on `i3` and so not
  something a watcher on `i1` can POST to. It is the only checker that can still report the edge being gone, because it does
  not run there.
- **The scheduled checks now cover `i4`.** `check-estate-inventory.py` no longer lists
  it as an optional bench (every named host must answer), `check-container-addresses.py`
  carries its four addresses, and `check-container-limits.py` reads its caps.
- **Why the bench was reported unreachable, and what fixed it.** The checks run on the
  edge with key-only ssh (`/root/.ssh/container-address`), and two things were missing:
  `i4`'s `authorized_keys` did not carry that key, and the edge's `/root/.ssh/config`
  `Host` line did not name `i4`. Both were host-side gaps, not a powered-off bench.
  With both fixed, the inventory check reads `i4` cleanly (0 findings). Neither file is
  in this repo, so this note is the record of what the hosts need.

### Changes applied 2026-10-09

- **`i4`'s uplink became a USB Ethernet NIC, and its containers changed with it.** Until
  this change `i4`'s only uplink was Wi-Fi, which cannot be bridged, so containers reached
  `192.168.1.x` through a `nictype: routed` NIC on `wlp1s0`: the host proxy-ARPed for the
  address and the container's gateway was the host's link-local `169.254.0.1` — *not* the
  `.1` router. A USB NIC (`enx00051b940a40`) now gives a bridgeable uplink, so the host
  address lives on **`br-lan`** (`.54`, the bridge pinned to the NIC's MAC so Cerulean's
  DHCP reservation still matches) and the containers are `nictype: bridged, parent:
  br-lan` with the **`.1` router** as their next hop. Wi-Fi is disabled outright —
  `wlp1s0`'s netplan stanza held the only copy of the PSK, so removing the stanza is the
  disable — which also means **there is no fallback link**: a broken bridge or cable is a
  console job (`/root/RECOVERY-net-bridge.md` on `i4` is the host-side record, including
  the rollback and the per-container moves).
- **`proxy` (the edge) and `terminal` moved back to `i1`; `atheniq` and `mail` moved onto
  `i4`.** Both directions in one pass, so `i4` still carries four containers — `atheniq`
  `.59`, `mail` `.15`, `vault` `.73`, `vpn` `.43` — and `i1` carries five: `proxy` `.71`,
  `terminal` `.22`, `monarch` `.56`, `ontrak` `.21`, `genie-preview` `.24`. Every one kept
  its address, for the same reason the 2026-10-01 and 2026-10-02 moves did: the address is
  declared *inside* the container and `incus copy` carries its rootfs, so a relocation
  changes the next hop and nothing else. The edge is back where the 2026-10-01 pass put it
  — `i1`, the host every consumer dials — and `i4` stays a real host rather than returning
  to the bench.
- **`atheniq` is the one container here whose address is not pinned in its own manager.**
  It takes `.59` over **DHCP** and keeps it because its MAC is pinned (an explicit Incus
  `hwaddr` plus `dhcp-identifier: mac` in its netplan, which is why the copy had to carry
  the old MAC). That survives a move; it does not survive a lease change, which is the
  state the 2026-10-01 incident came from. `check-container-addresses.py` reports it under
  rule 3 on every run, and §*Open items* carries the conversion to the pinned-static style
  the other containers use.
- **The address table moved with the containers, and it has to.**
  `scripts/check-container-addresses.py` is the contract the scheduled check enforces, and
  a row naming the host a container *left* is not a stale note: it is a `FAIL` ("not
  present") on every run plus an `unexpected` warning for the host it moved to. That is
  exactly what the installed copy reported for the five containers this change touched —
  three failures and five warnings, all false — which is how the drift was found. The
  table now names the hosts above.
- **`genie-preview` (`.24`) entered the table.** It runs `innotel/ontrak-genie:main` on
  `i1` and holds a LAN address, so rule 4 — every running container must be in the table,
  or every run warns about it — is what puts it there. **If it is a throwaway, delete the
  container and its row together**: the table is enforced in both directions, so a row for
  a container someone removes fails the same way a missing row warns.
- **The liveness watcher is now co-located with what it watches, and that is a defect.**
  `systemd/edge-liveness.{service,timer}` was installed on `i1` *because the edge was not
  there*, and this change moved the edge back onto `i1` — so the watcher and its subject
  share a host, and a host-level failure now takes out both. It still catches an edge
  *ingress* failure (a dead NPM, Authentik or Docker), which is most of what it has ever
  caught, but not the host going down, which is the case it was written for. Re-home the
  watcher to a non-edge host (`i4`, which gave the edge up) or move the edge off `i1`
  again; the unit and the script now say this at the top.
- **Not changed: the comments inside the containers.** `vault` and `vpn` still say "this
  host has no LAN bridge", and `proxy` and `terminal` still name the `169.254.0.1` hop as
  their next step. Every *directive* is correct — each address and gateway matches what is
  deployed — but the comments describe the 2026-10-02 topology. They live inside
  containers, not in this repo, so this is the record of what they need; see
  `hosts/i4/README.md` for the same note next to the file table.

## Changes applied 2026-09-27

- **Limits set** on the unbounded heavies: i2 `atheniq`/`capstone` = 6 GiB,
  `development` = 4 GiB; i1 `monarch`/`git` = 2 vCPU; i3 `olympus`/`onyx`/  `signara` = 1 GiB. Both `limits.memory` and `limits.cpu` apply live — `limits.cpu`
  as a cpuset pin, which the 2026-10-01 pass verified and corrected here (an
  earlier note claimed it waited for a restart).
- i1's survey-day `load average` of ~6 was **not** placement pressure: a
  cryptominer was running in the `git` container (`security-incident-2026-09-27-git-miner.md`).
  Once contained, i1 fell from load ~18 to ~4.8 and freed ~2.4 GiB.
- **`docs` and `ansible` moved i2 → i3**, keeping their IPs (`.125`/`.35`).
  i2 free went from 0.4 GiB to ~0.8 GiB; i3 carries them comfortably.
- The `.46` `distro-control-plane` duplicate was removed (see
  `dev-container-migration.md`).
- **i1 `git` (`.90`) was retired** (2026-09-27) after the cryptominer found in
  it: it was a second, un-hardened Gitea with exploit-PoC repos and no NPM host,
  and the estate's real Gitea is `.46` `atlas-gitea` (`git.innotel.us`). i1 is
  back to 7 containers. See `security-incident-2026-09-27-git-miner.md`.
- **`magnate` moved off `.46`** onto its own i3 container (`.57`, 2026-09-27).
- **`atlas` moved off `.46`** onto its own i2 container (`.90`, 2026-09-27),
  reusing the address the retired `git` container held. It is tiny (Gitea +
  convex + postgres + dashboard, well under 0.5 GiB), so i2's remaining headroom
  covers it.
- **`rizzaura` moved off `.46`** onto its own i2 container (`.62`, 2026-09-27):
  five small services (~50 MiB).
- **`olympus` factory + Studio moved off `.46`** onto their own i3 container
  (`olympus-gw`, `.64`, 2026-09-27). The `:20128` SSO proxy did **not** move: it
  fronts `omniroute` on `127.0.0.1:20128` and the estate dials
  `192.168.1.71:20128` by address, so it stays with `omniroute` on `.46` until
  that pair relocates together. i3 was the right host for the factory/studio
  because they consume the gateway over the LAN, not loopback.
- **The gateway door moved from `20129` onto `20128`** (2026-09-27) — the
  gateway's own default port, on `.46`'s LAN address. `omniroute` keeps
  `127.0.0.1:20128` and `172.17.0.1:20128`; the proxy takes the LAN address
  because a wildcard bind on that port overlaps them. Edge hosts 178/179 and all
  five live consumer stacks were repointed in the same change, and
  `ips/scripts/check-gateway-targets.py` now fails a stale `20129` by name.
- **`.46`'s `clipbucket` was retired rather than moved** (2026-09-27): it was a
  strict subset of i1 `.56`'s live one (`tube.innotel.us` → `.56:14011`), so only
  the container and image went; its 29 GB volume is retained. That leaves
  `omniroute` plus the gateway's SSO proxy/redis as the only things on `.46`.

## The hosts

Values are the 2026-10-01 evening (21:00 EDT) survey, after that move and before the
2026-10-02 one; `i4` joined as a host on 2026-10-02 and is described in §*The i4 host*
below, though its container set changed again on 2026-10-09 (§*Changes applied
2026-10-09*); the current container sets, and the figures for the hosts that changed,
are in finding 1.

| Host | Address | vCPU | RAM | Load at survey | RAM available | Root disk | Pool |
|---|---|---|---|---|---|---|---|
| **i1** | `.51` | 4 | 14.9 GiB | 2.73 / 12.27 / 14.55 † | 3.9 GiB | 98 G (14 %) | `incus` (zfs) |
| **i2** | `.52` | 8 | 13.1 GiB | 0.54 / 0.68 / 0.89 | 4.5 GiB | 23 G (48 %) | `main-pool` (btrfs) |
| **i3** | `.53` | 8 | 5.3 GiB | **0.18** / 0.30 / 0.23 | 3.0 GiB | 23 G (67 %) | `tank` (zfs) |

† i1's load is `monarch`'s media stack, not the move: qbittorrent and the *arr
apps are the top CPU consumers, and the load stayed up after the copies finished.
`monarch` is pinned to CPUs 2-3 (finding 5), so this is real work on cores the edge
does not need — the load is high, but its blast radius is bounded to a pair of
cores. The pre-move reading was 0.33 / 1.39 / 2.59.`i2` and `i3` used to carry the same `tank` profile set, which is what let the
`genesis` move work unchanged — but they had diverged, and i2's pool split has since
been reconciled. On **i2** the `default` and `docker` profiles point at **`main-pool`**:
a 465 GiB btrfs pool on `/dev/sdc`, added 2026-10-01 beside the older zfs `tank` on
`/dev/sdb`. `capstone` and `www` moved onto it first; the 2026-10-01 pass found
`atlas`, `dev`, `genesis` and `rizzaura` still on `tank` — a new container would have
landed on one pool and four sat on the other — and moved them across, so **i2 is one
pool again** and `tank` (and its 300 GB `/dev/sdb`) was retired. `i3` is unchanged on
`tank`. All four are `incus` container hosts, reached over SSH as `root` with the
estate's incus root password; `i2` runs on pm3 (VM200), `i3` on pm4 (VM200), and i1
is another guest on the same Proxmox host as i2 — which is why "more vCPUs on pm3"
is the way i1's CPU would be grown, a decision the evening pass made and declined
(finding 2). `i4` is a separate small box with no LAN bridge (§*The i4 host*).

### The i4 host (was the bench)

`i4` (`.54`) was a test bench that ran no estate container. On 2026-10-02 it became a
host: `proxy`, `terminal`, `vault` and `vpn` moved onto it from `i1`, keeping their
addresses. On 2026-10-09 two of those four left again — `proxy` and `terminal` went back
to `i1`, and `atheniq` `.59` and `mail` `.15` came the other way — so the four addresses
the estate dials here are `atheniq`, `mail`, `vault` and `vpn`.

The hardware did not change, and it is the weakest of the four hosts: **2 vCPU,
~7.7 GiB, `dir`-backed storage (`default`, `tank`), a 153 G root disk**, and — the
part that matters — an uplink. That uplink is now a **USB Ethernet NIC bridged into
`br-lan`**, so `bridged` NICs have something to attach to and the containers use them;
Wi-Fi is disabled. Before 2026-10-09 the uplink was Wi-Fi (`wlp1s0`), which cannot be
bridged, so containers were `nictype: routed` on it with host proxy ARP and a link-local
`169.254.0.1` gateway. `dir`
storage is also why the copied rootfs images are larger here than the ZFS `USED`
figures they came from — the ZFS number is compressed, the `dir` one is not.

`i4` is in `HOSTS`/`EXPECTED` in `scripts/check-container-addresses.py`, in `HOSTS`
in `check-container-limits.py` and `check-address-latency.py`, and it is a **required** host
in `check-estate-inventory.py` (the `OPTIONAL` set is empty now). Its one bench
container, `lantest`, was retired on 2026-10-02 — the routed pattern is carried by the
production containers, and a container the address table does not name warns on every
scheduled run. That is what put `genie-preview` into the table on 2026-10-09. The WiFi hop was **not** the latency problem it first looked
like: with power save off it answered in ~7 ms (see the 2026-10-02 change log), so the
2026-10-09 wired uplink was for throughput and reliability rather than latency. The move
added a place to put the edge, not CPU or RAM to run it on.

The trust the checks depend on is now written down rather than done by hand.
`scripts/trust-estate-hosts.py`, run on the edge, puts the check key into every host's
`authorized_keys` and owns a marked block in the edge's `/root/.ssh/config` naming every
host by address with the check key as its `IdentityFile`; `--check` reports drift without
changing anything. That is the scripted form of exactly what was missing here — see
`estate_check.HostUnreadable` for what a missing half looks like from the inside. And the
reachability and latency are scheduled numbers now: `check-address-latency.py` pings
every address the estate dials — each host and every container in the address table — one
ping per sample after an idle gap (so a trained radio cannot hide power save). No reply is
a failure, and a median at 200 ms is a warning, ~25× the healthy reading and far below the
100–900 ms the power save produced.

That password is **not written down here** — golden rule 4 (no credential in any
repo file) applies to documentation as much as to code, and a literal in a doc
ships in every clone. A provisioning script takes it from the environment or from
its own gitignored `.env` instead (`ONTRAK_INCUS_PASSWORD` in Ontrak Sync's
`scripts/setup.sh` is the reference implementation).

## What is on each host now

Memory is live usage; disk is the container rootfs.

Memory is the container's live usage and the cap set on it (`limits.memory` / `limits.cpu`).
The rows for the hosts the 2026-10-09 pass changed were read that day; the rest are the
2026-10-02 reading.

| Host | Container | IP | Role | Mem (cap) | CPU cap |
|---|---|---|---|---|---|
| i1 | `genie-preview` | `.24` | the Genie preview (`innotel/ontrak-genie:main`) | 183 MiB (4 GiB) | 2 |
| i1 | `monarch` | `.56` | media (Jellyfin etc.) | 3.83 GiB (6 GiB) | 2 |
| i1 | `ontrak` | `.21` | Ontrak family stack + Ontrak Sync | 707 MiB (6 GiB) | 4 |
| i1 | `proxy` | `.71` | **the Cerulean edge** — NPM, Authentik, Vault, Technitium, metrics | 3.02 GiB (5 GiB) | 2 |
| i1 | `terminal` | `.22` | web terminal (termix, guacd, zapit) | 539 MiB (1024 MiB) | 1 |
| i2 | `atlas` | `.90` | Gitea + convex + postgres + dashboard | 415 MiB (1 GiB) | — |
| i2 | `capstone` | `.30` | Zeus / capstone telephony | **5.84 GiB** (6 GiB) | — |
| i2 | `dev` | `.74` | **the `dev` container** — every project's docker stack | 1.85 GiB (4 GiB) | 4 |
| i2 | `genesis` | `.66` | BusinessOps — intake + assisted EIN filing | 183 MiB (1024 MiB) | 2 |
| i2 | `rizzaura` | `.62` | Rizz Aura (5 svc) | 207 MiB (512 MiB) | — |
| i2 | `www` | `.80` | public website (+ `tun0`) | 655 MiB (1536 MiB) | 4 |
| i3 | `distro` | `.61` | distro control plane | 191 MiB (1 GiB) | — |
| i3 | `magnate` | `.57` | Magnate | 194 MiB (1 GiB) | — |
| i3 | `olympus-archived-20260930` | — | archived Olympus — **STOPPED** | — (1 GiB) | — |
| i3 | `onyx` | `.60` | Onyx (RAG) | 274 MiB (1 GiB) | — |
| i3 | `pi` | `.70` | pi | 176 MiB (1536 MiB) | 4 |
| i3 | `signara` | `.44` | Signara | 562 MiB (1 GiB) | — |
| i3 | `subscribe` | `.58` | Subscribe | 88 MiB (1 GiB) | — |
| i4 | `atheniq` | `.59` | AthenIQ LMS (Tutor / Open edX) | 3.09 GiB (8 GiB) | 3 |
| i4 | `mail` | `.15` | mail (SMTP/IMAP) | 102 MiB (4 GiB) | 2 |
| i4 | `vault` | `.73` | Vaultwarden + Linkwarden + Meilisearch | 960 MiB (2048 MiB) | 1 |
| i4 | `vpn` | `.43` | WireGuard (`10.7.0.2` wg0) | 268 MiB (1024 MiB) | 1 |

`cloud`, `voice`, `docs`, `ansible`, `slack` and `olympus`/`olympus-gw`, which the
2026-09-27 survey listed beside `atheniq`, are **no longer present on any host** — those
repositories were retired with the `.46` migration. `atheniq` is the exception: it stayed,
moved `i2` → `i1` → `i4`, and is a running container above. The retired names are kept
here only so the two surveys can be read against each other.

## Findings

1. **Memory pressure is spent, and i1 is roomy again.** i1 has gone from eight
   containers and **3.9 GiB** available back to three and **8.1 GiB** after the
   2026-10-02 move sent the edge and its three satellites to `i4`; i2 has **4.5
   GiB**, i3 **3.0 GiB**, and the new host `i4` **3.2 GiB**. The `.46` migration
   (which retired `atheniq`, `cloud`, `voice`, `docs`, `ansible`, `slack`) is what
   freed i2; the 2026-10-01 rebalance redrew i1, and the 2026-10-02 move redrew it
   again, and the 2026-10-09 pass swapped two containers each way without changing either
   host's container count. **`i4` is the one to watch now:** the swap landed `atheniq`
   (3.09 GiB live, an 8 GiB cap) on the smallest host, which reads **1.0 GiB available
   of 7.2 GiB** with a 32 % root — tighter than the 2026-10-02 readings for any host
   (i1 8.1 GiB, i2 4.5 GiB, i3 3.0 GiB, all from that date), and the reason §*Open
   items* carries `atheniq`'s address and its `limits.cpu`.
2. **i1 carries the edge again, and it was never CPU-bound.** Five containers now
   (`proxy`, `monarch`, `ontrak`, `terminal`, `genie-preview`), of which the busy one is
   `monarch`. The survey-day 1.67 load on 4 vCPU was not saturation, and the evening
   spike is `monarch`'s media stack (qbittorrent and the *arr apps) — exactly the
   container `limits.cpu=2` exists to fence, and it **is** fenced: `monarch` is
   pinned to CPUs 0-1, so the load runs on cores the other four do not need. **pm3
   therefore does not need more vCPUs:** its guest i1 has never been provisioned to
   its 4, and i2's load is 0.89 across 8. The 2026-10-02 move put the edge on `i4`,
   the estate's weakest box — a deliberate placement, and the thing to watch if the
   WiFi hop costs more in throughput or reliability than it buys in placement
   (§*The i4 host*) — latency alone is measured and fine. The 2026-10-09 pass settled
   that question the other way: the edge is back on `i1` and `i4` has a wired uplink.
3. **i2 is the app host and the busiest by container size.** Six containers and
   8.9 GiB used, but 4.5 GiB free and load 0.89 on 8 vCPU. `capstone` alone is
   5.84 GiB of the 8.9 — 66 % of the host's usage in one container.
4. **i3 is CPU-idle and the smallest.** Load 0.18 on 8 vCPU, and after giving the
   edge back it carries six running containers on 5.3 GiB (3.0 GiB available). It
   has the CPU the others lack and the RAM the others have to spare.
5. **i1's CPU caps were the real defect and were fixed; the 2026-10-09 moves put the
   same pattern back on `i4`; the memory caps still oversubscribe on purpose.** Before
   the 2026-10-01 pass i1's CPU caps summed to **24 vCPU on a 4-vCPU host** (`proxy`
   alone was capped at the whole host), so a runaway there could starve everything else.
   They were set so no container can claim the whole host (the largest cap was **2**).
   Incus implements `limits.cpu` as a **cpuset pin**, not a quota, and applies it live:
   each container gets exactly N host CPUs in `cpuset.cpus.effective`, which a container
   cannot exceed even when idle cores exist. Verified 2026-10-02 on the then-current
   three: i1 `monarch` 0-1, `ontrak` 2, `mail` 3. **The caps are over the hosts again,
   and it is the containers that moved that carry it:** i1's five caps sum to **11 vCPU
   on 4**, of which `ontrak`'s **4** is the whole host — the exact pattern the 2026-10-01
   pass removed — and i4's four sum to **7 vCPU on 2**, where `atheniq`'s cap of **3 is
   above the host** and the estate's own check warns by name ("incus clamps it, so the
   written cap is not the fenced one"). A cap is set for the host a container *was* on
   and travels with the rootfs, which is why a placement change is also a limits change.
   Memory caps still oversubscribe every host — i1 22 GiB of caps on 14.9 GiB, i4 15 GiB
   on 7.2 GiB, i2 14 GiB on 13.1 GiB, i3 6.5 GiB of running caps on 5.3 GiB — which is
   deliberate: a memory cap is a per-container runaway guard, not a reservation.
6. **The invariants this page describes are checked, not asserted.** Every
   container's address is static in its own manager, so a recreation cannot renumber
   it; every declared `limits.cpu` is the cpuset actually pinned (finding 5); and the
   host and pool inventory is compared against reality — which is how `i4` and
   `main-pool` stopped being invisible, and how i2's two pools came to be reconciled to
   one. Three checks run every 30 minutes on the Cerulean edge and alert on a failure,
   on a run that could not reach a host, on a run that has stopped succeeding, and on a
   check that has never reported at all (`EstateCheckAbsent` — a check whose textfile
   never reached node-exporter is otherwise the exact shape of the 2026-10-01 silence).
   As of 2026-10-09 the address table is back in line with the deployed estate — the
   moves of that day had left it naming hosts the containers had left, which the
   installed copy reported as three `FAIL`s and five warnings across two of the four
   hosts, all false. Re-run against the live estate from the edge, where it actually
   runs, it reports **0 failures and one warning**: `atheniq`'s DHCP address, which
   §*Open items* carries.
7. **No host can absorb the two heavies.** `monarch` (3.76 GiB) fits neither i2
   (4.5 GiB free, but it is the app host) nor i3 (5.3 GiB total); `capstone`
   (5.84 GiB) fits nowhere but i2. So the heavies stay where they are, and any real
   rebalance is a *light* container moving to meet idle CPU, or more RAM on pm4.

## Recommended target map

Keep each container where its *dependencies* are. The map has changed three times since
the 2026-09-27 survey: the 2026-10-01 evening pass moved the edge back onto `i1`, the
2026-10-02 pass sent the edge — and `terminal`, `vault`, `vpn` with it — to `i4`, and the
2026-10-09 pass sent the edge and `terminal` back while `atheniq` and `mail` went the
other way. The deployed estate and this map agree again; what remains is sizing, not
placement (finding 7).

| Host | Belongs there | Reasoning |
|---|---|---|
| **i1** (4 vCPU / 14.9 GiB) | `proxy` `.71`, `terminal` `.22`, `monarch` `.56`, `ontrak` `.21`, `genie-preview` `.24` | `ontrak` is here because the family stack and Ontrak Sync share the box. `monarch` is here for its disk (161 G) and is pinned on purpose. `proxy` is here because this page's own argument — it owns `:80/:443/:53` and every service dials it by LAN address — was right the first time: the 2026-10-02 move overrode it by operator choice, and the 2026-10-09 pass restored it. `terminal` is the door the edge is worked through, so it follows the edge. `genie-preview` is a light preview of `innotel/ontrak-genie`. |
| **i2** (apps, 8 vCPU / 13.1 GiB) | `capstone`, `dev`, `www`, `atlas`, `genesis`, `rizzaura` | The heavy, RAM-hungry set. `dev` (`.74`) is where the projects run — every project's docker stack — and is the target of the standalone-project migration. `genesis` is small and could sit on i3 by this table's logic; it is on i2 by operator choice (2026-10-01). |
| **i3** (light, 8 vCPU / 5.3 GiB) | `distro`, `magnate`, `onyx`, `subscribe`, `signara`, `pi`, stopped `olympus-archive` | Small, self-contained services. Has CPU to spare; only RAM limits it. |
| **i4** (2 vCPU / 7.2 GiB) | `atheniq` `.59`, `mail` `.15`, `vault` `.73`, `vpn` `.43` | `vault` and `vpn` are left from the 2026-10-02 set; `mail` and `atheniq` came from `i1` on 2026-10-09. Every one is dialled by its LAN address, so the host is a placement choice and not a routing one — the A record does not move. This is the smallest host and now the tightest (1.0 GiB available, finding 1), so `atheniq` at 3.09 GiB is a fit by *function*, not by headroom: it is here because the box that gave up the edge was the one with room to take it, and its address had to survive the move. |

### Actions (applied 2026-09-27)

1. **Set limits on the unbounded heavies** (prevents another wedge; no restarts
   needed for `limits.memory`/`limits.cpu`):
   - i2: `atheniq` `limits.memory=6GiB`, `capstone` `limits.memory=6GiB`,
     `development` `limits.memory=4GiB`.
   - i1: `monarch` `limits.cpu=2`, `git` `limits.cpu=2` (protects the 4-vCPU edge
     from a transcode/index burst).
   - i3: `olympus`/`onyx`/`signara` `limits.memory` so a runaway RAG index cannot
     fill the 6 GiB box.
2. **Free i2 headroom** by moving the two light, dependency-free services to i3:
   `docs` (467 MiB) and `ansible` (37 MiB). Optional third: `cloud` (299 MiB).
   That takes i2 from 0.4 GiB free to ~1.2 GiB, and i3 stays under 3 GiB used.
3. **Do not move `monarch` off i1 yet.** It fits neither i2 (0.4 GiB free) nor
   i3 (would need 2.5–3.8 GiB of its 2.6 GiB). If i1's CPU load stays high, the
   real fix is more vCPUs on the pm3 VM, not a relocation. (`git` is retired;
   see the changes above.)
4. **`atheniq` stays on i2.** It caused i3's CPU wedges and needs the RAM; i3's
   5.4 GiB cannot hold it. Moving it to i2 (done 2026-09-27) was correct. Cap it
   (action 1) so its growth cannot fill i2.

### Actions (applied 2026-10-01)

1. **`genesis` moved i3 `.65` → i2 `.66`.** Genesis was created on i3 the same
   day and moved to i2 at the operator's request, to sit with the app host. i2's
   `limits` are caps, not reservations, so the 2 GiB it is allowed adds nothing
   to i2's *floor* — but it does add a consumer to a host that already runs the
   three heaviest containers, so treat i2's ~3 GiB free as the ceiling for the
   next arrival. The i3 container was deleted; i3's data volume was empty, so
   nothing was carried. `genesis` now resolves its two secrets from Cerulean
   Vault (`cerulean/genesis`) with a path-scoped token.
2. **Router reservation follows the address.** The manual router reservation is
   now for `.66`, not `.65` (see `1-primary/genesis/docs/Deployment.md`).

## Open items (as of 2026-10-09)

- ~~Ten running containers still take their address from DHCP.~~ **Done
  2026-10-01:** all ten are pinned in their own network manager, and the check
  reads netplan, systemd-networkd and ifupdown so a container pinned outside
  netplan is still counted as pinned. Keep it that way — a new container arrives
  on DHCP and must be pinned before it is dialled by address.
- **`atheniq` `.59` takes its address over DHCP, and it is dialled by address.** The
  item above says a container must be pinned before it is dialled; this one is not. What
  keeps `.59` is its **MAC** — an explicit Incus `hwaddr` plus `dhcp-identifier: mac` in
  its netplan — which is what survived the move onto `i4`, and which will not survive a
  lease change. This is the state the 2026-10-01 incident came from, which is why
  `check-container-addresses.py` reports it as `not_pinned` (a warning, not a failure)
  on every run. **Fix:** give it the `99-static.yaml` the other containers use
  (`dhcp4: false`, `addresses: [192.168.1.59/24]`, a default route and a nameserver on
  `192.168.1.1`), leave the MAC alone, and re-run the check until the warning is gone.
  `hosts/i4/README.md` has the same note next to its file table.
- **`atheniq`'s `limits.cpu` is 3 on a 2-CPU host, and `ontrak`'s is 4 on a 4-CPU host.**
  Both were set for the host the container used to be on and travelled with the rootfs
  (`ontrak` moved `i4` → `i1` on 2026-10-02, `atheniq` `i1` → `i4` on 2026-10-09). Incus
  clamps the pin, so nothing is broken — but the written cap is not the fenced one, the
  estate's own `check-container-limits.py` says so, and i1's CPU caps summing to 11 vCPU
  on 4 is the defect the 2026-10-01 pass removed (finding 5). Right-size both against
  the host each is on now.
- **pm4 (host of i3) has only ~1.4 GiB RAM free**, so i3 cannot be grown in place;
  any move of a heavy onto i3 needs RAM added to pm4, or a fourth host.
- **i2's root filesystem is 48 % (12 G free)** at the 2026-10-02 refresh — down from
  72 % before the `.46` migration, so the earlier concern is resolved; watch it as the
  standalone-project migration fills `dev` (`.74`).
- ~~The edge services are spread across hosts.~~ **Done 2026-10-01 (evening), redrawn
  2026-10-02, and redrawn back 2026-10-09:** the edge (`proxy`) and `terminal` are on
  `i1`; `vault` and `vpn` stayed on `i4` and were joined there by `atheniq` and `mail`.
  Every container kept its address, because the address is declared inside the
  container — except `atheniq`, which keeps it by MAC (see the DHCP item above). The
  target map and the estate agree.
- ~~i1's CPU caps sum to twice the host.~~ **Done 2026-10-01 (evening), over the host
  again since 2026-10-09:** the caps were 24 vCPU on a 4-vCPU host and were set so no
  container can claim the whole host (largest cap 2). They are now **11 vCPU on 4**,
  because the containers that moved brought their caps with them — `ontrak` 4 is the
  whole host — and `i4` landed the same way at 7 vCPU on 2. See the caps item above and
  finding 5. **pm3 does not need more vCPUs** — i1 was never saturated (finding 2);
  revisit only if a real CPU constraint appears.
- ~~i1's new CPU caps are pending a restart.~~ **Done 2026-10-01:** Incus applies
  `limits.cpu` live and implements it as a cpuset, not a quota, so no restart was
  needed and none is pending. Verify a cap with
  `cat /sys/fs/cgroup/lxc.payload.<c>/cpuset.cpus.effective` (count the CPUs it
  names) — **not** `cpu.max`, which stays `max` because there is no quota.
- **The scheduled checks have a single runner.** They run on the Cerulean edge
  (`proxy`, an `i1` container again since 2026-10-09): if the edge cannot read a host the
  `EstateCheckCouldNotRun` rule fires, and if a check stops running entirely
  `EstateCheckStale` does. If the whole edge is down, nothing on the edge reports at
  all — so that one case is watched from **off** the edge: `scripts/check-edge-liveness.py`
  runs under `systemd/edge-liveness.{service,timer}` every five minutes and TCP-connects
  to the edge's own doors (`:80`/`:443`). It exits non-zero and can `--notify` when they
  stop answering, which is the one report a host cannot make about itself.
- **The off-edge watcher is no longer off the edge.** It is installed on `i1`, and the
  2026-10-09 pass moved the edge back onto `i1` as well — so the one checker whose whole
  point is that it does not run where the edge runs does. It still catches an edge
  *ingress* failure (a dead NPM, Authentik or Docker), which is most of what it has ever
  caught, but a host failure now takes the watcher and the edge out together, which is the
  case it exists for. **Fix:** install the script and
  `systemd/edge-liveness.{service,timer}` on a non-edge host (`i4` gave the edge up on
  2026-10-09 and is the obvious one) and remove them from `i1`, or move the edge off
  `i1` again. `systemd/edge-liveness.service` and `scripts/check-edge-liveness.py` say
  this at the top, so the next reader does not have to derive it.
- **The in-container network comments describe the 2026-10-02 topology.** `vault` and
  `vpn` say "this host has no LAN bridge"; `proxy` and `terminal` still name the
  `169.254.0.1` hop as their next step. No *directive* is wrong — every address and
  gateway matches what is deployed — but a comment that describes a topology the host no
  longer has is how the next reader gets it backwards. It is a comment-only edit inside
  four containers, and it is written down here rather than done in the same pass as the
  move because nothing else was being restarted. The ssh key the checks use (`/root/.ssh/container-address`) is authorized
  `from="192.168.1.71"` only and lives on the edge, and a host is only readable when
  the key is in its `authorized_keys` **and** the host is named in the edge's
  `/root/.ssh/config`. `i4` was missing both, which is why the inventory check reported
  the bench unreachable until 2026-10-02. Those two files are installed (and their drift
  reported) by `scripts/trust-estate-hosts.py`, so restoring them is a command rather
  than a memory. The scripts and units live at `/opt/innotel/estate-checks/` and
  `/etc/systemd/system/estate-checks.*` there, and the watcher at
  `/opt/innotel/edge-liveness/` with `/etc/systemd/system/edge-liveness.*` on `i1`; the
  platform-stack copies are the source.
- ~~The liveness watcher's outward channel is not wired.~~ **Done 2026-10-02:**
  `check-edge-liveness.py` now mails the failure through the estate's own mail server
  (`--mail-to`, sent via `192.168.1.15:25`) as well as exiting non-zero; `i1` sets
  `EDGE_LIVENESS_MAIL_TO=admin@innotel.us` in `/etc/innotel/edge-liveness.env`, and a
  simulated dark edge delivered. `--notify` is still there for another channel. Its
  textfile is written to `/var/lib/node_exporter/textfile/edge-liveness.prom` on `i1`,
  which no collector reads today; a node-exporter pointed at that directory would make it
  scrapable.
- **The estate's one Alertmanager delivers — resolved 2026-10-02.** `signara`
  (i3) runs `signara-alertmanager-1`, which had **empty** `SMTP_HOST`, `SMTP_USER`,
  `SMTP_PASS` and `ALERT_EMAIL_TO` — the exact state its own config comment warns
  about ("a receiver that looks configured, validates, and delivers nowhere",
  `amtool check-config` says SUCCESS on it). It now points at the estate's own
  Stalwart (`192.168.1.15:25`, unauthenticated LAN sender) and mails
  `admin@innotel.us`; verified by injecting an alert through the v2 API and
  reading the 250 delivery in `/var/log/stalwart/stalwart.2026-10-02`.
  Two failures had to be fixed first, neither visible to `check-config`: the
  relay refused Alertmanager's default `EHLO localhost` (`550 Invalid EHLO
  domain`) and then its self-signed certificate that names no address (`x509:
  cannot validate certificate for 192.168.1.15`). Signara's receiver now takes an
  `SMTP_HELLO` (a dotted EHLO name) and an `SMTP_TLS_INSECURE` (keep `STARTTLS`,
  skip verification, off by default) — commit `d6d8ad1`. Its port stays
  loopback-only (`127.0.0.1:9093` on `i3`), so the edge-dark watcher on `i1`
  still mails through the mail server directly rather than POSTing to it.
  A receiver that delivers today can go silent tomorrow (a changed relay, an expired
  path), so delivery is not left as a one-off proof: `check-alert-delivery.py` re-proves
  it every run — see §*Receiving alerts is now checked, not assumed*.
- **The signing domain's mail is authenticated now — 2026-10-03.** Alertmanager's
  receiver delivers through the estate's Stalwart, but mail from `signara.innotel.us`
  (the domain Signara signs as) had no SPF, DKIM or DMARC at all — the domain existed
  only as web names. `signara.innotel.us` is now a Stalwart domain (managed with
  `admin@innotel.us`) with automatic DKIM, and its SPF, DKIM (ed25519 + rsa) and
  DMARC records are published in the zone the public actually sees — **Technitium on
  the edge (`.71`)**, the server `ns1/ns2.innotel.us` answer for, *not* the older BIND
  on `www` (`.80`, a separate copy of the zone the LAN resolver reads). Verified live
  with `dig` against `.71` and the public resolver. The parent domain's own DKIM keys
  (Stalwart had generated them but never published them) are live too, so mail sent
  as `@innotel.us` verifies.
  Three operational facts, all learned the hard way:
  - **Stalwart publishes its DKIM records to Technitium itself now — 2026-10-03.** It
    used to do a TSIG dynamic update at `192.168.1.80` (the older BIND copy of the
    zone), which allowed only `key "cerulean"`, so every `DnsManagement` task failed and
    the keys stayed `pending`. That is fixed at both ends: Technitium's `innotel.us`
    zone now accepts dynamic updates from the `cerulean` key (hmac-sha256) under an
    update security policy that permits only `TXT` records at `*._domainkey.innotel.us`,
    and Stalwart's `DnsServer` points at `192.168.1.71` with that key. Publishing is
    narrowed to DKIM (`publishRecords.dkim` only) as well, so SPF, DMARC, MX and the rest
    are left exactly as published. Verified live 2026-10-03: the `innotel.us` keys
    rotated to `v1-*-20261003` and appeared in Technitium within the polling interval,
    one SOA bump and no other record touched. `check-mail-auth.py` still reads the public
    zone every 30 minutes, so a rotation that fails to publish stays a finding.
  - **The mail server's management plane has a credential again.** Stalwart v0.16
    honours `STALWART_RECOVERY_ADMIN=admin:…` while running normally, so it is pinned
    in `/etc/stalwart/stalwart.env` (0640) on `mail` — the management login for
    `http://127.0.0.1:8080/jmap/`. It replaced a management plane with no known
    credential.
  - **Technitium's console password was reset** (its stored password no longer
    matched) by removing `auth.config` and restarting `cerulean-technitium`; the live
    admin password now matches `TECHNITIUM_ADMIN_PASSWORD` in Cerulean's `.env`, so
    Cerulean's own DNS calls work again. A brief DNS blip, then healthy.
  - **The authenticated submission path exists now — 2026-10-03.** Stalwart refuses to
    *relay* unauthenticated external mail (`550 Relay not allowed`), so the alert
    receiver was moved off its unauthenticated `:25` hop. Two config objects were added
    to Stalwart (via the JMAP management API, so they live in the config store and
    survive restarts): a `NetworkListener` named `submission` bound to `[::]:587`
    (SMTP + STARTTLS, `tlsImplicit false`) and a **User** account
    `alertmanager@innotel.us` in the `innotel.us` domain carrying one `Password`
    credential. The listener id is load-bearing: Stalwart decides submission-vs-inbound
    from the listener *name* (`!= "smtp"` means submission, i.e. require auth), not the
    port. Signara's `.env` on i3 now sets `SMTP_PORT=587`, `SMTP_USER`/`SMTP_PASS` for
    that account (keeping `SMTP_HELLO=signara.innotel.us` and `SMTP_TLS_INSECURE=true`
    for Stalwart's self-signed cert), and `signara-alertmanager-1` was recreated.
    Verified live with `scripts/check-alert-delivery.py` and the `mail` log:
    `auth.success listenerId = "submission", localPort = 587 … accountName =
    "alertmanager@innotel.us"`, EHLO `signara.innotel.us`, then
    `Delivery completed … from = "alertmanager@innotel.us", to = ["admin@innotel.us"]`.
    Adding the listener needed `systemctl restart stalwart` on `mail` to bind it — a
    `ReloadSettings` action reloads settings but does not rebind listeners.
  - **The estate still cannot deliver to external MTAs directly.** The ISP blocks
    outbound TCP/25 estate-wide (`gmail-smtp-in` is unreachable on `:25` from `i1`,
    `mail` and the edge, while `:587` works), so `remote`-queue mail is accepted,
    authenticated and queued but cannot leave. Customer-facing send therefore needs a
    smarthost that speaks 587/465, not direct-to-MX. The alert path is unaffected: it
    delivers to a local mailbox (`admin@innotel.us`) over the `local` queue.
- **The move shifted disk the other way: i1 is back to 13 % and `i4` took the
  images.** The four rootfs copies added tens of GB to `i4`'s `dir` pool (stored
  uncompressed, so larger than the ZFS `USED` figures they came from); `i4`'s root
  peaked at **51 %** and is **39 %** after pruning the dangling images and build cache
  (6.8 GB) and then the tagged-but-unreferenced images (5.5 GB more). `i1` fell
  back to 13 % once its four sources were deleted. `dev` (`.74`) growth on i2 remains
  the one to watch.
