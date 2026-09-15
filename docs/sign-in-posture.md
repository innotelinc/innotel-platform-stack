# Sign-in posture — Authentik is the only identity

**Rule.** Identity lives in Cerulean's **Authentik** (IdentityOps). A platform
may *consume* identity; it may not keep a second user store or a second password
path. Every deployed surface is either **Authentik-only** or explicitly listed
below as an exception with the reason.

This document is the inventory of every login on the host, what enforces it, and
the break-glass path for each. It exists because "we use SSO" is not a fact you
can audit — a password form left in the template is a second identity store even
when nobody uses it.

> **Mechanism (2026-09-15).** Authentik **forward-auth is retired**. No NPM proxy
> host carries an `auth_request` gate or `/outpost.goauthentik.io/...` subrequest
> any more, and the per-zone outpost providers are superseded. A surface either
> speaks **OIDC itself** or is fronted by a real OIDC relying party — an
> **`oauth2-proxy` gateway** that runs the browser through an authorization-code
> flow against Cerulean Authentik. There is no outpost and no `auth_request`
> anywhere in the path.

---

## 1. How a gateway works, and the three things that make it work

```
browser ──https──▶ NPM edge (192.168.1.46)
                     │  e.g. n8n.capstone.innotel.us
                     ▼  http://192.168.1.46:14010
               n8n-sso (oauth2-proxy)
                     │   ──▶ Authentik: /application/o/authorize/  (code + PKCE)
                     │   ──▶ redis: cerulean-sso-sessions (the session lives here)
                     ▼  http://n8n:5678
                  n8n  (its own login page is unused; nothing else fronts it)
```

Every gateway shares **one `_innotel_sso` cookie on `.innotel.us`** and **one
session store**, so a single Authentik sign-in covers all of them.

Three settings are load-bearing, and each was **measured on the live edge**, not
assumed. All three were missing from the first cut of the gateways, and the
combination meant the sign-in had never actually completed:

| Setting | What goes wrong without it |
| --- | --- |
| `--insecure-oidc-allow-unverified-email` | Authentik's own `email` scope mapping sets `email_verified: false` (it has no authoritative source for the claim), and oauth2-proxy refuses such a token: `Error redeeming code during OAuth2 callback: email in id_token (…) isn't verified` → **HTTP 500** on `/oauth2/callback`. |
| `--oidc-groups-claim=groups` | With no groups claim, `--allowed-group` restricts **nothing** — any authenticated identity is admitted, and the group binding becomes decoration. |
| `--session-store-type=redis` | A cookie session carries the email, the ID token and every group. `dhunter` is in **28** groups, so it exceeds the 4KB cookie ceiling; oauth2-proxy splits it across several cookies, those `Set-Cookie` headers overflow the edge's `proxy_buffer_size`, and nginx answers the login with `upstream sent too big header while reading response header from upstream` → **HTTP 502** on `/oauth2/callback`. |

The third one is not size-dependent on the *user*: it was reproduced with a
single-group identity too, because the ID token alone is already over the
ceiling. Server-side sessions remove the ceiling rather than raise it, and the
browser holds one opaque id.

**One store, three networks.** The gateways live in three different Docker
networks (the edge's own, Monarch's, Capstone's), so the session store is
published on the host — `cerulean-sso-sessions`, `192.168.1.46:16380` — with a
password that is the whole control on that listener (an empty one refuses to
start). Sessions are the only thing in it, there is no volume, and losing it
costs a re-login.

### The gateway inventory

| Host(s) | Gateway | Listen (host) | Upstream | Authentik client |
| --- | --- | --- | --- | --- |
| `proxy.innotel.us`, `admin.zeus`, `admin.monarch`, `admin.signara` | `cerulean-npm-sso` | `127.0.0.1:4180` (the edge's own netns) | NPM admin UI `127.0.0.1:81` | `npm-edge` |
| `radarr` · `sonarr` · `lidarr` · `whisparr` · `bazarr` · `prowlarr` `.monarch.innotel.us` | `{radarr,sonarr,lidarr,whisparr,bazarr,prowlarr}-sso` | `14001`–`14006` | `http://<app>:<port>` | `monarch-media` |
| `qbittorrent` · `sabnzbd` `.monarch.innotel.us` | `qbittorrent-sso`, `sabnzbd-sso` | `14007`, `14008` | `http://qbittorrent:8080`, `http://sabnzbd:8080` | `monarch-media` |
| `req.monarch.innotel.us`, `req.innotel.us` | `jellyseerr-sso` | `14009` | `http://jellyseerr:5055` | `monarch-media` |
| `n8n.capstone.innotel.us` | `n8n-sso` | `14010` | `http://n8n:5678` | `innotel-app-gateway` |
| `grist.capstone.innotel.us` | `grist-sso` | `14011` | `http://grist:8484` | `innotel-app-gateway` |
| `signoz.capstone.innotel.us` | `signoz-sso` | `14012` | `http://signoz:8080` | `innotel-app-gateway` |
| `workflow.capstone.innotel.us` | `workflow-sso` | `14013` | `http://workflow-studio:8090` | `innotel-app-gateway` |
| `pbx.capstone`, `pbx.innotel.us`, `pbx.zeus`, `fax.zeus` | `pbx-sso` | `14014` | `http://192.168.1.46:8083` (FreePBX) | `innotel-app-gateway` |
| `dns.internal.innotel.us` | `technitium-sso` | `14015` | `http://192.168.1.46:5380` | `innotel-app-gateway` |

Each gateway's deployment file is the stack that owns the app: the media
gateways in `3-media/monarch/docker-compose.yml`, the app gateways in
`2-voice/capstone/docker-compose.yml`, and the edge's own in
`1-primary/npm/compose.cerulean.yml`.

**Skip-auth routes**, i.e. what stays reachable without a session:

| Gateway | Open paths | Why |
| --- | --- | --- |
| `n8n-sso` | `^/webhook/`, `^/webhook-test/`, `^/healthz$` | Receiving webhooks is n8n's purpose; an interactive login in front of them would break every caller rather than add a check. The editor and the REST API stay gated. |
| `signoz-sso` | `^/api/v1/health$` | liveness. Traces arrive at the OTel collector's own ports, not here. |
| `grist-sso`, `workflow-sso`, `pbx-sso`, `technitium-sso`, and the media gateways | **nothing** | Their APIs are reached internally (container name / loopback), never through the public name, so there is no integration to preserve. Grist in particular runs in **single-identity mode** (`GRIST_DEFAULT_EMAIL`), so its `/api` is effectively unauthenticated — exempting it would publish the dashboards. |

### The bare `innotel.us` zone

`proxy.innotel.us` (the NPM admin UI) is an edge door in the **bare**
`innotel.us` zone, and `req.innotel.us` is the subscriber-facing Jellyseerr
name. Both are outside `MONARCH_DOMAIN` / `NPM_BASE_DOMAIN`, so no
`npm-proxy-hosts.py` manages them: they are **manual** proxy hosts. They are
pointed at a gateway like everything else, and re-pointing them is a manual
step — the redirect URIs they use (`https://req.innotel.us/oauth2/callback`)
are registered on the `monarch-media` provider.

---

## 2. First-party apps — all Authentik-only

Enforcement is always **server-side**. Hiding a form is presentation; the
handler that mints the session is the control.

| App | Surface | Posture | Enforced in | Break-glass |
|---|---|---|---|---|
| **Magnate** | `/admin`, `/admin/login` | Authentik OIDC only | `lib/auth.ts` `breakglassLoginEnabled()` gates `app/api/admin/login/route.ts`; `components/AdminLogin.tsx` renders the form only when on | `BREAKGLASS_LOGIN=1` |
| **Cerulean** | portal (all `/api/*` behind a session) | Authentik OIDC only | `server/src/config.ts` `auth.localEnabled` (default **false**), `server/src/routes.ts` `POST /auth/login` → 403 | `BREAKGLASS_LOGIN=1` (legacy alias `AUTH_LOCAL_ENABLED=1`) |
| **Distro** | control plane `/admin`, `/login` | Authentik OIDC only | `src/oidc.js` `localLoginEnabled()`, `src/http.js` gates `/api/auth/signup` + `/api/auth/login` → 403 | `BREAKGLASS_LOGIN=1` |
| **Olympus (Studio)** | Studio UI | Authentik OIDC (PKCE) — **no local path exists** | `web/studio/lib/auth.ts` | none needed |
| **Zeus** | portal | Authentik when the `AUTHENTIK_*` vars are set (`AUTH_MODE` auto) | `src/lib/oidc.ts` `passwordLoginEnabled()`, `app/api/auth/login/route.ts` → 403 | `AUTH_MODE=both` (button + form) |
| **Rizz Aura** | app / rankings / community / admin | Authentik OIDC only — **no password store at all** | `api/auth.mjs` | none |
| **Capstone dashboard** | Control Center | Authentik OIDC only | `dashboard-backend/app/main.py` `/auth/login` is an OIDC redirect | none |
| **NPM Edge (admin UI)** | `proxy.innotel.us`, `admin.zeus`, `admin.monarch`, `admin.signara` | Authentik OIDC only, via the `cerulean-npm-sso` gateway — **no password path on the edge** | `backend/lib/sso.js`: `cameFromEdge()` (loopback) + `identityAllowed()` gate `POST /tokens/sso`; `passwordGrantAllowed()` refuses `POST /tokens` on every edge request | `BREAKGLASS_LOGIN=1`, off the edge (the LAN admin port) |
| **Vault** | `secrets.cerulean.innotel.us` | Authentik OIDC (native `auth/oidc`, role `operator`, group `cerulean-platform`) | `scripts/vault-entrypoint.sh` step 4c configures it; the role's `bound_claims` requires the group | Vault's **token method**, which cannot be disabled, off the public name |

### The break-glass convention

`BREAKGLASS_LOGIN=1` re-enables the local password path for **recovery only** —
when Authentik is unreachable and you need to get in to fix it. It is off unless
explicitly set, and it is read server-side, so the form and the endpoint agree.

```bash
# 1. On the affected app's host, set the flag and restart that one service.
cd 5-dev/distro && sed -i 's/^BREAKGLASS_LOGIN=.*/BREAKGLASS_LOGIN=1/' .env
docker compose up -d control-plane

# 2. Sign in with the local credential, fix Authentik, sign out.

# 3. Unset it and restart — the password path closes again.
sed -i 's/^BREAKGLASS_LOGIN=.*/BREAKGLASS_LOGIN=/' .env
docker compose up -d control-plane
```

Cerulean keeps the older name for the same switch (`AUTH_LOCAL_ENABLED=1`), and
Zeus already had this shape as `AUTH_MODE=both`.

Two break-glass paths are deliberately **off the public name** rather than
flagged:

* **NPM Edge** — its password grant is refused on every request that came from
  the edge, even with `BREAKGLASS_LOGIN=1`, because that door is SSO-only by
  construction. The way back in is the admin port on the LAN
  (`http://<host>:81`), which is why that port stays published.
* **Vault** — the token method remains enabled and is the way in when Authentik
  is down. Pasted-token sign-in is no longer the *intended* door, only the
  recovery one.

---

## 3. Third-party apps

"Make it SSO-only" means one of three things, and only the first is a pure
configuration change.

| App | Today | SSO posture |
|---|---|---|
| **Jellyfin** | Authentik **LDAP outpost** (`jellyfin-ldap`) — logins resolve against Cerulean users, `paid_users` gates access | ✅ **already SSO.** Disabling a user in Authentik blocks their media login |
| **Homarr** (Monarch dashboard) | `AUTH_PROVIDERS: "oidc"` with `AUTH_OIDC_*` set | ✅ **already OIDC-only** |
| **Dograh** | Authentik application `dograh` (provider 28) | ✅ **already OIDC-only** |
| **n8n · Grist · SigNoz · Workflow Studio · FreePBX/AvantFax · Technitium** | own local login | **Fronted by an `oauth2-proxy` gateway** (`innotel-app-gateway`) — see §1. Authentik is the only door on the public name |
| **The media stack** (Radarr, Sonarr, Lidarr, Whisparr, Bazarr, Prowlarr, qBittorrent, SABnzbd, Jellyseerr) | own local login | **Fronted by an `oauth2-proxy` gateway** (`monarch-media`); the apps run `AuthenticationMethod=External`, i.e. they trust the proxy and have no login of their own |
| **OmniRoute gateway** | local dashboard password | Fronted by `olympus-gateway-sso` at `gateway.olympus.innotel.us` (see `5-dev/olympus/docs/gateway-sso.md`); OmniRoute's own OIDC cannot be enabled — it strips the trailing slash from the issuer and Authentik's `iss` always ends with one |
| **MinIO** | access keys | Native OIDC exists (`MINIO_IDENTITY_OPENID_*`); object-store API keys are not a user login |
| **searxng · iptv · subscribe-portal · workflow-studio** | no login | n/a — nothing to convert (workflow-studio now has a gateway because the *app* it hangs off does) |

### Guest surfaces that are public on purpose

`subscribe.*.innotel.us` (the pricing/checkout portal) and the media *request*
page (`req.innotel.us`, now Jellyseerr through the gateway, so it requires an
Authentik session) are the only names that are meant to be reachable by people
with no platform account. `subscribe` is a marketing page — it has no login to
gate.

---

## 4. Verifying it, rather than trusting it

Two things have to be true for every gated host, and both are cheap to check:

```bash
# 1. the public name demands Authentik (302, with the right client id)
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://n8n.capstone.innotel.us/

# 2. no host anywhere carries a forward-auth gate any more
#    (via the NPM API: every proxy host's advanced_config must be empty of
#     auth_request, and there must be no outpost references)
```

Sign-in itself is verified by driving the **real** flow, not by inspecting
config. The probe used for this pass creates a throwaway Authentik identity,
runs Authentik's own flow executor for the client under test, follows the code
back through the edge, and asserts three things: a member signs in and reaches
the app, an identity in 28 groups also signs in (the cookie-size case), and a
non-member is **refused**. It deletes the identity on the way out — including
when a check fails.

Where that leaves the platform, measured:

```
edge admin (proxy.innotel.us)   1 group   PASS   callback 302 → UI 200, 1 session cookie
edge admin (proxy.innotel.us)   28 groups PASS   callback 302 → UI 200, 1 session cookie
edge admin (proxy.innotel.us)   no group  PASS   callback 403 (refused)
media (radarr.monarch…)         same three cases, PASS
app (n8n.capstone…)             same three cases, PASS
Vault (secrets.cerulean…)       member    PASS   Vault token issued, listed + read the store
Vault (secrets.cerulean…)       non-member PASS  refused at Authentik before any token
```

`npm-proxy-hosts.py --check` in each zone remains the drift check for the host
map. The end-to-end regression tests are committed per zone, so this posture is
a test result rather than a claim:

| Test | Covers |
|---|---|
| `1-primary/cerulean/scripts/verify-sso.py` | the four edge admin names, Vault's redirect + `auth_url`, the console bind, the session-store bind, the app's password endpoint |
| `2-voice/capstone/scripts/verify-sso.py` | n8n, Grist, SigNoz, Workflow Studio, FreePBX, Technitium, plus the loopback-only ports and the session store |
| `3-media/monarch/scripts/verify-sso.py` | the nine media apps on both Jellyseerr names, plus the loopback-only ports |
| `1-primary/magnate/scripts/verify-sso.py` | Magnate's own OIDC-only admin sign-in |

Each creates a throwaway Authentik identity (and a second one outside the
required group), drives that zone's real authorization-code flow per target,
asserts the sealed session opens the app, asserts the non-member is refused, and
deletes the identities on the way out. Exit codes are 0 pass / 1 fail / 2 cannot
run, so they can be wired into a check job as-is.

---

## 5. Closed since the first pass

Every item the earlier pass left open is now shut, and each closure has a
committed test behind it (the table in §4).

1. **The apps' own ports are loopback-only.** n8n `:5678`, Grist `:8484`,
   SigNoz `:3301`, Workflow Studio `:8090` and FreePBX `:8083` now publish on
   `127.0.0.1`, so their gateway is the only door — which is what makes their
   own login forms being switched off safe. Every reference that used to reach
   them across the LAN was moved first: n8n writes to Grist as
   `http://grist:8484` on `interview-net`, Zeus's portal dials the PBX by
   container name (`http://zeus-freepbx`, it shares `pbx-net`), that same name
   is the PBX gateway's upstream, and host-side scripts keep dialling
   `127.0.0.1`.
2. **Technitium's console is off the LAN.** It is host-networked, so compose
   cannot restrict it — but Technitium can: `webServiceLocalAddresses` is now
   `127.0.0.1,172.17.0.1`. Containers dial `http://172.17.0.1:5380` (the
   docker0 gateway, reachable from every bridge), the LAN address is refused,
   and the public door stays `dns.internal.innotel.us` behind its gateway.
   `scripts/setup.sh` enforces the setting on an existing config directory,
   since the environment variable is only read on first start.
3. **The shared SSO session store is off the LAN.** `cerulean-sso-sessions`
   was published on the LAN address; it now publishes on loopback + docker0,
   and each zone's gateways set `SSO_SESSION_REDIS_HOST=172.17.0.1`.
4. **Vault's UI lands on OIDC.** Vault's `sys/config/ui` is Enterprise-only, so
   there is no server-side default method, and its ember router moves to
   `/ui/vault/auth` client-side — a redirect on that path would never fire. The
   redirect therefore catches the document entry points: `npm-proxy-hosts.py`
   now renders a per-host `advanced_config`, and the `secrets` host uses it to
   send `/` and `/ui/` to `/ui/vault/auth?with=oidc`. The role is named in
   `/auth/oidc/config` as `default_role`, so the form's Role field can stay
   empty.
5. **Zeus's portal had a dead PBX integration.** The running compose file
   (`docker-compose.yml`, not the full variant) never set `FREEPBX_URL`, so
   `/api/health` reported `freepbx_api` and `avantfax` **down**. Both now point
   at the container name and the endpoint is green.

## 6. Deliberately still open

1. **OmniRoute (`:20128`) and the Asterisk plane** (`5060/5061`, `8088/8089`,
   `5038`, `10000`, RTP) stay reachable on the LAN. OmniRoute is dialled by the
   browser; the telephony ports are how the PBX works. The AMI (`5038`) and ARI
   (`8088`) bind the LAN address only — never `0.0.0.0` — because their ACLs
   admit the LAN subnet and nothing else.
2. **qBittorrent's `:6881`** is the BitTorrent peer port. Its Web UI (`:8080`)
   is loopback; the peer port has to be reachable or downloading stops.
3. **NPM's admin port `:81`** still answers on the LAN, but it is not a
   bypass: the fork believes identity headers only from a loopback peer and its
   password form is switched off, so a client on the LAN is told to open the
   edge hostname and can go no further.
4. **Vault's token tab** cannot be removed (break-glass); the UI now merely
   avoids it by default.
5. **`omniroute.capstone.innotel.us` does not resolve** — OmniRoute is declared
   `optional: true` in the Capstone host map and its dashboard has no NPM host,
   so there is no edge to gate. The provider is ready if it is published.

---

## Appendix — host reconcile (unrelated to sign-in)

Every compose project on the host runs under the name its canonical file pins
and from the canonical directory; nothing is launched with `-p <legacy-name>`
any more.

```
capstone-voice-aiagent-platform  ->  capstone    (23 containers)
cerulean-dns-platform            ->  cerulean    (10)
monarch-media-platform           ->  monarch     (14)
zeus-pbx-platform                ->  zeus        (3)
```

Named volumes were copied to the canonical prefix before the cutover
(`capstone-*` 13 volumes, `cerulean_infisical-*` 2), the containers recreated
from the canonical compose with the same profiles, and the containers'
anonymous volumes (image `VOLUME`s: searxng's `/etc/searxng` + cache, the n8n
sandbox's dind `/var/lib/docker`, the ClickHouse keeper's
`/var/lib/clickhouse`) copied into their replacements and restarted. Zeuss
needed no volume copy — `pbx-*` and `zeus-portal-data` are declared with
explicit `name:` — and Monarch none either, because its canonical
`monarch_clipbucket_*` volumes already existed.

Two things to know:

* **The legacy volumes are gone.** All 18 pre-migration volumes were deleted
  after every canonical counterpart was confirmed present with `du`-comparable
  or larger contents and no container referenced one. Rollback is now the copied
  volumes alone.
* **Do not `up -d` Monarch without a service list.** Its compose also defines
  `requestrr`, `flaresolverr`, `clipbucket`, `monarch-recs`, `monarch-health`,
  `watchtower` and the `legacy`/`npm` profiles, none of which run on this host;
  a bare `up -d` would start seven extra containers.

Also fixed on the way: the Capstone project's recorded compose file used to live
at a path that no longer existed (18 containers were unmanageable) and Docker had
re-created that path **as empty directories** where three config *files* are
mounted — `n8n-grader-workflow.json` was mounted as a directory, so the import
job could not read it. The five legacy root paths remain as **symlinks** to the
canonical dirs.

The one host still on a non-canonical image mix on purpose: Capstone's
`dograh-api` runs the published `ghcr.io/innotelinc/dograh-api:latest` while
`dograh-ui` runs the locally built `dograh-local/dograh-ui:capstone`, because
`docker-compose.dograh-build.yml` is only applied to the UI. Recreating the API
from that override swaps it to the local build — pass `--no-build` if you ever
do, or the override triggers a full source build.
