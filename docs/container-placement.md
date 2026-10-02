# Incus container placement — i1 / i2 / i3

Surveyed 2026-09-27; refreshed 2026-10-01 (a morning survey and an evening
rebalance). Three Incus hosts run every estate container. This page records what
each host is, what currently sits on it, and where it should sit.

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
  rest.** `limits.cpu` takes effect at a container's next restart, and none has
  restarted since these were set — `cpu.max` still reads `max` for the running
  containers — so each cap takes hold on that container's next restart. Until then
  i1 runs unthrottled, which is what the evening load shows.
- **The address invariant is now scheduled and alerting.**
  `systemd/container-address-check.{service,timer}` runs
  `scripts/check-container-addresses.py` every 30 minutes *on the Cerulean edge*
  and writes `container-address.prom` into node-exporter's textfile directory;
  `extensions/monitoring/prometheus/rules/container-address.yml` alerts on it
  (`ContainerAddressMoved`, `ContainerAddressCheckCouldNotRun`,
  `ContainerAddressCheckStale`, `ContainerAddressNotPinned`). The edge reaches the
  three hosts with a dedicated ssh key, `/root/.ssh/container-address`, authorized
  `from="192.168.1.71"` only, so the scheduled run needs no password on disk. A
  check that cannot reach a host publishes status 0 rather than nothing, because
  "no data" is how the outage stayed invisible. Verified running 2026-10-01.

## Changes applied 2026-09-27

- **Limits set** on the unbounded heavies: i2 `atheniq`/`capstone` = 6 GiB,
  `development` = 4 GiB; i1 `monarch`/`git` = 2 vCPU; i3 `olympus`/`onyx`/
  `signara` = 1 GiB. The `limits.memory` values apply live; **`limits.cpu`
  takes effect on the container's next restart** (i1's load is no longer CPU
  pressure anyway — see below).
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

## The three hosts

Values are the 2026-10-01 evening (21:00 EDT) survey, after the move.

| Host | Address | vCPU | RAM | Load at survey | RAM available | Root disk | Pool |
|---|---|---|---|---|---|---|---|
| **i1** | `.51` | 4 | 14.9 GiB | 2.73 / 12.27 / 14.55 † | 3.9 GiB | 98 G (14 %) | `incus` (zfs) |
| **i2** | `.52` | 8 | 13.1 GiB | 0.54 / 0.68 / 0.89 | 4.5 GiB | 23 G (45 %) | `tank` (zfs) |
| **i3** | `.53` | 8 | 5.3 GiB | **0.18** / 0.30 / 0.23 | 3.0 GiB | 23 G (67 %) | `tank` (zfs) |

† i1's load is `monarch`'s media stack, not the move: qbittorrent and the *arr
apps are the top CPU consumers, and the load stayed up after the copies finished.
It is also the clearest evidence that `limits.cpu` is not yet in force — a CPU cap
applies at a container's next restart, and none has restarted since the caps were
set (see the evening notes). The pre-move reading was 0.33 / 1.39 / 2.59.

`i2` and `i3` are the same `tank` profile set (`default`, `docker`, `large`,
`medium`, `small`), so export/import between them works unchanged. Both are
`incus` containers' hosts, reached over SSH as `root` with the estate's incus
root password; `i2` runs on pm3 (VM200), `i3` on pm4 (VM200), and i1 is another guest on the
same Proxmox host as i2 — which is why "more vCPUs on pm3" is the way i1's CPU
would be grown, a decision the evening pass made and declined (finding 2).

That password is **not written down here** — golden rule 4 (no credential in any
repo file) applies to documentation as much as to code, and a literal in a doc
ships in every clone. A provisioning script takes it from the environment or from
its own gitignored `.env` instead (`ONTRAK_INCUS_PASSWORD` in Ontrak Sync's
`scripts/setup.sh` is the reference implementation).

## What is on each host now

Memory is live usage; disk is the container rootfs.

Memory is the container's live usage and the cap set on it (`limits.memory` / `limits.cpu`).

| Host | Container | IP | Role | Mem (cap) | CPU cap |
|---|---|---|---|---|---|
| i1 | `acme` | `.49` | ACME / certificate issue — **STOPPED** | — (1 GiB) | 1 |
| i1 | `mail` | `.15` | mail (SMTP/IMAP) | 316 MiB (1 GiB) | 1 |
| i1 | `monarch` | `.56` | media (Jellyfin etc.) | 3.76 GiB (6 GiB) | 2 |
| i1 | `ontrak` | `.21` | Ontrak family stack + Ontrak Sync | 671 MiB (2 GiB) | 1 |
| i1 | `proxy` | `.71` | **the Cerulean edge** — NPM, Authentik, Vault, Technitium, metrics | 3.04 GiB (5 GiB) | 2 |
| i1 | `terminal` | `.22` | web terminal (termix, guacd, zapit) | 445 MiB (1024 MiB) | 1 |
| i1 | `vault` | `.73` | Vaultwarden + Linkwarden + Meilisearch | 916 MiB (2048 MiB) | 1 |
| i1 | `vpn` | `.43` | WireGuard (`10.7.0.2` wg0) | 319 MiB (1024 MiB) | 1 |
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
| i3 | `patchmon` | `.108` | patchmon — **STOPPED** | — (2 GiB) | 2 |
| i3 | `pi` | `.70` | pi | 176 MiB (1536 MiB) | 4 |
| i3 | `signara` | `.44` | Signara | 562 MiB (1 GiB) | — |
| i3 | `subscribe` | `.58` | Subscribe | 88 MiB (1 GiB) | — |

`atheniq`, `cloud`, `voice`, `docs`, `ansible`, `slack` and `olympus`/`olympus-gw`,
which the 2026-09-27 survey listed, are **no longer present on any host** — those
repositories were retired with the `.46` migration. The container names are kept
here only so the two surveys can be read against each other.

## Findings

1. **Memory pressure is spent, and i1 is now the fuller host.** i1 has gone from
   three containers and 5.3 GiB available to eight and **3.9 GiB** after taking the
   edge back; i2 has **4.5 GiB** and i3 **3.0 GiB**. The `.46` migration (which
   retired `atheniq`, `cloud`, `voice`, `docs`, `ansible`, `slack`) is what freed
   i2, and the 2026-10-01 rebalance is what redrew i1. No host is tight, but i1 is
   no longer the emptiest.
2. **i1 carries the edge again, and it was never CPU-bound.** Eight containers,
   but seven of them are the edge and `monarch`. The survey-day 1.67 load on 4 vCPU
   was not saturation, and the evening spike is `monarch`'s media stack
   (qbittorrent and the *arr apps) — exactly the container `limits.cpu=2` exists to
   fence, except that cap is not yet in force (see the evening notes). Unthrottled,
   i1 absorbed it and the edge stayed up. **pm3 therefore does not need more
   vCPUs:** its guest i1 has never been provisioned to its 4, and i2's load is 0.89
   across 8. The cap fix below is what was actually wrong.
3. **i2 is the app host and the busiest by container size.** Six containers and
   8.9 GiB used, but 4.5 GiB free and load 0.89 on 8 vCPU. `capstone` alone is
   5.84 GiB of the 8.9 — 66 % of the host's usage in one container.
4. **i3 is CPU-idle and the smallest.** Load 0.18 on 8 vCPU, and after giving the
   edge back it carries four running containers on 5.3 GiB (3.0 GiB available). It
   has the CPU the others lack and the RAM the others have to spare.
5. **i1's CPU caps were the real defect, and they are fixed; the memory caps still
   oversubscribe on purpose.** i1's CPU caps summed to **24 vCPU on a 4-vCPU host**
   (`proxy` alone was capped at the whole host), so a runaway there could starve
   everything else. They are now `proxy` 2, `monarch` 2 and 1 on each of the other
   six: the largest cap is **2**, so a runaway always leaves at least two vCPU for
   the rest. Memory caps still oversubscribe every host — i1 19 GiB of caps on
   14.9 GiB, i2 14 GiB on 13.1 GiB, i3 6.5 GiB of running caps on 5.3 GiB — which
   is deliberate: a memory cap is a per-container runaway guard, not a reservation.
6. **No running container's address can drift — and the check now watches.** Every
   container is static in its own manager, so a recreation cannot renumber it, and
   the scheduled check on the Cerulean edge re-runs every 30 minutes and alerts on
   drift, on a run that could not reach a host, and on a run that has stopped
   succeeding. It reports 0 failures and 0 warnings on 2026-10-01 and will name the
   first container that regresses.
7. **No host can absorb the two heavies.** `monarch` (3.76 GiB) fits neither i2
   (4.5 GiB free, but it is the app host) nor i3 (5.3 GiB total); `capstone`
   (5.84 GiB) fits nowhere but i2. So the heavies stay where they are, and any real
   rebalance is a *light* container moving to meet idle CPU, or more RAM on pm4.

## Recommended target map

Keep each container where its *dependencies* are. The 2026-10-01 evening pass
applied the one move this map called for — the edge back onto i1 — so the target
map and the deployed estate now agree. What remains is sizing, not placement: the
only moves left are *light* containers meeting idle CPU, not heavies meeting RAM
(finding 7).

| Host | Belongs there | Reasoning |
|---|---|---|
| **i1** (edge, 4 vCPU / 14.9 GiB) | `proxy` `.71`, `monarch` `.56`, `ontrak` `.21`, `mail` `.15`, `vpn` `.43`, `terminal` `.22`, `vault` `.73`, stopped `acme` `.49` | `proxy` is network ingress and identity — it owns `:80/:443/:53` and every service dials it by LAN address, so it is topology-bound. `mail`, `vpn`, `terminal`, `vault` and `acme` are the edge services, moved back here on 2026-10-01 to match the documented topology. `ontrak` is here because the family stack and Ontrak Sync share the box. `monarch` is here for its disk (161 G) and is pinned on purpose. |
| **i2** (apps, 8 vCPU / 13.1 GiB) | `capstone`, `dev`, `www`, `atlas`, `genesis`, `rizzaura` | The heavy, RAM-hungry set. `dev` (`.74`) is where the projects run — every project's docker stack — and is the target of the standalone-project migration. `genesis` is small and could sit on i3 by this table's logic; it is on i2 by operator choice (2026-10-01). |
| **i3** (light, 8 vCPU / 5.3 GiB) | `distro`, `magnate`, `onyx`, `subscribe`, `signara`, `pi`, stopped `patchmon`/`olympus-archive` | Small, self-contained services. Has CPU to spare; only RAM limits it. |

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

## Open items (as of 2026-10-01)

- ~~Ten running containers still take their address from DHCP.~~ **Done
  2026-10-01:** all ten are pinned in their own network manager, and the check
  reads netplan, systemd-networkd and ifupdown so a container pinned outside
  netplan is still counted as pinned. Keep it that way — a new container arrives
  on DHCP and must be pinned before it is dialled by address.
- **pm4 (host of i3) has only ~1.4 GiB RAM free**, so i3 cannot be grown in place;
  any move of a heavy onto i3 needs RAM added to pm4, or a fourth host.
- **i2's root filesystem was 45 % (12 G free)** at this refresh — down from 72 %
  before the `.46` migration, so the earlier concern is resolved; watch it as the
  standalone-project migration fills `dev` (`.74`).
- ~~The edge services are spread across hosts.~~ **Done 2026-10-01 (evening):**
  `vault`, `terminal`, `acme`, `mail` and `vpn` are back on i1, each keeping its
  address because the address is pinned inside the container. The target map and the
  estate now agree.
- ~~i1's CPU caps sum to twice the host.~~ **Done 2026-10-01 (evening):** the caps
  were 24 vCPU on a 4-vCPU host and are now `proxy` 2, `monarch` 2 and 1 on the
  rest, so no container can claim the whole host. **pm3 does not need more vCPUs**
  — i1 was never saturated (finding 2); revisit only if a real CPU constraint
  appears.
- **i1's new CPU caps are pending a restart.** They are set in config but not in
  force: `limits.cpu` applies at a container's next restart, and the containers were
  restarted by the move *before* the caps were set. Verify with
  `incus config get <c> limits.cpu` against `cpu.max` inside the container's cgroup
  (`/sys/fs/cgroup/lxc.payload.<c>/cpu.max`) after any future restart of i1's
  containers.
- **The scheduled check has a single runner.** It runs on the Cerulean edge
  (`proxy`), which is itself an i1 container: if the edge is down, the check cannot
  report, and the `ContainerAddressCheckStale` rule is what catches that. The ssh
  key it uses (`/root/.ssh/container-address`) is authorized `from="192.168.1.71"`
  only and lives on the edge; rotate it with the host inventory if the edge moves.
- **i1's root disk grew to 14 %** after the edge moved back — the moved rootfs
  images added several GB (the `vpn` image alone is 3.35 GB). Comfortable on a 98 G
  disk, but it is the number that moved, and `dev` (`.74`) growth on i2 remains the
  one to watch.
