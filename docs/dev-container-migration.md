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
- Remaining: **atlas**, **rizzaura**, **olympus gateway**, **magnate**,
  **clipbucket**, **omniroute**.

## What runs inside `.46` today

19 containers across 8 Compose projects:

| Project | Containers | Published | Public names (NPM → `.46`) |
|---|---|---|---|
| **atlas** | `atlas-gitea` (3004, 2222), `atlas-convex` (3210-3211), `atlas-convex-dashboard` (6791), `atlas-gitea-db` | `0.0.0.0:3004`, `0.0.0.0:3210-3211`, `0.0.0.0:2222` | `atlas.innotel.us`, `git.innotel.us`, `gitlab.innotel.us`, `git.atlas.innotel.us` (→ `:3004`); `convex.innotel.us` (→ `:3210`) |
| **rizzaura** | `rizz-api` (3020), `rizz-community` (3012), `rizz-admin` (3013), `rizz-app` (3021), `rizz-rankings` (3022) | all `0.0.0.0:30xx` | `api.rizz*` `community.rizz*` `admin.rizz*` `app.rizz*` `rankings.rizz*`, `rizzaura.net`, `www.rizzaura.net` |
| **olympus** | `olympus`, `olympus-studio` (3050), `olympus-gateway-sso`, `olympus-gateway-sso-sessions` (16379), `olympus-autoheal` | `:20129` (gateway), `127.0.0.1:3050` | `gateway.studio.innotel.us`, `gateway.olympus.innotel.us` (→ `:20129`) |
| **subscribe** | `subscribe-portal` (3040) | `0.0.0.0:3040` | `subscribe.innotel.us` + 16 `subscribe.*` names |
| **magnate** | `magnate` (3002) | `0.0.0.0:3002` | `app.magnate`, `admin.magnate`, `billing.magnate` |
| **monarch** | `clipbucket` (8098) | `127.0.0.1:8098` | none (internal) |
| **capstone** | `omniroute` (20128) | `127.0.0.1:20128`, `172.17.0.1:20128` | none (internal) |
| **distro** | `distro-control-plane` (20140) | `0.0.0.0:20140` | none — `distro.innotel.us` is served by the **i3** container `.61`, so this one is a duplicate |

Docker on `.46`: 13.6 GB images (11.2 GB reclaimable), **35.9 GB volumes**,
6.6 GB build cache. Most of that travels with the projects.

## Duplicates to reconcile before migrating

These projects already have a *named* Incus container elsewhere, so the `.46`
copy may be the authoritative one or may be dead. Decide per project — do not
migrate blind:

| Project | Also exists as | Serves the public name? |
|---|---|---|
| olympus | i3 `olympus` `.50` (`olympus.innotel.us`, `studio.olympus` → `.50:3050`) | split: the app is on i3, the **gateway** (`:20129`) is on `.46` |
| distro | i3 `distro` `.61` (`distro.innotel.us` → `.61:20140`) | **no** — `.46`'s `distro-control-plane` was a duplicate and is **removed** (2026-09-27) |
| monarch | i1 `monarch` `.56` | no — `clipbucket` on `.46` is internal-only |
| capstone | i2 `capstone` `.30` | no — `omniroute` on `.46` is internal-only |
| atlas / git | i1 `git` `.90` — a **live Gitea** (Postgres + MariaDB, ~16 000 CPU-s) | no NPM host points at `.90`; `git.innotel.us` goes to `.46:3004`. Decide which Gitea wins before touching either |

## Proposed target hosts

Using the placement review (`container-placement.md`): i2 has 8 vCPU but is
memory-tight; i3 has CPU to spare but only 5.4 GiB RAM; i1 is the edge.

| Project | Proposed host | Why |
|---|---|---|
| subscribe | i3 | **done** — `subscribe` container, static `.58` |
| magnate | i3 | small; subscription UI |
| distro-control-plane | — | duplicate; **removed 2026-09-27** |
| atlas (gitea + convex) | i2 | needs a DB + build CPU; retire/reconcile the i1 `git` |
| rizzaura (5 svc) | i2 | active app suite, needs CPU |
| olympus gateway | i3 (next to `olympus` `.50`) | reunite gateway with the app it fronts |
| clipbucket / omniroute | i2 | internal; low priority |

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

1. **Tidy dead weight**: `distro-control-plane` (duplicate), i1 `git` `.90`
   (unused), the stopped *ontrak* volumes, stale capstone volumes, and
   `docker builder prune` (6.6 GB). No service depends on these.
2. ~~**subscribe**~~ — **done 2026-09-27** (its own container on i3, `.58`).
3. **magnate** — single container, live names.
4. **atlas** — gitea + convex + db; watch the git remotes.
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
