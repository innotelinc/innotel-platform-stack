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

### Re-checked from the edge (2026-09-16)

Ports, probed from the primary host against **the addresses the proxy hosts
actually forward to** — the only check available without shell on the other three
boxes, and the one that catches a name pointed at where a stack used to live.

| Host | Answers on the LAN | Silent | Consequence |
|---|---|---|---|
| `.30` Zeus + Capstone | `3001` portal, `3010` UI, `3478` coturn, `8089` ARI/WS, `8095` dashboard-api, `8096` dashboard, `14010`–`14015` the six app gateways | **`8000`** | `api.capstone.innotel.us` and `backend.api.capstone.innotel.us` both forward to `.30:8000` and reach nothing — dograh-api is not listening there |
| `.50` Olympus | `3050` Studio, `20129` gateway-sso, `20130` site server | `16379` (its own store, loopback — by design) | no public name is broken |
| `.56` Monarch | `3011`, `7575` homarr, `8097`, `8098` clipbucket, `4545` requestrr, `6881` peer port, `14001`–`14009` the nine gateways | **`3000`/`3210`/`3211`/`6791`** | PLUTUS is still not running (as recorded above); Clipbucket and requestrr are still ungated |

Three more findings from the same pass:

- **`tube.innotel.us` forwards to `.46:8098`, which answers nothing**, while
  Clipbucket listens on `.56:8098` (open, redirecting to its own login). The
  media stack moved and this one proxy host did not;
  `3-media/monarch/docs/operations.md` still records `.46:8098` as its target.
- **`homarr`, `requestrr` and `clipbucket` all run on `.56` but are absent from
  `3-media/docker-compose.yml`** — a group-3 host started from the group compose
  comes up without them. Same drift as action 1, measured rather than inferred.
- **`dns.internal.innotel.us` answers 502.** Its gateway moved to `.30` with Group
  2, but the Technitium console it fronts stayed on `.46` and is bound to loopback
  and docker0 only, so the new host cannot reach it at all. A posture decision
  rather than a typo — both ways to close it are set out in
  [`sign-in-posture.md`](sign-in-posture.md) §5.

### What the host pass then fixed, and what is still open (2026-09-16)

With shell on the three hosts, the port probe above turned into actual fixes and
one correction of the probe's own reasoning:

| Found | Action |
|---|---|
| `.30`: `capstone-postgres-1`, `capstone-redis-1` and `minio` all **Exited (255)**, `restart=no`, stopped together at 22:43 — seven minutes after the rest of the stack was recreated. `dograh-api` (host-networked, dials `127.0.0.1:5432`) had been crash-looping on `ConnectionRefusedError ('127.0.0.1', 5432)` ever since | started all three; `dograh-api` came back healthy, and `api.capstone.innotel.us` went from connection-refused to answering (`404` at `/`, `200` at `/docs`) |
| `.30`: `dograh-api` then logged `DOGRAH_FAILURE … ari-connection` — its Asterisk ARI endpoint is stored **per organization in Postgres**, not in `.env`, and still named the pre-migration host (`http://192.168.1.46:8088`) | updated the one row (`telephony_configurations`, provider `ari`) to `http://192.168.1.30:8088`; `[ARI org=1] WebSocket connected` and no further provider errors. A DB-wide scan for `192.168.1.46` found only this row and 16 historical `webhook_deliveries.payload` records, which were left alone |
| `tube.innotel.us` forwards to `.46:8098`, where every request since the split was logged `connect() failed (111: Connection refused)` while Clipbucket listens on `.56:8098` | re-pointed the manual proxy host to `.56:8098` through the NPM API; the name answers again (this host is not in the monarch host map, which is scoped to `*.monarch.innotel.us`, so it stays manual) |
| `backend.api.capstone.innotel.us` answers nothing over HTTPS while `api.capstone.innotel.us` does | **Not a fault**: the host has no `listen 443` — it is an HTTP-only duplicate that answers `404` on port 80, and both point correctly at `.30:8000` |
| The shared SSO session store appeared unreachable from `.56` (no client connection) | **The probe was wrong, not the config.** oauth2-proxy dials Redis lazily, so only *used* gateways appear in the store's client list; `.56` was already pointed at `192.168.1.46` and a bare `nc` from inside `radarr-sso` proves the path. Both moved zones now pass their own end-to-end `verify-sso.py` — see [`sign-in-posture.md`](sign-in-posture.md) §5 |

Still open on the hosts, all three needing a decision rather than a repair:

- **PLUTUS is up on `.56` now** (this was open when the table above was written):
  `.env.local` written for that host, the Convex schema pushed and seeded against
  the backend there (`clipCount: 3`), and `backend` / `web` (3000) / `dashboard`
  (6791) running. The push ran from `.46` against `http://192.168.1.56:3210` with a
  key generated inside the backend, so nothing had to be installed on `.56` (it has
  no `node_modules`; `web` is nginx serving the export). `OMNIROUTE_BASE_URL` turned
  out never to have been set on the deployment — the Convex functions were falling
  back to `localhost:20128` *inside their own container* — and is now
  `http://192.168.1.46:20129/v1`. Open: the storefront's own public name is still
  only `subscribe.plutus` / `auth.plutus` pointing at `.46`, so nothing routes the
  migrated app yet.
- **Clipbucket is running but serving its installer.** Its DB volume has **0
  tables** on `.56` and on `.46` alike (the `clipbucket` schema holds only
  `db.opt`), and `monarch_clipbucket_files` is the freshly cloned source (122–141
  MB),  so there is no migrated content on either host to point it at. The
  documented rollback source is `.72:8088`. Its *exposure* is closed in the tree
  as of the same pass (`127.0.0.1:8098` plus `clipbucket-sso`), so what remains
  is a data question, not a door.
- **`dns.internal.innotel.us`** (see above) — **the tree now takes the first of
  the two ways out**: `technitium-sso` deploys with the console it fronts, from
  `1-primary/cerulean/docker-compose.yml` under the `technitium` profile, so the
  gateway no longer sits on another host from the loopback-bound console. What
  is left is the deploy, and then the 502 is gone without the console ever
  answering on the LAN.

### The media host's last open doors (2026-09-16, same pass)

Both tables above record the same three names on `.56` as reachable with no
relying party — `media.innotel.us` / `media.magnate.innotel.us` (Jellyfin,
`:8097`), `tube.innotel.us` (Clipbucket, `:8098`) and `tv.monarch.innotel.us`
(the IPTV guide, `:3011`) — plus Requestrr's console on `:4545`, the one port
that was not in either table because nothing in this repo knew its name. All
four now have a gateway **and** a loopback bind, so the gateway is the only door
rather than one of two:

| Name | Gateway | Port (`.56`) | App |
|---|---|---|---|
| `media.innotel.us`, `media.magnate.innotel.us` | `jellyfin-sso` | `14010` | `jellyfin:8096` |
| `tube.innotel.us` | `clipbucket-sso` | `14011` | `clipbucket:80` |
| `tv.monarch.innotel.us` | `iptv-sso` | `14012` | `iptv:3000` |
| `requestrr.monarch.innotel.us` | `requestrr-sso` | `14013` | `requestrr:4545` |

**Deployed on 2026-09-17** (the forwards and the provider were changed and then
verified by driving each name, not by reading config):

| Name | Edge forward | Verified |
|---|---|---|
| `media.innotel.us`, `media.magnate.innotel.us` | `192.168.1.56:14010` (`jellyfin-sso`) | `302` → Authentik, `client_id=monarch-media`, correct `redirect_uri` (was `502`) |
| `tube.innotel.us` | `192.168.1.56:14011` (`clipbucket-sso`) | `302` → Authentik (was `502`) |
| `tv.monarch.innotel.us` | `192.168.1.56:14012` (`iptv-sso`) | `302` → Authentik |
| `requestrr.monarch.innotel.us` | `192.168.1.56:14013` (`requestrr-sso`) | **proxy host created**, but the name does not resolve and the gateway is not deployed yet — see below |

The provider `Monarch-media` (pk 37) now holds **16/16** redirect URIs; the one
missing was `requestrr`'s. The zone script's own check agrees afterwards:
`all 16 proxy hosts match npm-hosts.conf`.

Two findings from that pass, both worth carrying:

- **A proxy host's certificate is state, and `--hosts-only` used to clear it.**
  The update body carried `certificate_id: 0`, which NPM writes as a value rather
  than reading as "leave it alone": all sixteen hosts in the `monarch.innotel.us`
  zone came back without the wildcard certificate and with `ssl_forced` off. The
  values were restored from the 02:00 NPM backup (`proxy_host` table) within
  minutes, and the script now (a) carries an existing host's certificate over when
  a run resolves none and (b) reports `serves no TLS certificate` as drift unless
  `--skip-ssl` says the zone is deliberately cert-less.
- **`requestrr` still needs two things, and neither is the edge.** Its DNS record
  was never created — the script's Technitium step could not reach
  `192.168.1.46:5380` from where it ran, and unlike its neighbours the name has no
  record at all — and the gateway `requestrr-sso` is not running on `.56`, so
  `4545` is still the one app port in this stack answering off-host with its own
  password. Deploy the compose on the media host (which publishes `127.0.0.1:4545`
  and adds `14013`) and add the A record; the proxy host and the redirect URI are
  already in place.

Three things this leaves worth knowing, all of them easy to get wrong twice:

- **The edge still owns three of the four forwards.** `media.*` and `tube.*` are
  not `*.monarch.innotel.us`, so pointing them at the gateway rather than at the
  app's own port is an NPM change on `.46` and not a line in the media repo —
  the same division that let `tube.innotel.us` keep serving a dead `.46:8098`
  for a day after the split. Only `requestrr` is a `npm-hosts.conf` row.
  `scripts/verify-sso.py` in the media repo now drives all four names end to
  end, so a forward left behind fails rather than looking configured.
- **Registering the redirect URI is a second step, on a different host.** A
  gateway derives `redirect_uri` from the request's Host, and a name missing
  from the `monarch-media` provider dies at the callback with
  `invalid_request: redirect_uri does not match` — no login form, no clue in the
  app's own logs. The list is `MONARCH_SSO_REDIRECT_URIS` in
  `3-media/monarch/.env.example`, and the `authentik-setup.py` invocation that
  registers it is written beside the variable.
- **Jellyfin's native clients were given back their door, with the page still
  gated.** The first cut of this pass gated `jellyfin-sso` on every path, which
  shuts out a TV or mobile client: those speak the Jellyfin API rather than
  opening a sign-in page. The fix is the one `sign-in-posture.md` §5 named — a
  skip-auth rule for the API instead of re-opening `8097` — and it is one line:
  `OAUTH2_PROXY_SKIP_AUTH_ROUTES: "!=^/(web(/.*)?)?$"`, because oauth2-proxy
  reads `!=` as "skip when the path does *not* match". Measured against that
  image before it was written in: `/Users/…`, `/Items`, `/socket` and
  `/emby/System/Info/Public` answer 200 (passed through, Jellyfin authenticates
  the client's own token) while `/`, `/web/` and `/web/index.html` answer 403
  with no session. The user's identity on the API path is still Cerulean's: the
  token such a client holds came from signing in against the LDAP outpost.
- **Two of these apps also keep a credential store of their own**, which the
  gateway — a gate on the *name* — never touched: Seerr's email-and-password
  sign-in (`main.localLogin`; Seerr has no OIDC support at all, measured) and any
  account in Jellyfin's own database rather than the LDAP outpost. Both are now
  scripted, unit-tested and run by `drift-check.sh`
  (`3-media/monarch/scripts/seerr-login-methods.py`,
  `jellyfin-login-methods.py`). The two things each leaves alone on purpose:
  Seerr's Jellyfin sign-in (it is the Cerulean identity, and without it the
  gateway authenticates nobody *into* Seerr), and Jellyfin's break-glass `admin`
  (the one local account `jellyfin-admin-password.py` maintains; strays are
  disabled, never deleted, so their history survives and the LDAP provider can
  take the account back).

### The gateway's own host split (2026-09-16, same pass)

OmniRoute is declared by `2-voice/capstone`, and the migration moved capstone and
zeus to `.30` — but the gateway's compose block keeps it on the Cerulean/trust host,
so it stayed on `.46` while its SSO proxy left with olympus to `.50`. The two halves
were then on different hosts, which forced a LAN binding on `20128`
(`OMNIROUTE_LAN_BIND=192.168.1.46`) whose only gate was the dashboard's own password.
That is exactly the configuration `5-dev/olympus/scripts/gateway-auth-mode.py` refuses:
turning the password off makes reachability the whole control.

Put back together, and published as asked on a `studio` name:

- the gateway's LAN binding is **gone** (loopback + docker0 only, which is what the
  compose's other two bindings were always for);
- the proxy runs on the **gateway's host again** (`5-dev/olympus` on `.46`, upstream
  `http://127.0.0.1:20128`), and `.50`'s copy was removed;
- `requireLogin=false` is **live** — Authentik is the only gate, and the stored
  password is kept as the recovery path;
- `gateway.studio.innotel.us` is published (DNS CNAME, Let's Encrypt cert #49, NPM
  host #178) with `/v1` refused at the edge, and the old
  `gateway.olympus.innotel.us` is a second door to the same proxy — it began as a
  `301`, which then sent the callback to the other name, so the proxy derives each
  callback from the request's Host instead and both names log in on themselves;
- consumers now dial the proxy: `.30`'s `N8N_INSTANCE_AI_MODEL_URL` and `OMNIROUTE_URL`
  (they had been pointing at their *own* docker0 since the split — broken, not just
  stale) and PLUTUS's `OMNIROUTE_BASE_URL`. The distro control plane keeps
  `host.docker.internal:20128` on `.46`, and its `POST /api/auth/login` still answers
  `200` with a cookie, which is what its tenant key provisioning depends on.

Two defects in the guard had to be fixed first, both of which made it fail **open**:
it rejected the host's own docker0 (so it refused on every deployment the estate
actually runs), and it looked for a container named `g2-omniroute`, which matches
nothing here — and an unreadable binding is a warning, not a refusal. Details and the
verification list are in `5-dev/olympus/docs/gateway-sso.md`.

### What the new door left behind (2026-09-16, same pass)

Moving the door is not the same as moving every reference to the old one, and the
references still taught the closed port. `ips/scripts/check-gateway-targets.py` is the
guard: a target naming `<host>:20128` is wrong whatever the host, because that port
answers on the gateway host's loopback and bridge alone, while the door is the SSO
proxy on `:20129`, which exempts `/v1` for API clients. It found ten, across the
templates and docs of every group:

- `1-primary/atheniq`, `3-media/plutus`, `4-social/onyx`, `5-dev/atlas`,
  `5-dev/olympus` — `.env.example` and compose defaults naming the mesh
  (`10.10.2.1:20128`), or a compose service (`omniroute:20128`) this project does not
  declare and can never resolve;
- `2-voice/capstone/n8n-grader-workflow.json` — the committed grader workflow dialled
  `host.docker.internal:20128`, the n8n **container's own** docker0;
- `5-dev/olympus` — `setup.sh` and `scripts/docker-entrypoint.sh` *wrote* the mesh form,
  so the templates were not the only source of it. `MESH_GATEWAY_HOST` goes with it.

Fixed in the tree and pushed to the hosts that deploy them (`.30`'s capstone,
`.50`'s olympus, `.56`'s plutus), the containers that carried the old value were
recreated (`.50`'s `olympus`/`studio`, `.46`'s `onyx-ai`), and the operational docs in
atheniq, atlas, onyx, rizzaura, capstone, distro, olympus and `ips` were re-taught.
`ips/.github/workflows/ci.yml` runs the check on every push.

Two things it cannot see, both found by hand:

1. **A file that declares the gateway is exempt — per file, not per host.**
   `2-voice/capstone/docker-compose.yml` declares it, so its
   `host.docker.internal:20128` defaults were waved through: right on `.46`, dead on
   `.30`, where that alias is n8n's own docker0. The same shape hides any compose
   file that runs on two hosts.
2. **Loopback is exempt, and loopback is right only on the gateway's host.** `.46`
   exports `OMNIROUTE_BASE_URL=http://localhost:20128` from
   `/etc/profile.d/omniroute.sh`, `/root/.bashrc` and `/root/.profile` — and a shell
   export **beats** `.env` in compose interpolation, so it reached containers on `.46`.
   That is how `onyx-ai` came to hold `localhost:20128`, which inside that container is
   onyx-ai. `onyx`'s `.env` now names the door so a clean-environment deploy is correct;
   the host exports themselves were left alone, being host files rather than repo files
   and correct for the CLIs they were written for.

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

## How the group composes are produced (2026-09-18)

`ips/scripts/gen-group-compose.py` builds `ips/groups/<group>.yml` from the
member repos named in its `SOURCES` table, and `--check` fails when the
checked-in file is stale. Both the drift check and `--check` are wired into
CI (`.github/workflows/ci.yml`); each skips cleanly in a checkout that stands
alone, so a host with the estate is where they do the real work. The hand-written files it replaces are gone: each group
dir's `docker-compose.yml` is a pointer, and `stack.sh` runs the generated file.

Four decisions in it are worth knowing, because each one was a measured call and
not a preference:

- **`SOURCES` is measured, not guessed.** A repo's own files do not say which of
them a group host runs, so the answer was read off the hosts: the
`com.docker.compose.project.config_files` label of every running container
(`.30`, `.46`, `.50`, `.56`, 2026-09-18). That is what settled `zeus` — the voice
host runs `docker-compose.full.yml` (`zeus-freepbx`, `zeus-portal`, `pbx-coturn`),
not the standalone portal compose, which declares a second portal and a second
gateway that no host runs.
- **A repo's `container_name` is never rewritten.** The hand-written files
invented `g<N>-` prefixes; no host has ever run a container with one. The hosts
run `cerulean`, `jellyfin`, `zeus-portal`, `omniroute` — the repos' own names,
which is also what every script, doc and Consul registration here names. The
gateway's host-split section above records the cost of the guess: a guard that
looked for a container named `g2-omniroute` matched nothing, so it warned where
it should have refused.
- **A repo deployed as two files is merged the way compose merges them.** Signara
runs as `docker-compose.prod.yml` **plus** `docker-compose.override.prod.yml`;
list keys append (`ports:`, `profiles:`), everything else replaces, and a
mapping-valued key (`environment:`) is merged key by key so no YAML key is
defined twice. Dropping the override described a stack nobody runs — its
healthcheck fix and minio's public `9002` port were the parts that mattered.
- **Paths are rewritten; a container path is not.** `build: .`, `context: .`,
`env_file: .env` (and its `- .env` / `- path: .env` list forms) and every bind
mount are prefixed with the declaring repo, because compose resolves them
against the file that declares them. A `dockerfile:` stays as written (it is
relative to its context), as does anything under `command:`/`entrypoint:`.
All five files pass `docker compose config --no-interpolate`, and four of the
five render fully (`config -q`, every profile on) against nothing but the member
repos' own `.env`/`.env.example` files — every resolved build context and bind
mount lands on a real path in the repos. The fifth was one variable:
`1-primary` needs `TUTOR_MYSQL_ROOT_PASSWORD`, and no repo carries it, because
Tutor's LMS compose is generated on the host (`tutor local`) and only the
group-owned extras here reference it. It is in `ips/.env.example` now, under
AthenIQ/Tutor. This is the class of gap the merge would have hidden: a
`${VAR:?}` in a group-owned service is invisible to every repo's env template,
so the group compose is what has to be rendered to find it.

What the generator reports and cannot decide: two services publishing one host
port. `1-primary` has one real case (`3000`: `cerulean`, `client`, `frontend`,
`magnate` — from variable defaults, so the value a given host uses can differ);
the rest are opt-in and reported as notes, not warnings. Two host-port pairs are
newly visible (`9443` authentik, `8200` vault, `3478` coturn) and all three are
behind profiles on at least one side.

Two things it found that are not about generation:

1. **`1-primary/sign` declares `env_file: .env.prod`, and no such file exists**
   on the trust host (`.46`) or in the bundle — only `.env.example`. No OpenSign
   container runs anywhere, so this is not a live outage, but a host rebuilt from
   group 1 cannot start that service until someone writes the file.
2. **`requestrr.monarch.innotel.us` has no DNS record** and
   **`api.signara.innotel.us` answers 502 with no Signara container running** —
   both recorded under the sign-in posture work, neither touched here.
3. **One literal credential is now carried in two repos.** Monarch's clipbucket
   service sets `MYSQL_PASSWORD: ClipDB!2026` in `docker-compose.yml` rather than
   reading it from `.env`, so the generated `groups/3-media.yml` carries the same
   literal. Both repos are private and in the same org, so this is not a new
   exposure — but it is the one place where a *generated* file copies a secret
   value instead of a `${VAR}` reference. The fix belongs in the source
   (`${CLIPBUCKET_DB_PASSWORD}` + an `.env.example` entry), and until it is made,
   this is what `grep` finds when someone asks "what literals did we duplicate".
   It is the only such literal across all five generated files.

| # | Action | Status |
|---|---|---|
| 1 | `include:` each member repo's compose in its group compose, or generate the group compose from the repos — then delete the duplicated service blocks | **done** — `ips/scripts/gen-group-compose.py` derives every group compose from its member repos and writes it to `ips/groups/<group>.yml`; nothing is re-declared by hand. `2-voice` 39 services, `3-media` 82, `1-primary` 46, `4-social` 20, `5-dev` 21. See *How the group composes are produced* below |
| 2 | Move the group composes into a tracked location (or a repo) so drift is reviewable | **done** — they are generated *in* this repo (`ips/groups/`), so they have history, review and a diff; `stack.sh` prefers them, and each group dir's `docker-compose.yml` is a pointer (`include: ../ips/groups/<group>.yml`) for anyone running a group from its own directory |
| 3 | Until then, port the services the group *names* but does not start: Zeus's `pbx` (2-voice), PLUTUS (3-media), Olympus + `studio` (5-dev) | **done** — `zeus-freepbx`, the four PLUTUS services, `olympus` + `studio` + `autoheal` |
| 4 | Front the media apps: port the nine `*-sso` gateways and `monarch-init`/`monarch-seed` into 3-media, or the group-3 stack is both unconfigured and ungated | **done** — plus `whisparr`, `bazarr`, `iptv`, `authentik-ldap` and the appdata mount bridge they need |
| 5 | Delete the stale `chef` from 5-dev (Atlas retired it) and add `convex-dashboard`, `certbot` | **done** |
| 6 | Re-pack with the fixed `migrate-stack.py` before the next move — the old bundles cannot carry services that never had a container | **open** — `migrate-stack.py` records the declared set now; the bundles still have to be re-packed on the source host |
| 7 | Re-check the group composes against the repos after the next repo-side service change — the drift this page records was invisible until someone ran both `config --services` sides | **now a check, and it measures 0 findings** — with the files generated (action 1) there is nothing left to drift, and the check now reads the group compose *through* its pointer, so it still fails the moment a repo adds a service the generated file does not declare. Before the generator: 52 findings, and `ips/scripts/check-group-compose-drift.py` compares each group compose with its member repos' service sets, reading *every* compose file a repo carries (`docker-compose.full.yml`, `compose.cerulean.yml`, the overlays) and pairing prefixed renames with the repo that owns them instead of calling them drift. Wired into this repo's CI. Its run over the estate on 2026-09-16: `4-social` **clean**; `1-primary` missing `app`, `backup-ui` (npm) and `client`, `mongo` (sign); `5-dev` missing `gateway-sso`, `gateway-sso-sessions` (the host-gateway overlay is newer than the group file); `3-media` missing `clipbucket`, `clipbucket-sso`, `homarr`, `iptv-sso`, `jellyfin-sso`, `monarch-health`, `monarch-recs`, `requestrr`, `requestrr-sso` and still declaring `monarch-api` no repo has; `2-voice` missing 35 — Capstone's 29 services and Zeus's `pbx`/`signoz` family — with `capstone-api` in no repo. It reports; closing it is action 1, and the reporting is what makes action 1 worth doing |

### Every host's checkouts were still withholding commits (2026-09-18)

`.56`'s `ips` checkout sat 10 commits behind with a dirty tree, so nothing new
could be run *from there* — `scripts/check-vault-refs.py` (the check the secrets
pass added) could only be run from `.46`, i.e. not on the host whose `.env` files
it partly exists to read. Sweeping the other hosts found the same shape
everywhere, in eleven checkouts across `.30`, `.50` and `.56`:

| Host | Checkout | Was | Now |
|---|---|---|---|
| `.30` | `ips` | 11 behind, 8 dirty | clean, `90c4b32` |
| `.30` | `2-voice/zeus` | 7 behind, 3 dirty | clean, `7c7bf24` |
| `.30` | `2-voice/capstone` | 3 untracked | clean |
| `.50` | `ips` | 11 behind, 8 dirty | clean, `90c4b32` |
| `.50` | `5-dev/olympus` | 1 behind, 1 untracked | current, `40f285f` |
| `.56` | `ips` | 10 behind, 15 dirty | clean, `90c4b32` |
| `.56` | `3-media/monarch` | 2 untracked | clean |
| `.56` | `3-media/plutus` | 1 behind, 1 dirty | clean, `75658e1` |

Nothing on any of them was unique. Measured file by file against this repo's
history, rather than by eye (`git hash-object` plus `git log --find-object`):

- **Every modified tracked file was content already upstream**, byte for byte —
  older copies of files this repo has since moved past. That includes the `ips`
  edits on all three hosts (all blobs from `8f47c58`/`86354db`/`6990cb9`),
  zeus's `scripts/npm-proxy-hosts.py` (`9bbbcf1`), and the whole untracked
  `scripts/tests/` + `scripts/dbcheck.py` + `groups/*.yml` set that the blocked
  pull had never delivered. Zeus's `docker-compose.full.yml` delta was
  **comments only** — all 16 changed lines, verified line by line — and the
  coturn-TLS / portal-Vault-mount work it described is already in the file.
- **Three held older drafts of documents rewritten here since** —
  `docs/stack-migration-gaps.md` (the *same* draft on all three hosts, and its
  text still said the gateway's key store was empty and autoheal was stopped), the
  monitoring `.env.host.example` (missing the port block) and plutus's
  `.env.example`, which dialled the gateway's own port the commit after it was
  changed to dial the proxy door.
- **Three files were genuinely host-local and were kept**: `extensions/monitoring/.env.host`
  (that host's ports and Grafana password, mode 0600, sha256 unchanged across the
  pull) and the `.env`/compose backups taken during the Vault and issuer work,
  moved to `/root/host-backups-2026-09-18/` rather than left in a checkout.
- **One untracked file was left alone on purpose**:
  `5-dev/olympus/build-requests/resume-generator.md` is a *build request*, not
  stale state — `build-requests/` is tracked upstream, the file is new content
  someone wrote on that host, and deleting it to tidy a tree would delete the
  request.

Every dirty state was tarred off-host before anything was removed, and every
checkout on every host is now clean and current — which is how
`python3 scripts/check-vault-refs.py` came to run from `.56` (`ok: no vault://
references in the estate`, for what is checked out there) and from `.30` and
`.50`. One pull also had a consequence worth stating: `.30`'s zeus compose moved
**forward** to what is already running there (coturn's TLS flags and the portal's
Vault token mount are in both the file and the live containers), so nothing had
to be recreated — the checkout had simply been behind the deployment. The lesson
is the one the generator's section repeats: a stale working copy is not inert —
it silently
withholds every later commit from the host that has it.

### The subscribe portal was in no group compose, so nothing started it (2026-09-18)

`web/subscribe/docker-compose.ext.yml` is this repo's own service — a small
nginx that picks a service's page by Host header and is what every
`subscribe.*.innotel.us` host forwards to (`192.168.1.46:3040`, the file's own
header). It was **not** a member of any group compose and not an
extension (`stack.sh` only enables `extensions/<name>/`), so on a rebuilt host
nothing created it: the 17 `subscribe.*` proxy hosts and the `Subscribe` tile on
the Homarr board all answered **502** while the portal itself was one command
away. Started here, and it is `restart: unless-stopped`, so it survives reboots:

```
docker compose -f web/subscribe/docker-compose.ext.yml up -d
```

The gap was the service's *placement*, not its definition: it now lives in
`groups/extras/1-primary.yml` (the group that owns the edge, beside the Tutor
services) and is registered as group-owned in
`scripts/check-group-compose-drift.py`, so `stack.sh up 1` starts it rather than
reporting it as an extra. `scripts/subscribe-hosts.py` reconciles the hosts and
`scripts/sync-subscribe-pages.py` regenerates the pages — the container is the
part that had no owner.

Regenerating for this also picked up the repos' newer comments and the
`MONARCH_SSO_*`/`MONARCH_SEERR_OWNER` environment in `3-media` (monarch had moved
on since the files were written), which is the drift the generator exists to
close. One finding is still open and not from this change: `5-dev`'s member repo
`ontrak` declares 5 services (`gateway`, `guacamole`, `guacd`, `lab-setup`,
`portal`) that `gen-group-compose.py`'s `SOURCES` does not list, so the drift
check reports them MISSING — adding the repo to `SOURCES` is what closes it, and
that changes what a rebuilt `5-dev` host deploys.
