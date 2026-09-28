# Getting the standalone projects off `.46`

`.46` is the i2 container `development` — the estate's dev box. Over time it has
become the *host* for a whole set of unrelated projects, each running as a Docker
Compose project inside it, and each with its public names forwarded to
`192.168.1.46`. That couples every one of those names to a single kernel: a bad
image, a full rootfs (156 GiB) or a runaway stack takes them all down together.

This is the plan to give each project its own Incus container.

## Progress

- **2026-09-27**: `.46` dead weight reclaimed — the `distro-control-plane`
  duplicate, the OnTrak volumes and ~4 GB of build cache removed.
- **2026-09-27**: **`subscribe` migrated** to its own container (`subscribe` on
  i3, static `192.168.1.58:3040`); all 15 `subscribe.*` NPM hosts repointed and
  the `.46` stack removed. The owner script
  (`scripts/subscribe-hosts.py`) now defaults to `192.168.1.71` (edge NPM) and
  `192.168.1.58` (portal).
- **2026-09-27**: **`magnate` migrated** to its own container (`magnate` on i3,
  static `192.168.1.57:3002`); the three NPM hosts (`app`/`admin`/`billing`.
  `magnate.innotel.us`, ids 6/23/53) were repointed from `.46:3002` to
  `.57:3002`, the `.46` stack and image were removed, and `:3002` is no longer
  listening on `.46`. The image was carried over as a saved artifact rather
  than rebuilt (i3 is memory-tight and the Next.js build wants ~3 GB of heap);
  the live data volume was copied while `.46`'s copy was quiescent, so no
  writes were lost in the cut-over and the DB was verified readable after the
  move. `magnate` was a single container with a bind-mounted `./data`
  (SQLite), so nothing else travelled.
- **2026-09-27**: the i1 `git` duplicate was **retired** (it was the
  cryptominer's host; see `security-incident-2026-09-27-git-miner.md`). `.46`
  `atlas-gitea` is the authoritative Gitea and stays where it is.
- **2026-09-27**: **`atlas` migrated** to its own container (`atlas` on i2,
  reusing the freed Gitea address `192.168.1.90`). The four NPM hosts
  (`atlas`/`git`/`gitlab`/`git.atlas` → `:3004`, `convex` → `:3210`) were
  repointed from `.46` to `.90`, and the `.46` stack, its three volumes and its
  images were removed. Gitea + convex + postgres + dashboard moved as images and
  volume tars (the source was copied without the retired Chef builder's
  `node_modules`/`pnpm-store`); the volume copies were quiescent, so no writes
  were lost.
- **2026-09-27**: **`rizzaura` migrated** to its own container (`rizzaura` on
  i2, static `192.168.1.62`); the 11 `*.rizz*` / `*.rizzaura.net` NPM hosts were
  repointed from `.46` to `.62`, and the `.46` stack, its volume, network and
  five images were removed. (The `*.rizzaura.net` names still fail TLS at the
  edge — NPM holds no `rizzaura.net`-zone certificate and those hosts have
  `certificate_id` 0. That is pre-existing, not the move.)
- **2026-09-27**: **`olympus` factory + studio moved** to their own container
  (`olympus-gw` on i3, static `192.168.1.64`). The **gateway SSO proxy**
  (`olympus-gateway-sso` + its redis, `:20128`) deliberately did **not** move:
  it fronts the OmniRoute gateway at `127.0.0.1:20128`, which on `.46` is the
  *capstone* `omniroute` container, and — decisively — about ten consumers
  across the estate dial `192.168.1.46:20128` by address. The proxy therefore
  stays co-located with `omniroute` until that pair moves together. The factory
  and studio now run on i3 and reach the door at `192.168.1.46:20128/v1` (the
  intended cross-host pattern; verified studio → gateway 200). `gateway.olympus`
  and `gateway.studio` still resolve to `.46:20128`.
- **2026-09-27**: `auth.monarch.innotel.us` (NPM host 39) pointed at dead
  `192.168.1.46:9000` and answered **502**; repointed to `.71:9000` (Cerulean
  Authentik), like every other `auth.*` host. After this the only forwards still
  aimed at `.46` are the two gateway names (178/179), and that is deliberate.
- **2026-09-27**: **`.46`'s `clipbucket` retired.** It was a duplicate: i1 `.56`
  (`monarch`) already runs the live `clipbucket` + `clipbucket-sso`
  (`tube.innotel.us` → `.56:14011`) and its `monarch_clipbucket_files` holds 27
  videos against `.46`'s 18 — the same `imported/<title>-<hash>-1080.mp4` names,
  a strict subset, so nothing there was unique. The container and its image were
  removed; **the two volumes (`monarch_clipbucket_files`, 29 GB, and
  `monarch_clipbucket_db`) are retained** as a second copy, because dropping them
  is the only irreversible step and it buys 29 GB on a host with 79 GB free.
- **2026-09-27**: **the OmniRoute door moved from `20129` to `20128`** — the
  gateway's own default port, on `.46`'s LAN address (`GATEWAY_SSO_BIND`).
  `omniroute` still holds `127.0.0.1:20128` and `172.17.0.1:20128`, so the proxy
  binds the LAN address rather than `0.0.0.0`: a wildcard bind overlaps both and
  the proxy would not start. Edge hosts **178/179** repointed to `.46:20128`, and
  every consumer followed in the same change — `olympus` and `olympus-gw`,
  `capstone` (n8n, dashboard-api), `zeus-portal`, `rizz-api`, `onyx-ai`, `distro`,
  plus the repo tooling (`ips/groups/*`, `check-gateway-targets.py` and its tests,
  `scripts/conform-project.sh`). The scanner now fails a stale `20129` by name
  (rule 5) instead of ignoring a port nothing looked at.
- Remaining: **`omniroute`** — which must carry the olympus gateway proxy with it
  (see the note above and `container-placement.md`).

## What runs inside `.46` today

`.46` is down to **3 containers in 2 projects** — `olympus-gateway-sso` and its
redis (they stay because they front `omniroute`, which is loopback-published),
and `omniroute` itself. The table below is the original inventory:

| Project | Containers | Published | Public names (NPM → `.46`) |
|---|---|---|---|
| **atlas** | `atlas-gitea` (3004, 2222), `atlas-convex` (3210-3211), `atlas-convex-dashboard` (6791), `atlas-gitea-db` — **migrated 2026-09-27** to i2 `.90` | `0.0.0.0:3004`, `0.0.0.0:3210-3211`, `0.0.0.0:2222` | `atlas.innotel.us`, `git.innotel.us`, `gitlab.innotel.us`, `git.atlas.innotel.us` (→ `:3004`); `convex.innotel.us` (→ `:3210`) |
| **rizzaura** | `rizz-api` (3020), `rizz-community` (3012), `rizz-admin` (3013), `rizz-app` (3021), `rizz-rankings` (3022) — **migrated 2026-09-27** to i2 `.62` | all `0.0.0.0:30xx` | `api.rizz*` `community.rizz*` `admin.rizz*` `app.rizz*` `rankings.rizz*`, `rizzaura.net`, `www.rizzaura.net` |
| **olympus** | `olympus`, `olympus-studio` (3050), `olympus-autoheal` — **factory+studio migrated 2026-09-27** to i3 `.64`; `olympus-gateway-sso` + `-sessions` stay on `.46` (paired with `omniroute`) | `:20128` (gateway), `127.0.0.1:3050` | `gateway.studio.innotel.us`, `gateway.olympus.innotel.us` (→ `.46:20128`) |
| **subscribe** | `subscribe-portal` (3040) | `0.0.0.0:3040` | `subscribe.innotel.us` + 16 `subscribe.*` names |
| **magnate** | `magnate` (3002) — **migrated 2026-09-27** to i3 `.57` | `0.0.0.0:3002` | `app.magnate`, `admin.magnate`, `billing.magnate` |
| **monarch** | `clipbucket` (8098) — **retired 2026-09-27** (duplicate of i1 `.56`'s live one; both volumes retained) | `127.0.0.1:8098` | none (internal) |
| **capstone** | `omniroute` (20128) | `127.0.0.1:20128`, `172.17.0.1:20128` | none (internal) |
| **distro** | `distro-control-plane` (20140) | `0.0.0.0:20140` | none — `distro.innotel.us` is served by the **i3** container `.61`, so this one is a duplicate |

Docker on `.46`: 13.6 GB images (11.2 GB reclaimable), **35.9 GB volumes**,
6.6 GB build cache. Most of that travelled with the projects. After the
2026-09-27 retirements the volumes that remain are the live `omniroute` data and
the **retained 29 GB `monarch_clipbucket_files` copy** described above — the one
thing still on `.46` that belongs to a project running elsewhere.

## Duplicates to reconcile before migrating

These projects already have a *named* Incus container elsewhere, so the `.46`
copy may be the authoritative one or may be dead. Decide per project — do not
migrate blind:

| Project | Also exists as | Serves the public name? |
|---|---|---|
| olympus | i3 `olympus` `.50` (`olympus.innotel.us`, `studio.olympus` → `.50:3050`) | **resolved 2026-09-27**: the factory + Studio run in their own container (`olympus-gw` i3 `.64`); the `:20128` SSO proxy stays on `.46`, because it fronts `omniroute` (`127.0.0.1:20128`) and the estate dials `192.168.1.46:20128` by address |
| distro | i3 `distro` `.61` (`distro.innotel.us` → `.61:20140`) | **no** — `.46`'s `distro-control-plane` was a duplicate and is **removed** (2026-09-27) |
| monarch | i1 `monarch` `.56` | **no, and `.46`'s was a strict subset** — i1 serves the name (`tube.innotel.us` → `.56:14011`) and holds 27 videos to `.46`'s 18. Retired 2026-09-27. |
| capstone | i2 `capstone` `.30` | no — `omniroute` on `.46` is internal-only |
| atlas / git | i1 `git` `.90` — **retired 2026-09-27** (compromised; PoC repos only) | **resolved** — `.46` `atlas-gitea` (`git.innotel.us`) is authoritative; no NPM host ever pointed at `.90`. See `security-incident-2026-09-27-git-miner.md`. |

## Proposed target hosts

Using the placement review (`container-placement.md`): i2 has 8 vCPU but is
memory-tight; i3 has CPU to spare but only 5.4 GiB RAM; i1 is the edge.

| Project | Proposed host | Why |
|---|---|---|
| subscribe | i3 | **done** — `subscribe` container, static `.58` |
| magnate | i3 | **done** — `magnate` container, static `.57` |
| distro-control-plane | — | duplicate; **removed 2026-09-27** |
| atlas (gitea + convex) | i2 | **done** — `atlas` container, static `.90` |
| rizzaura (5 svc) | i2 | **done** — `rizzaura` container, static `.62` |
| olympus gateway | i3 (factory/studio) | **done** — `olympus-gw` `.64`; the `:20128` SSO proxy **cannot** move alone — it fronts `omniroute` at `127.0.0.1:20128` and ~10 consumers dial `192.168.1.46:20128` |
| clipbucket | — | **done 2026-09-27** — a duplicate, so it was retired rather than moved (volumes retained) |
| omniroute | tbd | the last workload on `.46`; it moves *with* the olympus SSO proxy, and nothing else can be on the gateway's port while it does |

## Method (per project)

1. `incus launch` the same rocky/ubuntu image on the target host, attach the
   matching profile (`default`+`docker`) so it gets a `br0` address.
2. Install Docker + Compose, copy the project directory from
   `/usr/src/projects/complete/<path>` and its named volumes
   (`docker run --rm -v <vol>:/src -v $PWD:/dst alpine cp -a ...`, or a
   `docker volume` tar round-trip).
3. `docker compose up -d` on the new container; verify each service locally.
4. Repoint the NPM proxy hosts from `192.168.1.46` to the new container IP
   (one `PUT` per host, or the Cerulean provisioning script), then verify through
   the edge with `--resolve`.
5. Only then `docker compose down` + remove the stack and its volumes on `.46`,
   and reclaim with `docker system prune` (careful: 11 GB of images).

## Suggested order (least risk first)

1. **Tidy dead weight**: `distro-control-plane` (duplicate), the stopped
   *ontrak* volumes, stale capstone volumes, and `docker builder prune`
   (6.6 GB). No service depends on these. (i1 `git` `.90` was retired
   2026-09-27 — see `security-incident-2026-09-27-git-miner.md`.)
2. ~~**subscribe**~~ — **done 2026-09-27** (its own container on i3, `.58`).
3. ~~**magnate**~~ — **done 2026-09-27** (its own container on i3, `.57`).
4. ~~**atlas**~~ — **done 2026-09-27** (its own container on i2, `.90`).
5. **rizzaura** — 5 containers, the rizz suite.
6. **olympus gateway** — reunite with i3 `olympus`.
7. **clipbucket / omniroute** — internal, last.

## Risks

- **Secrets**: each project's `.env` lives in its checkout; copying the directory
  copies them. Anything in Vault (`vault://`) must still resolve from the new
  container's network position.
- **Volume ownership**: named volumes keep host uid/gid; the new container must
  use the same user mapping or services will see the data as wrong-owner.
- **NPM cut-over**: until every host for a project is repointed, a name can hit
  `.46` for one route and the new container for another — repoint all of a
  project's names together.
- **`.46` is the command host**: migrating does not change that; `.46` stays as
  the dev box, just without the production stacks.
