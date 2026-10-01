# Incus container placement — i1 / i2 / i3

Surveyed 2026-09-27; refreshed 2026-10-01. Three Incus hosts run every estate
container. This page records what each host is, what currently sits on it, and
where it should sit.

The 2026-10-01 refresh matters because the estate's *shape* changed, not just its
numbers: the pre-migration containers on `.46` (`atheniq`, `cloud`, `voice`,
`docs`, `ansible`, `slack`, `olympus`/`olympus-gw`) are gone, and the edge
services that used to live on i1 now live on i2 and i3. The tables below are the
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

Values are the 2026-10-01 (18:51 EDT) survey.

| Host | Address | vCPU | RAM | Load at survey | RAM available | Root disk | Pool |
|---|---|---|---|---|---|---|---|
| **i1** | `.51` | 4 | 14.9 GiB | 1.67 / 2.51 / 3.96 | **5.3 GiB** | 98 G (13 %) | `incus` (zfs) |
| **i2** | `.52` | 8 | 13.1 GiB | 0.93 / 1.14 / 2.19 | 4.1 GiB | 23 G (45 %) | `tank` (zfs) |
| **i3** | `.53` | 8 | 5.3 GiB | **0.33** / 0.34 / 0.27 | 2.6 GiB | 23 G (66 %) | `tank` (zfs) |

`i2` and `i3` are the same `tank` profile set (`default`, `docker`, `large`,
`medium`, `small`), so export/import between them works unchanged. Both are
`incus` containers' hosts, reached over SSH as `root` with the estate's incus
root password; `i2` runs on pm3 (VM200), `i3` on pm4 (VM200).

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
| i1 | `monarch` | `.56` | media (Jellyfin etc.) | **4.91 GiB** (6 GiB) | 2 |
| i1 | `ontrak` | `.21` | Ontrak family stack + Ontrak Sync | 815 MiB (2 GiB) | 2 |
| i1 | `proxy` | `.71` | **the Cerulean edge** — NPM, Authentik, Vault, Technitium, metrics | **3.35 GiB** (5 GiB) | 4 |
| i2 | `atlas` | `.90` | Gitea + convex + postgres + dashboard | 450 MiB (1 GiB) | — |
| i2 | `capstone` | `.30` | Zeus / capstone telephony | **5.96 GiB** (6 GiB) | — |
| i2 | `dev` | `.74` | **the `dev` container** — every project's docker stack | 1.05 GiB (4 GiB) | 4 |
| i2 | `genesis` | `.66` | BusinessOps — intake + assisted EIN filing | 177 MiB (1024 MiB) | 2 |
| i2 | `rizzaura` | `.62` | Rizz Aura (5 svc) | 215 MiB (512 MiB) | — |
| i2 | `terminal` | `.22` | web terminal | 374 MiB (1024 MiB) | — |
| i2 | `vault` | `.73` | Vaultwarden + Linkwarden + Meilisearch | 943 MiB (2048 MiB) | 4 |
| i2 | `www` | `.80` | public website (+ `tun0`) | 552 MiB (1536 MiB) | 4 |
| i3 | `acme` | `.49` | ACME / certificate issue — **STOPPED** | — (1 GiB) | — |
| i3 | `distro` | `.61` | distro control plane | 165 MiB (1 GiB) | — |
| i3 | `magnate` | `.57` | Magnate | 225 MiB (1 GiB) | — |
| i3 | `mail` | `.15` | mail (SMTP/IMAP) | 197 MiB (1 GiB) | — |
| i3 | `olympus-archived-20260930` | — | archived Olympus — **STOPPED** | — (1 GiB) | — |
| i3 | `onyx` | `.60` | Onyx (RAG) | 260 MiB (1 GiB) | — |
| i3 | `patchmon` | `.108` | patchmon — **STOPPED** | — (2 GiB) | 2 |
| i3 | `pi` | `.70` | pi | 174 MiB (1536 MiB) | 4 |
| i3 | `signara` | `.44` | Signara | 563 MiB (1 GiB) | — |
| i3 | `subscribe` | `.58` | Subscribe | 90 MiB (1 GiB) | — |
| i3 | `vpn` | `.43` | WireGuard (`10.7.0.2` wg0) | 229 MiB (1024 MiB) | 4 |

`atheniq`, `cloud`, `voice`, `docs`, `ansible`, `slack` and `olympus`/`olympus-gw`,
which the 2026-09-27 survey listed, are **no longer present on any host** — those
repositories were retired with the `.46` migration. The container names are kept
here only so the two surveys can be read against each other.

## Findings

1. **No host is under memory pressure any more — the 2026-09-27 findings are
   spent.** i2, the survey-day bottleneck at 0.4 GiB free, has **4.1 GiB
   available**; i1 has 5.3 GiB and i3 has 2.6 GiB. The `.46` migration (which
   retired `atheniq`, `cloud`, `voice`, `docs`, `ansible`, `slack`) is what freed
   i2, and it is the reason this refresh exists.
2. **i1 carries the least.** Three containers, 5.3 GiB available — more headroom
   than either app host. But only 4 vCPU, and `monarch` (media transcoding) is its
   CPU hog: CPU, not memory, is i1's constraint.
3. **i2 is now the app host and the busiest by count.** Eight containers and
   9.2 GiB used, but 4.1 GiB free and load 0.93 on 8 vCPU. `capstone` alone is
   5.96 GiB of the 9.2 — 65 % of the host's usage in one container.
4. **i3 is CPU-idle but is the smallest host.** Load 0.33 on 8 vCPU, and it now
   carries 8 running containers on only 5.3 GiB (2.6 GiB available). It has the
   CPU the others lack and the RAM the others have to spare.
5. **The caps oversubscribe every host** — i1 13 GiB of caps on 14.9 GiB, i2
   17 GiB on 13.1 GiB, i3 **13.5 GiB on 5.3 GiB**. That is deliberate (a cap is a
   per-container runaway guard, not a reservation) and safe for memory, but it
   means the caps cannot be read as a placement budget: nothing here is protected
   *collectively*, only individually. `proxy` is capped at 4 vCPU on a 4-vCPU host
   and `monarch`+`ontrak` add 4 more, so i1's CPU caps sum to twice the host.
6. **No running container's address can drift.** Every container the check covers
   is now static in its own manager, so a recreation cannot renumber it — the
   condition that produced the 2026-10-01 outage is closed. Re-run
   `scripts/check-container-addresses.py` after any container is recreated: it
   reports 0 failures and 0 warnings on 2026-10-01, and will name the first
   container that regresses.
7. **No host can absorb the two heavies.** `monarch` (4.91 GiB) fits neither i2
   (4.1 GiB free) nor i3 (5.3 GiB total); `capstone` (5.96 GiB) fits nowhere but
   i2. So the heavies stay where they are, and any real rebalance is a *light*
   container moving to meet idle CPU, or more RAM on pm4.

## Recommended target map

Keep each container where its *dependencies* are. After the `.46` migration the
target map is close to what is deployed — the remaining moves are *light*
containers meeting idle CPU, not heavies meeting RAM (finding 7).

| Host | Belongs there | Reasoning |
|---|---|---|
| **i1** (edge, 4 vCPU / 14.9 GiB) | `proxy` `.71`, `monarch` `.56`, `ontrak` `.21` | `proxy` is network ingress and identity — it owns `:80/:443/:53` and every service dials it by LAN address, so it is topology-bound. `ontrak` is here because the family stack and Ontrak Sync share the box. `monarch` is here for its disk (161 G) and is pinned on purpose; it is the reason i1 is CPU-bound. |
| **i2** (apps, 8 vCPU / 13.1 GiB) | `capstone`, `dev`, `www`, `vault`, `terminal`, `atlas`, `genesis`, `rizzaura` | The heavy, RAM-hungry set. `dev` (`.74`) is where the projects run — every project's docker stack — and is the target of the standalone-project migration. `genesis` is small and could sit on i3 by this table's logic; it is on i2 by operator choice (2026-10-01). |
| **i3** (light, 8 vCPU / 5.3 GiB) | `distro`, `magnate`, `mail`, `onyx`, `subscribe`, `signara`, `pi`, `vpn`, stopped `acme`/`patchmon`/`olympus-archive` | Small, self-contained services and the identity-adjacent bits (`mail`, `vpn`). Has CPU to spare; only RAM limits it. |

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
- **The edge services are spread across hosts.** The 2026-09-27 target map kept
  `vault`/`terminal`/`acme`/`mail`/`vpn` on i1 (they dial the edge by LAN address);
  they now sit on i2 (`vault`, `terminal`) and i3 (`acme`, `mail`, `vpn`). i1 has
  the headroom to take them back (5.3 GiB free) and doing so restores the documented
  topology — but each move changes a container's host, so it is a decision rather
  than a cleanup.
- **i1's CPU caps sum to twice the host** (`proxy` 4 + `monarch` 2 + `ontrak` 2 on
  4 vCPU). Not wrong — a cap is a ceiling — but it means i1's real constraint is
  vCPU, and the durable fix is more vCPUs on pm3, not another cap.
