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

**One store, three networks — and now three hosts.** The gateways live in three
different Docker networks (the edge's own, Monarch's, Capstone's), so the session
store is published on the edge host — `cerulean-sso-sessions`,
`192.168.1.46:16380`, on loopback, docker0 *and* the LAN address — with a
password that is the whole control on that listener (an empty one refuses to
start). Sessions are the only thing in it, there is no volume, and losing it
costs a re-login.

The LAN bind is not decoration. Capstone, Monarch and Olympus were split onto
servers of their own, and **neither the edge's loopback nor its docker0 gateway
is reachable across a host boundary** — so each zone that moved away must set
`SSO_SESSION_REDIS_HOST` to `192.168.1.46`. `172.17.0.1` is correct only on the
edge host itself, where it names the store directly; anywhere else it names that
host's *own* empty docker0 and every gateway exits on
`dial tcp 172.17.0.1:16380: connect: connection refused`. Olympus is the one
zone that does not share this store at all: it runs its own
(`olympus-gateway-sso-sessions`) with its own cookie secret, so a sign-in there
is a second sign-in.

### The gateway inventory

| Host(s) | Gateway | Listen (host) | Upstream | Authentik client |
| --- | --- | --- | --- | --- |
| `proxy.innotel.us`, `admin.zeus`, `admin.monarch`, `admin.signara` | `cerulean-npm-sso` | `127.0.0.1:4180` (the edge's own netns) | NPM admin UI `127.0.0.1:81` | `npm-edge` |
| `radarr` · `sonarr` · `lidarr` · `whisparr` · `bazarr` · `prowlarr` `.monarch.innotel.us` | `{radarr,sonarr,lidarr,whisparr,bazarr,prowlarr}-sso` | `14001`–`14006` | `http://<app>:<port>` | `monarch-media` |
| `qbittorrent` · `sabnzbd` `.monarch.innotel.us` | `qbittorrent-sso`, `sabnzbd-sso` | `14007`, `14008` | `http://qbittorrent:8080`, `http://sabnzbd:8080` | `monarch-media` |
| `req.monarch.innotel.us`, `req.innotel.us` | `jellyseerr-sso` | `14009` | `http://jellyseerr:5055` | `monarch-media` |
| `media.innotel.us`, `media.magnate.innotel.us` | `jellyfin-sso` | `14010` (`.56`) | `http://jellyfin:8096` | `monarch-media` |
| `tube.innotel.us` | `clipbucket-sso` | `14011` (`.56`) | `http://clipbucket:80` | `monarch-media` |
| `tv.monarch.innotel.us` | `iptv-sso` | `14012` (`.56`) | `http://iptv:3000` | `monarch-media` |
| `requestrr.monarch.innotel.us` | `requestrr-sso` | `14013` (`.56`) | `http://requestrr:4545` | `monarch-media` |
| `n8n.capstone.innotel.us` | `n8n-sso` | `14010` | `http://n8n:5678` | `innotel-app-gateway` |
| `grist.capstone.innotel.us` | `grist-sso` | `14011` | `http://grist:8484` | `innotel-app-gateway` |
| `grafana.capstone.innotel.us` | `grafana-sso` | `14012` | `http://grafana:3000` | `innotel-app-gateway` |
| `workflow.capstone.innotel.us` | `workflow-sso` | `14013` | `http://workflow-studio:8090` | `innotel-app-gateway` |
| `pbx.capstone`, `pbx.innotel.us`, `pbx.zeus`, `fax.zeus` | `pbx-sso` | `14014` | `http://192.168.1.46:8083` (FreePBX) | `innotel-app-gateway` |
| `dns.internal.innotel.us` | `technitium-sso` | `14015` | `http://192.168.1.46:5380` | `innotel-app-gateway` |

Host ports are *per host*, and `14010`–`14013` are now used twice: they are
Capstone's app gateways on `.30` and the four media gateways above on `.56`. The
edge forwards each name to the port on the machine that runs that stack, so the
repeat is not a conflict — but a table like this is the wrong place to look for
one, which is why the host is named where it is not the edge's.

Each gateway's deployment file is the stack that owns the app: the media
gateways in `3-media/monarch/docker-compose.yml`, the app gateways in
`2-voice/capstone/docker-compose.yml`, and the edge's own in
`1-primary/npm/compose.cerulean.yml`. **`technitium-sso` is the exception, and
it is the reason its row reads `.46`:** it used to run in Group 2 beside
Capstone, which worked only while every product shared one host — the console it
fronts is loopback-bound, so when the voice stack moved to a server of its own
the gateway went with it and could not reach anything, and
`dns.internal.innotel.us` answered 502 to anyone who had just signed in. It now
deploys with the console, from `1-primary/cerulean/docker-compose.yml` under the
`technitium` profile.

**Skip-auth routes**, i.e. what stays reachable without a session:

| Gateway | Open paths | Why |
| --- | --- | --- |
| `n8n-sso` | `^/webhook/`, `^/webhook-test/`, `^/healthz$` | Receiving webhooks is n8n's purpose; an interactive login in front of them would break every caller rather than add a check. The editor and the REST API stay gated. |
| `grafana-sso` | `^/api/health$` | liveness. Spans arrive at the OTel collector's own ports (`4317`/`4318`), are converted to metrics in Prometheus, and are not stored — so there is no trace UI to gate behind this name. |
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
| **n8n · Grist · Grafana · Workflow Studio · FreePBX/AvantFax · Technitium** | own local login | **Fronted by an `oauth2-proxy` gateway** (`innotel-app-gateway`) — see §1. Authentik is the only door on the public name |
| **The media stack** (Radarr, Sonarr, Lidarr, Whisparr, Bazarr, Prowlarr, qBittorrent, SABnzbd, Jellyseerr) | own local login | **Fronted by an `oauth2-proxy` gateway** (`monarch-media`); the apps run `AuthenticationMethod=External`, i.e. they trust the proxy and have no login of their own. **Two of them keep a second credential store the gateway cannot see**, so gating the name is not the whole job — see the note under the table |
| **Jellyfin · Clipbucket · the IPTV guide · Requestrr** | own login (Jellyfin through the LDAP outpost, the other three their own forms) | **Fronted by a gateway since 2026-09-16** — the three names that answered with no gate at all (`media.*`, `tube.*`, `tv.monarch.*`) plus the Discord bot's console. Each app is bound to `127.0.0.1` on the media host, so its gateway is the only door, not one of two |
| **OmniRoute gateway** | local dashboard password | Fronted by `olympus-gateway-sso` at `gateway.olympus.innotel.us` (see `5-dev/olympus/docs/gateway-sso.md`); OmniRoute's own OIDC cannot be enabled — it strips the trailing slash from the issuer and Authentik's `iss` always ends with one |
| **MinIO** | access keys | Native OIDC exists (`MINIO_IDENTITY_OPENID_*`); object-store API keys are not a user login |
| **searxng · iptv · subscribe-portal · workflow-studio** | no login | n/a — nothing to convert (workflow-studio now has a gateway because the *app* it hangs off does) |

**The gateway gates the name; it does not close the app's own store.** Two of the
media apps keep credentials outside Authentik, and each is a way in that no
gateway covers — the reason a user disabled in Authentik could still sign in.
Neither is configuration in the estate's repos, so each has a script (committed,
unit-tested, and run by `3-media/monarch/scripts/drift-check.sh`):

| App | What it would otherwise keep | Posture now |
|---|---|---|
| **Jellyseerr / Seerr** | `main.localLogin` — "Enable Local Sign-In": email and password in Seerr's own store (Seerr has **no OIDC support**, measured: no `openid` anywhere in its server or sources) | `scripts/seerr-login-methods.py` — off, and `--check` fails while it is on. Jellyfin sign-in stays: it is the Cerulean identity (LDAP), and it is how a user obtains a Seerr session without a second password |
| **Jellyfin** | accounts in Jellyfin's own database rather than the LDAP outpost | `scripts/jellyfin-login-methods.py` — the only permitted local account is the break-glass `admin`; strays are *disabled* (reversible, history survives), never deleted |

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
| `2-voice/capstone/scripts/verify-sso.py` | n8n, Grist, Grafana, Workflow Studio, FreePBX, Technitium, plus the loopback-only ports and the session store |
| `3-media/monarch/scripts/verify-sso.py` | the fifteen media names (the nine apps on both Jellyseerr names, plus `media.*`, `media.magnate.*`, `tube.*`, `tv.monarch.*`, `requestrr.monarch.*`), the non-member refusal, and every loopback-only app port |
| `3-media/monarch/scripts/seerr-login-methods.py` · `jellyfin-login-methods.py` | the two apps' *own* credential stores: Seerr's local sign-in off, and no Jellyfin-local account signable other than the break-glass `admin` (their unit tests run in that repo's CI; `drift-check` runs them as checks) |
| `1-primary/signara/scripts/verify-sso.py` | Signara's API flow end to end, its application-binding refusal, and that no password endpoint exists |
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
   Grafana `:3301` (the port SigNoz held), Workflow Studio `:8090` and FreePBX `:8083` now publish on
   `127.0.0.1`, so their gateway is the only door — which is what makes their
   own login forms being switched off safe. Every reference that used to reach
   them across the LAN was moved first: n8n writes to Grist as
   `http://grist:8484` on `interview-net`, Zeus's portal dials the PBX by
   container name (`http://zeus-freepbx`, it shares `pbx-net`), that same name
   is the PBX gateway's upstream, and host-side scripts keep dialling
   `127.0.0.1`.
2. **Technitium's console is off the LAN, and signs in through Authentik.** It
   is host-networked, so compose cannot restrict it — but Technitium can:
   `webServiceLocalAddresses` is now `127.0.0.1,172.17.0.1`. Containers dial
   `http://172.17.0.1:5380` (the docker0 gateway, reachable from every bridge),
   the LAN address is refused, and the public door stays
   `dns.internal.innotel.us` behind its gateway. `scripts/setup.sh` enforces the
   setting on an existing config directory, since the environment variable is
   only read on first start.

   The gateway there is only half of it, and this is the one surface where that
   distinction is visible: the console has a login of its own, and a gateway in
   front of it can prove *someone* signed in but never *who* — so the DNS/DHCP
   admin plane kept a password that Authentik does not know about. Technitium
   speaks OIDC itself, so `scripts/technitium-sso.py` points its own sign-in
   (Settings → Single Sign-On) at the `technitium` provider with
   `cerulean-platform → Administrators` mapped. `verify-sso.py` asserts it: the
   console's `/sso/login`, asked with the headers the gateway sends, must leave
   for this IdP as the client the provider has registered and for the callback it
   has registered. The local password form stays on purpose — DNS is what
   resolves the IdP, so an SSO-only console is one nobody can reach on the day DNS
   is what broke.
3. **The shared SSO session store is password-guarded, and published where it
   has to be.** `cerulean-sso-sessions` listens on the edge host's loopback,
   its docker0 and its LAN address (`192.168.1.46:16380`). The LAN bind is what
   lets a stack that moved to another server keep the same session: the other
   two bindings are unreachable from there, and a zone whose gateways were left
   on `172.17.0.1` (Monarch's were — see the live audit below) never reaches the
   store at all. The password is the whole control on that listener, and an
   empty one refuses to start.
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

## 5. Live audit after the split (2026-09-16)

Re-checked against the hosts the stacks actually run on — `.46` (primary + edge),
`.30` (voice), `.50` (factory), `.56` (media) — rather than against the files.

**Cerulean Authentik is the only login on every service, and no service's own
login form is reachable off its own host.** What that rests on, per surface:

| Surface | Why the form is unreachable |
|---|---|
| The media apps (`radarr`, `sonarr`, `lidarr`, `whisparr`, `bazarr`, `prowlarr`, `qbittorrent`, `sabnzbd`, `jellyseerr`) | each app is bound to `127.0.0.1:<port>` and runs `AuthenticationMethod=External` — the app has **no login of its own**, and the only LAN-listening door is its `oauth2-proxy` gateway on `14001`–`14009` |
| `n8n`, `grist`, `grafana`, `workflow-studio`, FreePBX/AvantFax, Technitium | fronted by their gateway on `14010`–`14015`; the upstream is loopback or a container name, never a published port |
| First-party apps (Magnate, Cerulean, Distro, Studio, Zeus portal, Rizz Aura, Capstone dashboard) | password path refuses unless `BREAKGLASS_LOGIN=1` is set **on that app's own host** (Zeus: `AUTH_MODE`); Studio has no local path at all |
| NPM Edge admin UI | `cameFromEdge()` + `identityAllowed()` gate `POST /tokens/sso`, and `passwordGrantAllowed()` refuses `POST /tokens` on every edge request — a password grant from inside the edge returns 401 (verified: it is what stops a scripted edge edit without the SSO route) |
| Vault, Jellyfin, Homarr, Dograh, OmniRoute dashboard | Vault via native OIDC (group-bound role); Jellyfin via the **Cerulean LDAP outpost** so only Cerulean identities exist; Homarr and Dograh OIDC-only; the gateway dashboard behind `olympus-gateway-sso` |

### Two surfaces still present a form, and why that is a decision, not a gap

1. **Jellyfin** — its login form is the LDAP bind: the fields are Cerulean
   credentials and a disabled Authentik user cannot sign in, but the page itself
   is still a form. **Taken the other way on 2026-09-16** (the call this section
   said was the owner's): the public names are fronted by `jellyfin-sso` and the
   app is bound to `127.0.0.1:8097`, so the form is no longer reachable off the
   host, and a browser still lands on it *after* the gateway — two prompts, one
   identity. The cost is the path the section warned about: **native TV/mobile
   clients speak the Jellyfin API, not a browser OIDC flow**, so a client that
   cannot open a sign-in page cannot reach Jellyfin by name any more. Bringing
   them back is a skip-auth rule for their own tokens at the gateway
   (`X-Emby-Token` / an API key), not re-opening `8097` — re-opening it
   republishes exactly the form the gateway exists to remove.
2. **FreePBX/AvantFax** — the GUI is already loopback-only and reachable only
   through `pbx-sso`. Its local admin login is the **only** recovery path when
   Authentik or the gateway is down (short of `fwconsole` over SSH); deleting it
   is a break-glass decision.

**Both smaller reachable forms are closed (2026-09-16).** `clipbucket` (`:8098`)
and `requestrr` (`:4545`) were the last two ports on `.56` answering off-host
with their own logins. Each got both halves of the fix this section offered — a
gateway (`clipbucket-sso` at `14011`, `requestrr-sso` at `14013`) *and* a
loopback bind — so the gateway is the only door rather than one of two.
`requestrr` also gained the name it had never had:
`requestrr.monarch.innotel.us`, an `npm-hosts.conf` row because it is inside
`MONARCH_DOMAIN` (the `media.*` and `tube.*` names are not, which is why those
stay edge changes). The Homarr tile that pointed at `<host>:4545` now points at
that name.

One step is easy to miss when a name is added, and its symptom points at the
wrong thing: the gateway's redirect URI must be **registered on the provider**,
not merely listed in the app's `.env`, or the sign-in dies at the callback with
`invalid_request: redirect_uri does not match` before any login form appears.
`MONARCH_SSO_REDIRECT_URIS` in `3-media/monarch/.env.example` is the list — it
now carries all seventeen callbacks — and the command that registers it is
written beside the variable.

### The store's address is the one thing a moved gateway cannot guess (2026-09-16)

The split moved the gateways but not the store, so the store's address is the one
thing a zone cannot derive from its own environment. It is also easy to be *wrong
about*: oauth2-proxy opens its Redis connection lazily, on the first session it
actually stores, so a gateway's absence from the store's client list is not
evidence of anything.

That mistake was made here first, then corrected. `redis-cli client list` on the
store showed:

```
7 addr=192.168.1.30     # Capstone's app gateways — connections dated to their restart
1 addr=127.0.0.1        # the edge's own gateway
0 addr=192.168.1.56     # Monarch's media gateways
```

and it read as "Monarch's gateways cannot reach the store". They can: a bare `nc`
run inside `radarr-sso`'s own network namespace reaches `192.168.1.46:16380`, and
`.56`'s `.env` already carried `SSO_SESSION_REDIS_HOST=192.168.1.46`. The `.30`
connections are the ones that had been *used*; the media gateways had simply not
stored a session since they were restarted. What settles the question is the
zone's own test, which drives a real authorization-code flow per target — and
both moved zones pass it.

Where that leaves the config: `SSO_SESSION_REDIS_HOST` must be the edge host's LAN
address on every zone that does not run the store. Monarch's `.env.example`
shipped the wrong value (`172.17.0.1` — that zone's own docker0, which holds no
store) and Capstone's template said nothing host-specific needed pinning; both now
carry `192.168.1.46` with the reason. Both zones' `verify-sso.py` asserted the
store was *off* the LAN and answering on loopback — true only while every stack
shared one box, and false in both directions now — and instead assert it answers
where their gateways dial and refuses an unauthenticated `PING`.

#### Measured by running the flows, not by reading config (2026-09-16)

| Zone | Host | Result |
| --- | --- | --- |
| Media — 9 gateways, `req.innotel.us` and its alias | `.56` | **PASS**: every name issued `_innotel_sso` and opened, non-member refused `403`, app ports loopback-only, store answers and returns `NOAUTH` |
| App — n8n, Grist, SigNoz, Workflow Studio, FreePBX/AvantFax, Technitium | `.30` | **5 of 6 PASS**; `dns.internal.innotel.us` answers **502** — see below |
| Olympus — Studio on both its names | `.50` | **PASS**: both names drove a full PKCE flow, sealed `studio_session`, and returned the projects document |

> **A zone's test is only as good as the token it runs with.** Olympus's first
> run reported a failed sign-in whose cause was its own `AUTHENTIK_TOKEN` (a
> `vault://cerulean/olympus/authentik#AUTHENTIK_TOKEN` reference) answering
> `403 Token invalid/expired` when the test tried to create its throwaway
> identity — the client-credentials check still passed, and the run went green
> once a live token was supplied. That token needs re-minting, or the zone's
> regression test fails for a reason that has nothing to do with the zone.

**`dns.internal.innotel.us` is now broken by construction.** Its gateway
(`technitium-sso`) moved to `.30` with the rest of Group 2, but the console it
fronts stayed on `.46`: `cerulean-technitium` is `network_mode: host` and binds
`127.0.0.1` and `172.17.0.1` only, refusing the LAN address by design (§5.2), and
there is no mesh network between the hosts — so `.30` cannot reach it at all
(`192.168.1.46:5380` and its own `172.17.0.1:5380` both refuse). Closing this is a
posture choice, not a config typo:

- run `technitium-sso` on `.46`, where its upstream lives on loopback and docker0,
  and point `dns.internal.innotel.us` at that host — keeps the console off the LAN,
  which is the posture §5.2 established; or
- bind the console on `.46`'s LAN address and point the `.30` gateway at it —
  simpler, but a bind is per-interface: it re-exposes the DNS/DHCP admin console to
  the whole office network, where its gateway can be bypassed.

**Olympus is unaffected by any of this.** It runs its own gateway and its own
session store (`olympus-gateway-sso-sessions`, its own cookie secret), so a sign-in
there is a second sign-in rather than a shared one — the one zone that deliberately
does not ride this store, and the reason its gateway still works with the edge on
another host.
