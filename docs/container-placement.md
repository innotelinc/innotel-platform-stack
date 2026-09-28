# Incus container placement — i1 / i2 / i3

Surveyed 2026-09-27. Three Incus hosts run every estate container. This page
records what each host is, what currently sits on it, and where it should sit.

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

## The three hosts

| Host | Address | vCPU | RAM | Load at survey | RAM free | Root disk | Pool | Pools' free |
|---|---|---|---|---|---|---|---|---|
| **i1** | `.51` | 4 | 15.3 GiB | 5.93 / 6.81 / 5.34 | 2.6 GiB | 98 G (12 %) | `incus` (zfs) | (monarch 161 G inside) |
| **i2** | `.52` | 8 | 15.5 GiB | 7.26 / 7.42 / 4.29 | **0.4 GiB** | 23 G (72 %) | `tank` (zfs) | 72 G |
| **i3** | `.53` | 8 | 5.4 GiB | **0.30** / 0.23 / 2.06 | 2.6 GiB | 23 G (69 %) | `tank` (zfs) | — |

`i2` and `i3` are the same `tank` profile set (`default`, `docker`, `large`,
`medium`, `small`), so export/import between them works unchanged. Both are
`incus` containers' hosts reached with `DD@l1lama40`; `i2` runs on pm3 (VM200),
`i3` on pm4 (VM200).

## What is on each host now

Memory is live usage; disk is the container rootfs.

| Host | Container | IP | Role | Mem | Disk |
|---|---|---|---|---|---|
| i1 | acme | `.49` | ACME / certificate issue | 105 MiB | 1.7 G |
| i1 | mail | `.15` | mail (SMTP/IMAP) | 201 MiB | 1.8 G |
| i1 | monarch | `.56` | media (Jellyfin etc.) | **3.82 GiB** | **161 G** |
| i1 | proxy | `.71` | **the Cerulean edge** — NPM, Authentik, Vault, Technitium, metrics | 2.66 GiB | 13.3 G |
| i1 | terminal | `.22` | web terminal | 328 MiB | 4.2 G |
| i1 | vault | `.73` | HashiCorp Vault | 819 MiB | 4.3 G |
| i1 | vpn | `.43` | WireGuard | 218 MiB | 2.9 G |
| i2 | atheniq | `.59` | Open edX / Tutor (11 svc) | **4.79 GiB** | 5.5 G |
| i2 | capstone | `.30` | Zeus / capstone telephony | **4.41 GiB** | 23.7 G |
| i2 | cloud | `.146` | cloud storage | 299 MiB | 9.2 G |
| i2 | development | `.46` | **the `dev` container** — every project's docker stack | 2.32 GiB | **156 G** |
| i2 | voice | `.9` | voice | 777 MiB | 11.6 G |
| i2 | www | `.80` | public website (+ `tun0`) | 235 MiB | 12.9 G |
| i3 | distro | `.61` | distro control plane | 130 MiB | 1.2 G |
| i3 | olympus | `.50` | Olympus gateway/studio | 265 MiB | 8.3 G |
| i3 | onyx | `.60` | Onyx (RAG) | 195 MiB | 3.6 G |
| i3 | patchmon | `.108` | patchmon | 202 MiB | 1.2 G |
| i3 | pi | `.70` | pi | 93 MiB | 1.1 G |
| i3 | signara | `.44` | Signara | 516 MiB | 2.8 G |
| i3 | slack | `.33` | Slack bridge | 285 MiB | 4.0 G |
| i3 | docs | `.125` | ONLYOFFICE Docs (moved from i2) | 467 MiB | 2.7 G |
| i3 | ansible | `.35` | Ansible runner + postfix (moved from i2) | 37 MiB | 464 M |

## Findings

1. **i2 is the bottleneck.** It holds the two biggest unbounded consumers
   (`atheniq` 4.79 GiB and `capstone` 4.41 GiB, plus `development` 2.32 GiB) —
   ~11.5 of its 15.5 GiB — and had **0.4 GiB free** at survey. `atheniq` grew
   from 2.8 GiB at boot to 4.79 GiB; nothing caps it.
2. **i3 is idle.** Load 0.30 on 8 vCPU with only 1.7 GiB of containers. It is the
   only host with real CPU headroom — but only 5.4 GiB of RAM, so it cannot take
   the heavies.
3. **i1 is CPU-bound, not memory-bound.** Load ~5.9 on **4 vCPU**; `git`
   (13 970 CPU-s) and `monarch` (media transcoding) are the hogs. 2.6 GiB RAM free.
4. **The wedge history is a placement/limits bug, not a hardware bug.** i3's
   earlier wedges were an *unbounded* `atheniq` saturating the guest CPU. It has
   since been moved to i2 (see below), which is why i3 is quiet now.
5. **The three heaviest containers had no `limits.memory`/`limits.cpu`.**
   `atheniq`, `capstone`, `development` (i2) and `monarch` (i1) — plus the now
   retired `git` — were unbounded; limits were set in the changes above.

## Recommended target map

Keep each container where its *dependencies* are, then fix the two outliers with
limits and a small relocation — no host has enough slack for a wholesale shuffle.

| Host | Belongs there | Reasoning |
|---|---|---|
| **i1** (edge, 4 vCPU) | `proxy` `.71`, `vault` `.73`, `acme` `.49`, `mail` `.15`, `vpn` `.43`, `terminal` `.22` | Network ingress and identity; `proxy` already owns `:80/:443/:53` and every service dials these by LAN address. Topology-bound — do not move. |
| **i2** (apps, 8 vCPU / 16 GiB) | `capstone`, `atheniq`, `development`, `voice`, `www`, `cloud` | The heavy, RAM-hungry, CPU-workload set. `development` (`.46`) is where the projects run and is the target of the standalone-project migration. |
| **i3** (light, 8 vCPU / 6 GiB) | `olympus`, `onyx`, `signara`, `distro`, `slack`, `patchmon`, `pi` **+ `docs`, `ansible`** | Small, self-contained services. Has CPU to spare; only RAM limits it. |

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

## Open items

- pm4 (host of i3) has only ~1.4 GiB RAM free, so i3 cannot be grown in place;
  any real rebalance needs RAM added to pm4, or a fourth host.
- i2's root filesystem is at 72 % (6.3 G free) — unrelated to the pool (`tank`),
  but worth watching as the `.46` migration lands.
