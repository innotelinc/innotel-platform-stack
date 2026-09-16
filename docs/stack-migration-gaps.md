# The new stacks' service gaps — group composes vs the repos they host

**Status: review — the porting pass landed 2026-09-16** · 2026-09-16

> **Update (2026-09-16).** Recommended actions **3, 4 and 5 are done**, and the
> check behind them passes: `./stack.sh verify all` reports all 18 component
> checkouts present, on their registry repository and on their registry branch.
> Zeus's PBX is in 2-voice (Capstone's copy now `standalone`-gated), PLUTUS,
> the nine `*-sso` gateways, `monarch-seed`/`monarch-init` and the services they
> wire are in 3-media, and 5-dev now carries Olympus + Studio and
> `convex-dashboard`/`certbot` with the stale `chef` deleted. What is **still
> open** is the structural half — actions 1, 2 and 6 — plus the services listed
> as "still absent" in each group below.

The migration moved every platform repo *into* its group dir
(`<n>-<group>/<repo>`, see [`Architecture.md`](Architecture.md)) and gave each
group a `docker-compose.yml` of its own — the file `./stack.sh up <group>` runs.
This page records what those group composes still do **not** carry, compared
with the compose files the repos themselves own.

## How the lists were produced

For each group and each hosted repo, the *active* service set is
`docker compose config --services` — run from the directory that holds the
`.env`, so profiles are resolved exactly as a deployment would: a service behind
`profiles: [legacy]` or `profiles: [npm]` is **not** counted. That matters,
because those are deliberately off, and counting them would hide the real gaps
in a list of services nobody intends to run.

The group compose is read the same way, with the values it marks required
(`OASIS_PG_PASSWORD`, `GITEA_PG_PASSWORD`, `DISTRO_INITIAL_PASSWORD`, …) passed
as placeholders — `config` needs *a* value, not the right one.

## 2-voice — group compose declares 8 (Capstone's PBX now `standalone`-gated)

`capstone-api`, `capstone-freepbx`, `capstone-postgres`, `capstone-redis`,
`consul-reg-g2`, `omniroute`, `zeus-coturn`, `zeus-portal`

| Repo | Declares | Absent from the group compose |
|---|---|---|
| `capstone` | 30 | 29 (below) |
| `zeus` | 3 | `coturn`, `pbx` |

**Real gaps**

- ~~**`pbx`** — Zeus's FreePBX/Asterisk, the platform's actual switchboard.~~
  **Done.** `zeus-freepbx` is now the group's switchboard (Asterisk 22 +
  FreePBX 17 + AvantFax, GHCR image, SIP/RTP/AMI/Webmin ports, the Asterisk +
  MariaDB volumes), `capstone-freepbx` is `profiles: [standalone]` so the two can
  no longer fight over 8083/5060/5061, and `zeus-portal` now gets the
  `FREEPBX_URL`/`AVANTFAX_URL`/`AUTH_MODE` it needs to reach it. The convergence
  doc's ownership (Zeus owns the PBX, `docs/service-audit.md` §1) is what the
  group compose now says too.
- **`coturn`** — present, but only under the name `zeus-coturn`. Not a gap;
  recorded so the diff stops flagging it.
- **`n8n-import`**, **`sandbox-certs`**, **`signoz-schema-migrator`** —
  one-shot initializers. They are gaps for a *migration*, not for a compose: a
  pack that walks containers cannot see a service that never had one, so they
  never travelled in the voice bundle (see "Migration-side gaps" below).
- **`postgres`** / **`redis`** — present as `capstone-postgres` /
  `capstone-redis`. Renamed, not missing.

**Capstone's other 24** (`dashboard`, `dashboard-api`, `dograh-api`,
`dograh-ui`, `grist`, `grist-sso`, `kokoro`, `minio`, `n8n`, `n8n-sso`,
`pbx-sso`, `searxng`, `sandbox-api`, `sandbox-runner-1`, `signoz` +
`signoz-clickhouse` + `signoz-clickhouse-keeper` + `signoz-metastore-postgres` +
`signoz-otel-collector`, `signoz-sso`, `speaches`, `technitium-sso`,
`workflow-sso`, `workflow-studio`) are AgentOps' own sub-stack. They belong to
the group only if group 2 is meant to be the standalone voice box; today they
exist only in Capstone's compose, so `up 2` starts a shell of it.

## 3-media — group compose declares 34 (was 15)

`consul-reg-g3`, `dispatcharr`, `flaresolverr`, `jellyfin`, `jellyseerr`,
`lidarr`, `monarch-api`, `nextpvr`, `prowlarr`, `qbittorrent`, `radarr`,
`sabnzbd`, `sonarr`, `tvheadend`, `watchtower`

| Repo | Declares | Absent from the group compose |
|---|---|---|
| `monarch` | 30 | 20 (below) |
| `plutus` | 4 | **all 4** — `backend`, `web`, `dashboard`, `admin-key-check` |

**Real gaps**

- ~~**PLUTUS is entirely absent.**~~ **Done.** `plutus-backend` (Convex +
  ffmpeg build), `plutus-dashboard`, `plutus-admin-key-check` and `plutus-web`
  are declared, on the ports the port map documents (3000/3210/3211/6791), with
  the repo's `.env.local` and static `out/` wired in and the shared Group-2
  OmniRoute as the model plane.
- ~~**The nine `*-sso` gateways** (`radarr-sso`, `sonarr-sso`, `lidarr-sso`,
  `whisparr-sso`, `bazarr-sso`, `prowlarr-sso`, `qbittorrent-sso`, `sabnzbd-sso`,
  `jellyseerr-sso`)~~ **Done.** All nine are declared, each pointing
  `OAUTH2_PROXY_UPSTREAMS` at its service name on the group network, sharing the
  one `_innotel_sso` cookie and the `x-sso-env`/`x-sso-gateway` anchors ported
  from `monarch/docker-compose.yml`. The apps are no longer LAN-reachable with
  their own local logins.
- **`whisparr`**, **`bazarr`**, **`iptv`** and **`authentik-ldap`** — **Done**:
  they are declared because the gateways above front the first two and
  `monarch-init` configures all four (Whisparr/Bazarr alongside the other *arr
  apps, iptv for the XMLTV guide, the LDAP outpost for Jellyfin's LDAP-Auth
  plugin).
- **Still absent:** **`homarr`**, **`requestrr`**, **`clipbucket`**,
  **`monarch-recs`**, **`monarch-health`**. The first three are optional
  library/support apps; the last two are built from `./ai-recs` and
  `./health-analytics` and need the `JELLYFIN_API_KEY` handoff verified before
  they are worth declaring.
- ~~**`monarch-seed`**, **`monarch-init`**~~ **Done.** Both are declared, and so
  is the mount bridge that makes them work: every app config volume the group's
  services mount is mounted again in `monarch-init` at `/docker/appdata/<app>`,
  the path `init.py` reads, so the initializer edits the live config rather than
  a copy. `qbittorrent` now waits on
  `monarch-seed: service_completed_successfully` — the reference that used to
  point at nothing — and the *arr apps (and qBittorrent/SABnzbd) mount
  `/mnt/media` at `/data` so the root folders and save paths `init.py` writes
  (`/data/media/<kind>`, `/data/torrents`) resolve. Its own state — the Jellyfin
  API key and the drift invariants file — lives in `monarch-init-data`.

  Two things the port had to fix on the way: `sabnzbd` published `8085` as its
  *container* port (hotio's SABnzbd listens on 8080), which left it unreachable
  from the host; and `SSO_SESSION_REDIS_HOST` still defaults to the
  pre-migration LAN address, so each group sets `SSO_*` in its own `.env`.
- **`monarch-recs`**, **`monarch-health`** — built from `./ai-recs` and
  `./health-analytics`; neither had a container on the source host, so neither
  was in the media bundle.

**Not a gap**

- **`monarch-api`** goes the other way: it exists in the group compose and in no
  repo. It is a group-only service, which is fine — but nothing else references
  it, so it is a candidate for deletion rather than a thing to port.

## 5-dev — group compose declares 12 (was 9)

`chef`, `convex`, `distro-control-plane`, `gitea`, `gitea-db`, `oasis-mail`,
`oasis-postgres`, `oasis-redis`, `consul-reg-g5`

| Repo | Declares | Absent from the group compose |
|---|---|---|
| `atlas` | 4 | `convex-dashboard`; group has a stale `chef` |
| `oasis` | ≥2 | `certbot` (cert renewal) |
| `distro` | 1 | — (`control-plane`, present as `distro-control-plane`) |
| `olympus` | 3 | **all 3** — `olympus`, `studio`, `autoheal` |

**Real gaps**

- ~~**Olympus is entirely absent.**~~ **Done.** `olympus` (the factory engine,
  own build + healthcheck on `doctor.py`, no published port by design) and
  `studio` (the ecosystem's one web UI, 127.0.0.1:3001 plus the edge host) are
  declared, with `autoheal` to restart either when its healthcheck fails — the
  three services the group-5 bundle could not carry because no container existed
  to pack. No model plane was added: they read `OMNIROUTE_BASE_URL` and default
  to the shared Group-2 gateway.
- ~~**`chef` is stale the other way.**~~ **Done.** The stale `chef` service (and
  its `chef-workspace` volume) is gone, and the Consul registration now points
  at `olympus-studio` and `convex` instead of `chef`. Atlas retired it
  ([build-plane convergence](convergence-onyx-olympus-distro-atlas.md) §8, Phase
  3); the group compose was the last place still starting it.
- **`convex-dashboard`** and **`certbot`** — **Done.** The Convex admin UI is
  declared loopback-only on 6790 (6791 belongs to PLUTUS in Group 3, and a
  single-box `up all` runs both), and Oasis's `certbot` renewal loop is
  declared under `profiles: [production]`, since the group terminates TLS at the
  edge unless a local reverse proxy reads `certbot-etc`.

## Verified on the migrated hosts (2026-09-16)

Zeus, Monarch and Olympus were cut over to dedicated servers — `zeus` +
`capstone` → **192.168.1.30**, `olympus` → **192.168.1.50**, `monarch` →
**192.168.1.56** — with the old server (**192.168.1.46**) keeping Cerulean,
Atlas, Distro, Magnate, Signara, ONYX, Rizz Aura, zapit and the shared
OmniRoute. What the check found, and what was done about it:

| Host | Found | Action |
|---|---|---|
| .30 | `capstone` declared 30 / ran 27 — the three one-shots (`n8n-import`, `sandbox-certs`, `signoz-schema-migrator`) had never run there | all three run to exit 0; `n8n-import` re-verified its webhook (HTTP 200) |
| .50 | declared 5 / ran 4 — **`olympus` itself and `autoheal` were missing** (Studio with no engine behind it, exactly the pack-time gap below). `ips` sat on a pre-migration revision whose registry still used flat sibling dirs | engine + autoheal built and started; `ips` updated to the group-relative registry on all three hosts |
| .56 | declared 31 / ran 22 — `monarch-init`, `monarch-seed`, `monarch-recs`, `monarch-health`, `flaresolverr`, `requestrr`, `clipbucket` missing; **PLUTUS never present at all** | seven services started (init/seed exit 0, idempotent against the migrated state); `plutus` cloned and moved into `3-media/plutus` |
| .56 | **`lidarr.db`, `prowlarr.db` and `whisparr2.db` were truncated by the copy** (618 KB / 237 KB / 442 KB against 3.1 MB / 389 KB / 1.5 MB at the source) — SQLite `database disk image is malformed`, so both apps were up but listening on nothing | stopped the three, kept the truncated copies as `*.truncated-<ts>`, restored the intact databases from the source host, restarted — all three answer HTTP 200, integrity `ok`, and are byte-identical to the source |

Everything else on .56 passed `PRAGMA integrity_check` and matches the source
byte-for-byte (`autobrr`, `homarr`, `jellyseerr`, `nginx-proxy-manager`,
`sabnzbd`, `jellyfin-subscription`); `sonarr.db`, `radarr.db` and `bazarr.db`
differ only because their apps are live and writing. **`scripts/dbcheck.py`** is
the read-only checker used for this — run it against `/docker/appdata` before
believing a restore is complete.

### Still open on those hosts

- **PLUTUS is installed but not running.** Its compose wants a filled
  `plutus/.env.local` and a built `out/` (the Next.js static export, built on the
  host) — neither travels with the repo, and the Convex schema has to be
  deployed to the fresh volume. Starting it half-configured would be worse than
  the honest "not running".
- ~~**Olympus's doctor reports `BLOCKED`**~~ **Fixed.** The gateway's key store
  was **empty** (`GET /api/keys` → `{"keys":[],"total":0}`), which is why every
  consumer's stored key was rejected. A key was minted through the gateway's own
  API (`POST /api/auth/login` → session cookie → `POST /api/keys`, which wants
  `name`, not the `label` the OpenAPI file documents) and written into every
  `OMNIROUTE_API_KEY=` on .46, .50 and .30, with a `.env.bak-omnikey-*` beside
  each. Olympus's doctor is now `Status: READY` (gateway reachable, HTTP 200),
  `health=healthy`, and **autoheal is back on** with the restart loop gone.
- ~~**`watchtower` was left stopped**~~ **Started**, deliberately and checked:
  the only compose project on .56 is `monarch` (28/28 containers), so its
  host-wide default scope is exactly the containers it is meant to touch. Next
  scheduled run is logged at startup.
- **`monarch-init`'s own report** asks for one manual step: the qBittorrent
  WebUI rejects the shared credentials (the stored PBKDF2 hash does not match
  this qBittorrent version), so the password has to be set in the WebUI and
  `monarch-init` re-run.
- **The hosts run the repos' own compose files, not the group composes.**
  `/usr/src/projects/complete/<group>/docker-compose.yml` is on each host but
  unused, so `./stack.sh up <group>` there would start a *second* copy of the
  same apps on the same ports. Deploying those hosts means their repo composes;
  the group composes are for a host that has none running.

## Migration-side gaps (what the bundles left behind)

Packing walks *containers*. A service the compose file declares but that never
got a container has nothing to walk, so it never entered the bundle — and a
restore that only replays containers comes back looking complete with the
service absent. `migrate-stack.py` now records the declared set as well and
starts it on restore; re-packed bundles report the gap at pack time:

| Group | Services the old bundle could not carry |
|---|---|
| 2-voice | `portal`, `n8n-import`, `sandbox-certs`, `signoz-schema-migrator` |
| 3-media | `monarch-recs`, `monarch-health`, `monarch-seed`, `monarch-init`, `clipbucket`, `flaresolverr`, `requestrr`, `watchtower` |
| 5-dev | `olympus`, `autoheal` |

(`portal` is Zeus's portal — a core service, not an initializer.)

## Structural finding

Every gap above is the same shape: the group compose **re-declares by hand**
what the repo already declares, so the two drift, and the drift is invisible
until someone asks. Two consequences worth acting on:

1. **The group composes are not tracked.** The workspace root is not a git
   repository (`git rev-parse --show-toplevel` fails above `ips/`), so
   `2-voice/docker-compose.yml`, `3-media/docker-compose.yml` and
   `5-dev/docker-compose.yml` have no history, no review and no diff. A drift
   like the stale `chef` above cannot be caught by CI.
2. **The fix is to stop re-declaring.** Compose ≥ 2.20 can `include:` another
   compose file, which is exactly the relationship here: the group compose is
   the group, and its services *are* the member repos' services plus the group's
   own (mesh, Consul registration). Including each member repo's
   `docker-compose.yml` (with the group supplying env and the mesh network)
   makes one source of truth per repo and deletes this whole class of gap. The
   alternative — hand-porting ~55 services into three untracked files — moves the
   drift rather than removing it.

## Recommended next actions

| # | Action | Status |
|---|---|---|
| 1 | `include:` each member repo's compose in its group compose, or generate the group compose from the repos — then delete the duplicated service blocks | **open** — the porting pass below did the opposite (it added back to the hand-declared file), so this is now more valuable, not less |
| 2 | Move the group composes into a tracked location (or a repo) so drift is reviewable | **open** |
| 3 | Until then, port the services the group *names* but does not start: Zeus's `pbx` (2-voice), PLUTUS (3-media), Olympus + `studio` (5-dev) | **done** — `zeus-freepbx`, the four PLUTUS services, `olympus` + `studio` + `autoheal` |
| 4 | Front the media apps: port the nine `*-sso` gateways and `monarch-init`/`monarch-seed` into 3-media, or the group-3 stack is both unconfigured and ungated | **done** — plus `whisparr`, `bazarr`, `iptv`, `authentik-ldap` and the appdata mount bridge they need |
| 5 | Delete the stale `chef` from 5-dev (Atlas retired it) and add `convex-dashboard`, `certbot` | **done** |
| 6 | Re-pack with the fixed `migrate-stack.py` before the next move — the old bundles cannot carry services that never had a container | **open** — `migrate-stack.py` records the declared set now; the bundles still have to be re-packed on the source host |
| 7 | Re-check the group composes against the repos after the next repo-side service change — the drift this page records was invisible until someone ran both `config --services` sides | **open** — and it is the reason action 1 keeps its place |
