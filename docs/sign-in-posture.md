# Sign-in posture — Authentik is the only identity

**Rule.** Identity lives in Cerulean's **Authentik** (IdentityOps). A platform
may *consume* identity; it may not keep a second user store or a second password
path. Every deployed surface is either **Authentik-only** or explicitly listed
below as an exception with the reason.

This document is the inventory of every login on the host, what enforces it, and
the break-glass path for each. It exists because "we use SSO" is not a fact you
can audit — a password form left in the template is a second identity store even
when nobody uses it.

> **Change of mechanism (2026-09-15).** Authentik **forward-auth is retired**.
> No NPM proxy host carries an `auth_request`/`/outpost.goauthentik.io/...`
> gate any more, and the per-zone outpost providers are superseded. Every
> surface that cannot speak OIDC itself is instead fronted by a real OIDC
> relying party — an **`oauth2-proxy` gateway sidecar** that runs the browser
> through a code flow against Cerulean Authentik and shares one `.innotel.us`
> session cookie. The first one is the NPM admin UI
> (`cerulean-npm-sso`, see `1-primary/npm/docs/stack.md`). Sections below that
> describe forward-auth/outpost wiring are kept as history and are no longer the
> deployed state.

---

## 1. First-party apps — all Authentik-only

Enforcement is always **server-side**. Hiding a form is presentation; the
handler that mints the session is the control.

| App | Surface | Posture | Enforced in | Break-glass |
|---|---|---|---|---|
| **Magnate** | `/admin`, `/admin/login` | Authentik OIDC only | `lib/auth.ts` `breakglassLoginEnabled()` gates `app/api/admin/login/route.ts`; `components/AdminLogin.tsx` renders the form only when on | `BREAKGLASS_LOGIN=1` |
| **Cerulean** | portal (all `/api/*` behind a session) | Authentik OIDC only | `server/src/config.ts` `auth.localEnabled` (default **false**), `server/src/routes.ts` `POST /auth/login` → 403 | `BREAKGLASS_LOGIN=1` (legacy alias `AUTH_LOCAL_ENABLED=1`) |
| **Distro** | control plane `/admin`, `/login` | Authentik OIDC only | `src/oidc.js` `localLoginEnabled()`, `src/http.js` gates `/api/auth/signup` + `/api/auth/login` → 403 | `BREAKGLASS_LOGIN=1` |
| **Olympus (Studio)** | Studio UI | Authentik OIDC (PKCE) — **no local path exists** | `web/studio/lib/auth.ts` | none needed; the recovery path is re-provisioning the `studio` app |
| **Zeus** | portal | Authentik when the `AUTHENTIK_*` vars are set (`AUTH_MODE` auto) | `src/lib/oidc.ts` `passwordLoginEnabled()`, `app/api/auth/login/route.ts` → 403 | `AUTH_MODE=both` (button + form) or `freepbx` |
| **Rizz Aura** | app / rankings / community / admin | Authentik OIDC only — **no password store at all** | `api/auth.mjs` (every frontend redirects to `api` for login) | none |
| **Capstone dashboard** | dashboard | Authentik OIDC only | `dashboard-backend/app/main.py` `/auth/login` is an OIDC redirect | none |
| **NPM Edge (the admin UI)** | `proxy.innotel.us`, `admin.zeus`, `admin.monarch` | Authentik OIDC only (via the `oauth2-proxy` gateway `cerulean-npm-sso`) — **no password path on the edge** | `backend/lib/sso.js`: `cameFromEdge()` (loopback) + `identityAllowed()` gate `POST /tokens/sso`; `passwordGrantAllowed()` refuses `POST /tokens` on every edge request | `BREAKGLASS_LOGIN=1`, off the edge (the LAN admin port) |

### The break-glass convention

`BREAKGLASS_LOGIN=1` re-enables the local password path for **recovery only** —
when Authentik is unreachable and you need to get in to fix it. It is off unless
explicitly set, and it is read server-side, so the form and the endpoint agree.

Recovery runbook:

```bash
# 1. On the affected app's host, set the flag and restart that one service.
#    e.g. Distro:
cd 5-dev/distro && sed -i 's/^BREAKGLASS_LOGIN=.*/BREAKGLASS_LOGIN=1/' .env
docker compose up -d control-plane

# 2. Sign in with the local credential, fix Authentik, sign out.

# 3. Unset it and restart — the password path closes again.
sed -i 's/^BREAKGLASS_LOGIN=.*/BREAKGLASS_LOGIN=/' .env
docker compose up -d control-plane
```

Cerulean keeps the older name for the same switch: set `AUTH_LOCAL_ENABLED=1`
(or `BREAKGLASS_LOGIN=1`) and restart.

Zeus already had this shape as `AUTH_MODE`; `AUTH_MODE=both` is its break-glass.

NPM Edge is the one case where the switch is *not* the whole story: its
password grant is refused on every request that came from the edge, including
when `BREAKGLASS_LOGIN=1` is set, because a door to the admin UI is SSO-only by
construction. The way back in is off the edge — the admin port on the LAN
(`http://<host>:81`) — which is also why that port stays published.

---

## 2. Third-party apps — what each actually needs

These are upstream projects; "make it SSO-only" means one of three things, and
only the first is a configuration change.

| App | Today | SSO posture |
|---|---|---|
| **Jellyfin** | Authentik **LDAP outpost** (`jellyfin-ldap`) — logins resolve against Cerulean users, `paid_users` gates access | ✅ **already SSO.** Disabling a user in Authentik blocks their media login |
| **Homarr** (Monarch dashboard) | `AUTH_PROVIDERS: "oidc"` with `AUTH_OIDC_*` set | ✅ **already OIDC-only** |
| **Dograh** | Authentik application `dograh` (provider 28) | ✅ **already OIDC-only** — `AUTH_PROVIDER=oidc`, client `dograh`, and the container's `AUTHENTIK_CLIENT_SECRET` matches the provider's, so `/api/v1/auth/oidc/login` → Authentik `authorize` with PKCE |
| **OmniRoute gateway** | local dashboard password (`OMNIROUTE_INITIAL_PASSWORD`) | Fronted by the **Capstone-zone forward-auth** provider (`omniroute.capstone.innotel.us`) |
| **FreePBX** | local admin (`pbx.freepbx` credentials) | No OIDC support. Fronted by the **Zeus-zone forward-auth** provider at `pbx.zeus.innotel.us` |
| **n8n** (Capstone) | local owner account | OIDC/SAML require n8n **Enterprise**; fronted by the **Capstone-zone forward-auth** provider |
| **Grist** (Capstone) | local account | OIDC requires **Enterprise**; fronted by the **Capstone-zone forward-auth** provider |
| **SigNoz** (Capstone) | local admin | SSO requires **Enterprise**; fronted by the **Capstone-zone forward-auth** provider |
| **Infisical** | local admin | **Retired** SecretOps — no repo runs the profile any more, so there is no Infisical login left to inventory. Its replacement is the gate that matters — `secrets.cerulean.innotel.us` (Vault) is fronted by the **Cerulean-zone forward-auth** provider |
| **Technitium** | `TECHNITIUM_ADMIN_PASSWORD` | No OIDC. Keep local (DNS admin, not user-facing) or forward-auth the console |
| **MinIO** | access keys | Native OIDC exists — set `MINIO_IDENTITY_OPENID_*`; object-store API keys are not a user login |
| **searxng · iptv · subscribe-portal · workflow-studio** | no login | n/a — nothing to convert |

**Forward-auth is the established pattern here, not a workaround**, and it is
now wired for **every zone on the host**. Authentik runs one proxy outpost (*authentik
Embedded Outpost*) carrying a **domain-level** provider per zone — the external host is
the zone's `auth.` name and `cookie_domain` is the zone itself, so one provider gates
every app underneath it:

| Zone | Provider | `cookie_domain` |
|---|---|---|
| capstone | `capstone-npm-forward-auth` (24) | `capstone.innotel.us` |
| monarch | `monarch-npm-forward-auth` (26) | `monarch.innotel.us` |
| zeus | `zeus-npm-forward-auth` (32) | `zeus.innotel.us` |
| olympus | `olympus-npm-forward-auth` (33) | `olympus.innotel.us` |
| cerulean | `cerulean-zone-npm-forward-auth` (34) | `cerulean.innotel.us` |

All five are attached to the embedded outpost and were verified by driving the
outpost's `/outpost.goauthentik.io/auth/nginx` endpoint with each app's host
(`Host` + `X-Forwarded-Host`, exactly as the nginx snippet does): every protected
host answers **401** unauthenticated — `n8n`, `grist`, `signoz`, `omniroute`,
`pbx.capstone`, `pbx.zeus`, `secrets.cerulean`, `proxy.innotel.us` — while
leaving the identity provider's own hosts alone.

The other half is the **edge**, and it is live for the Capstone zone. Driving the
public hostnames shows the real gate:

```
n8n.capstone.innotel.us     302 -> https://auth.capstone.innotel.us/outpost.goauthentik.io/start?rd=...
grist.capstone.innotel.us   302 -> ... (same outpost)
signoz.capstone.innotel.us  302 -> ... (same outpost)
```

That is `auth_request` already wired in NPM — Capstone's
`scripts/npm-proxy-hosts.py` renders the snippet and defaults forward-auth **on**
— so n8n, Grist and SigNoz are Authentik-gated **today**. The last two ungated
hosts were closed the same way: NPM API credentials do exist after all, in
`1-primary/cerulean/.env`, so it was a scripted change rather than a console one.

| Host | Before | Now |
|---|---|---|
| `pbx.zeus.innotel.us` | 302 → its own `/admin` (FreePBX login) | 302 → `auth.zeus.innotel.us/outpost.goauthentik.io/start` |
| `secrets.cerulean.innotel.us` | 307 → its own `/ui/` (Vault login) | 302 → `auth.cerulean.innotel.us/outpost.goauthentik.io/start` |
| `proxy.innotel.us` (the NPM admin UI) | its own login form | 302 → `auth.innotel.us/outpost.goauthentik.io/start` |

Nothing reaches those hosts programmatically — both are interactive UIs, so
gating them cannot break an integration. The snippet is now rendered by the
provisioners themselves: Zeus's and Cerulean's `scripts/npm-proxy-hosts.py` carry
the same `FORWARD_AUTH_SNIPPET` as Capstone's, so re-running any of them
re-applies the gate instead of wiping it. `forward_auth: False` opts a host out —
`auth`/`dns`/`certs`/`admin` are the Cerulean app itself, and gating `auth` would
lock the zone out of its own IdP.

#### The bare `innotel.us` zone

`proxy.innotel.us` is the first gated host in the **bare** `innotel.us` zone, and
that is why it needed more than an NPM edit. The embedded outpost resolves the
app from the forwarded `Host`, so it only authorizes hosts a `forward_domain`
provider's `cookie_domain` actually covers; the five existing providers covered
`capstone`/`cerulean`/`monarch`/`olympus`/`zeus` only. Gating the host without one
made the auth subrequest fail and NPM return **500** — the same shaped failure as
the earlier `forward_single` mistake.

The zone provider was created with the generic provisioner (which is parameterised
by `MONARCH_DOMAIN` and is not Monarch-specific):

```
MONARCH_DOMAIN=innotel.us \
  AUTHENTIK_FORWARD_PROVIDER="Innotel Zone NPM Forward Auth" \
  AUTHENTIK_FORWARD_APP_SLUG=innotel-npm-forward-auth \
  AUTHENTIK_FORWARD_GROUP=cerulean-platform \
  python3 3-media/monarch/scripts/authentik-forward-auth.py
```

(`forward_domain`, `cookie_domain=innotel.us`, `external_host=https://auth.innotel.us`,
provider pk 35, application `innotel-npm-forward-auth`, attached to the embedded
outpost.) Its `external_host` is the **base** `auth.innotel.us` that already
existed, which is why the redirect lands there and not on a zone IdP.

The gate is **not** open to any authenticated user: the application carries a
policy binding to the **`cerulean-platform`** group, so only its members pass.
This is the one place the provisioner had a silent gap — it looked
`AUTHENTIK_FORWARD_GROUP` up and printed `PASS`, but never created the binding,
so setting the var restricted nothing. `authentik-forward-auth.py` now binds the
application (and `--check` reports a missing binding as drift).

**Every NPM forward-auth gate is now group-scoped**, each to its own zone's
group — the pattern `capstone-npm-forward-auth` already followed:

| Application | Gates | Group |
|---|---|---|
| `capstone-npm-forward-auth` | 7 `capstone` hosts | `Capstone` |
| `cerulean-zone-npm-forward-auth` | `secrets.cerulean`, `admin.zeus` | `Cerulean` |
| `monarch-npm-forward-auth` | 10 `monarch` hosts (the *arr apps + `admin`) | `Monarch` |
| `zeus-npm-forward-auth` | `pbx.zeus`, `admin.zeus` | `Zeus` |
| `olympus-npm-forward-auth` | (none gated yet) | `Olympus` *(created)* |
| `innotel-npm-forward-auth` | `proxy.innotel.us` | `cerulean-platform` |

The bare `innotel.us` zone is the exception: it is the platform-wide admin edge,
not a product, so it binds the platform-operator group rather than a product
group. There was **no `Olympus` group** — it was created (plain, matching the
other product groups) so the olympus provider is not left as a permanent "any
authenticated user" hole for the first host that gets gated there. The host's
zone decides which application governs it, so `admin.zeus.innotel.us` is gated by
the **zeus** application even though its snippet's sign-in URL points at
`auth.cerulean.innotel.us`.

A second, unrelated defect surfaced while verifying: Authentik's application
**list** endpoint can silently omit an application — it reported `count=32` with
31 results and no next page, and `?search=olympus-npm-forward-auth` returned
`count=1` with **zero** results — while a direct slug `GET` resolved it fine.
The provisioner looked applications up through that list, so `--check` reported
the perfectly healthy `olympus-npm-forward-auth` as *missing*. `Ak.get_application()`
now does a direct slug `GET` and only falls back to the list, and all six gates
`--check` clean. It is **not** caused by the group binding — the app stayed
unlisted with the binding deleted. `3-media/monarch/scripts/tests/test_authentik_forward_auth.py`
now pins all three behaviours (binding creation, drift detection, slug
fallback); mutating `get_application` back to a list-only lookup fails it.

#### Verified end-to-end, not just inspected

`1-primary/cerulean/scripts/verify-forward-auth.py` creates a throwaway
Authentik identity, drives Authentik's real authentication flow against the
live edge, and deletes the identity on the way out — including when a check
fails. It now covers all seven gated hosts in two phases: with the identity in
**no** group every gate must refuse it, then each group is added **one at a
time** and must open exactly the hosts bound to it. Adding every group at once
would not catch a gate bound to the *wrong* group — the mistake that matters.

```
[2] a non-member (no groups) is refused by every gate        # 7/7 refused
[3] each group opens exactly the hosts bound to it
    + Capstone          -> n8n.capstone.innotel.us
    + Cerulean          -> secrets.cerulean.innotel.us
    + Monarch           -> admin.monarch.innotel.us, radarr.monarch.innotel.us
    + Zeus              -> admin.zeus.innotel.us, pbx.zeus.innotel.us
    + cerulean-platform -> proxy.innotel.us
PASS — 7 gated hosts: refused to a non-member and open to a member
```

That run found a **pre-existing bug** rather than confirming the bindings:
`admin.zeus.innotel.us` — a *second* edge door to the NPM admin UI, forwarding
to `:81` like `proxy.innotel.us` and the working `admin.monarch.innotel.us` —
signed in at `auth.cerulean.innotel.us`. The sign-in host is what selects the
provider, and that provider's cookie is scoped to `cerulean.innotel.us`, which a
`zeus.innotel.us` host can never present; the gate bounced forever for everyone,
group member or not. It now signs in at its own zone's `auth.zeus`, like
`pbx.zeus`, and the `Zeus` group reaches it.

#### Membership audit

Only three identities are people; the rest are Authentik plumbing
(`ak-outpost-*`, `ak-Capstone Dashboard-client_credentials`, `authentik-ldap`)
which never carries a browser session through a forward-auth gate.

| identity | Cerulean | cerulean-platform | Monarch | Zeus | Capstone |
|---|---|---|---|---|---|
| `dhunter` | yes | yes | yes | yes | yes |
| `akadmin` | yes | yes | yes | yes | yes |
| `justin`  | – | – | – | – | yes |

**`justin` is the one identity this tightening locks out.** Every gate used to
admit any authenticated user; `justin` is only in `Capstone`, so the Cerulean,
cerulean-platform, Monarch and Zeus gates are now closed to them. If that is not
intended, add `justin` to the relevant group(s) — nothing else in the audit
changes.

The new `Olympus` group was seeded from the `cerulean-platform` roster
(`akadmin`, `dhunter`), which is Olympus's own documented rule
(`GATEWAY_SSO_ALLOWED_GROUP` defaults to `cerulean-platform`) — worth knowing
that it therefore mirrors `cerulean-platform` exactly.

The NPM half is one host's `advanced_config`, rendered by Cerulean's
`forward_auth_snippet()`. It is a **manual** patch rather than a reconcile: the
host sits outside `NPM_BASE_DOMAIN`, so no `npm-proxy-hosts.py` manages it and
none will wipe the gate — but equally none will re-apply it. Re-paste from
`1-primary/cerulean/scripts/npm-proxy-hosts.py` if it is ever lost.

Because the admin UI is now gated at the edge, automation was moved off it:
monarch's `NPM_BASE_URL` is the LAN API `http://192.168.1.46:81`, not
`https://proxy.innotel.us` (pointing it at the public host would have 302'd the
API calls to the sign-in page and surfaced as **"proxy hosts drifted"**). The
break-glass path is unchanged and unauthenticated: NPM is still reachable
directly on the LAN at `http://192.168.1.46:81`, so a broken or unreachable
Authentik can never lock the proxy's own recovery UI out.

`omniroute.capstone.innotel.us` does not resolve at all — OmniRoute is declared
`optional: true` in the Capstone host map and its dashboard was never given an
NPM host, so there is no edge to gate; the provider is ready if it is published.
The snippet lives in `2-voice/capstone/scripts/npm-proxy-hosts.py`
(`FORWARD_AUTH_SNIPPET`) to paste from.

---

## 3. What this pass changed

- **Magnate** — `/admin/login` is Authentik-only. The `AUTHENTIK_*` variables are
  now **passed into the container** by `docker-compose.yml`; before this they
  existed only in `.env` (compose substitution) and never reached the process,
  so `oidcEnabled()` was false and the SSO button never rendered — the panel was
  effectively password-only despite the app supporting OIDC.
- **Cerulean** — `AUTH_LOCAL_ENABLED` now defaults to **false** in
  `server/src/config.ts`, so a checkout without the flag is SSO-only, and
  `POST /auth/login` refuses with 403 instead of accepting a password.
- **Distro** — the control plane's `/api/auth/signup` and `/api/auth/login` are
  gated, and the `distro` OIDC **application + provider were provisioned in
  Authentik** (they did not exist). Cerulean's `scripts/authentik-setup.py` got a
  fix along the way — Authentik's `/core/applications/` endpoint ignores a
  `slug=` filter, so provisioning a *new* application always failed with a 404
  on PUT.
- **Studio** — brought up against the `studio` Authentik app and Distro's
  control plane (`CONTROL_PLANE_INTERNAL_URL` / `CONTROL_INTERNAL_TOKEN`).
- **Audited as already compliant:** Zeus, Rizz Aura, Capstone dashboard, Homarr,
  Jellyfin (LDAP).
- **Forward-auth providers for all five zones** were created/repaired and attached
  to the embedded outpost (see §2). Creating `zeus`, `olympus` and `cerulean` was
  not enough on its own: they were first written as `forward_single` with an empty
  `cookie_domain`, which only authenticates the `auth.` host, not the apps beside
  it. They are now `forward_domain` with the zone as `cookie_domain`, matching the
  already-proven capstone/monarch pair.
- **The retired `atlas-chef` application and provider are gone from Authentik**
  (Chef retired as a builder).
- **Magnate was verified end-to-end, not just inspected.** A throwaway user on the
  admin allowlist and in the `Magnate` group was driven through the *real* flow over
  the public hostnames — `admin.magnate.innotel.us` → PKCE redirect →
  `auth.cerulean.innotel.us` (Authentik's own authentication flow) → authorization
  code → Magnate's callback exchanges it with the client secret + verifier →
  `admin_session` cookie set → `GET /admin` **200**. The temp user was deleted
  afterwards. So the panel accepts an Authentik-issued identity, and the password
  endpoint's 403 is a closed door rather than a broken one. That flow is now a
  **committed regression test**: `1-primary/magnate/scripts/verify-sso.py`
  (`npm run verify:sso`) creates a temp user, drives the real dance, asserts
  `POST /api/admin/login` → 403, and deletes the user even when a check fails
  (0 pass / 1 fail / 2 unreachable).
- **Magnate's Authentik admin API was dead, and is now alive.** `AUTHENTIK_BASE_URL`
  was never set (so `authentikConfigured()` was false and `lib/authentik.ts` threw
  "base URL is not configured"), and `AUTHENTIK_BOOTSTRAP_TOKEN` was stale —
  "Token invalid/expired". Both are fixed in `.env` (base URL +
  the shared instance token) and documented in `.env.example`; the Stripe webhook
  that provisions the Authentik account and `paid_users` had been silently skipping.
- **FreePBX and Vault are gated at the edge** — see §2, and both provisioners now
  render the snippet.
- **NPM Edge's own login is gone, and it is the last surface that had one.**
  Upstream Nginx Proxy Manager has an email+password user table and no OIDC, so
  the admin UI was the one door on the host that Authentik could gate but not
  own. The fork (`1-primary/npm`) now ships a first-party image
  (`docker/Dockerfile.sso`, tag `innotel/npm-edge`) whose `backend/lib/sso.js`
  signs the UI in with the identity the edge already authenticated, and refuses
  the password grant on every edge request. What proves "this came from the
  edge" is the connection itself: nginx runs in the same container, so the
  edge's requests are the only ones that arrive over loopback, and
  `socket.remoteAddress` cannot be forged by a header the way
  `X-authentik-email` can. That is why every admin proxy host
  (`proxy.innotel.us`, `admin.zeus`, `admin.monarch`) now forwards to the UI's
  `127.0.0.1` rather than the host's LAN IP — pointing one at the LAN IP does
  not open a hole, it just gets no SSO and says so. Cerulean's `NPM_EMAIL` stays
  as the one account allowed to use the password grant off-edge, because
  `npm-proxy-hosts.py` provisions hosts through the API with it; the LAN port
  stays published as the break-glass door. `backup-ui` is deliberately left
  LAN-only: it is management plane, not a user surface.

## 4. Open items

1. **Magnate's SecretOps doc rename is uncommitted.** `docs/stack.md`,
   `compose.infisical.yml`, `setup.sh` and `scripts/stack-lib.sh` carry the
   Infisical → Cerulean Vault rename but are unstaged, and `scripts/mesh.sh` is
   untracked. Commit or revert them so the tree stops drifting.
2. `cerulean-vault` and `cerulean-technitium` keep their own credentials by
   design — they are infrastructure, not user surfaces.
3. **Technitium's console** has no OIDC. If it is ever published through the edge,
   front it with the Cerulean-zone provider the way Vault now is.

---

## 5. Host reconcile — project names now match the composes

Every compose project on the host runs under the name its canonical file pins and
from the canonical directory; nothing is launched with `-p <legacy-name>` any
more.

```
capstone-voice-aiagent-platform  ->  capstone    (23 containers)
cerulean-dns-platform            ->  cerulean    (10)
monarch-media-platform           ->  monarch     (14)
zeus-pbx-platform                ->  zeus        (3)
```

Named volumes were copied to the canonical prefix before the cutover
(`capstone-*` 13 volumes, `cerulean_infisical-*` 2), the containers were recreated
from the canonical compose with the same profiles, and the containers' anonymous
volumes (image `VOLUME`s: searxng's `/etc/searxng` + cache, the n8n sandbox's
dind `/var/lib/docker`, the ClickHouse keeper's `/var/lib/clickhouse`) were copied
into their replacements and the containers restarted. Zeus needed no volume copy —
`pbx-*` and `zeus-portal-data` are declared with explicit `name:`, so they are
project-independent — and Monarch none either, because its canonical
`monarch_clipbucket_*` volumes already existed.

Two things to know:- **The legacy volumes are gone.** All 18 pre-migration volumes
  (`capstone-voice-aiagent-platform_*`, `cerulean-dns-platform_*`,
  `monarch-media-platform_clipbucket_*`, `zeus-pbx-platform_omniroute-data`) were
  deleted after every canonical counterpart was confirmed present with
  `du`-comparable or larger contents and no container referenced one. The Monarch
  `*_clipbucket_*` pair was empty (stubs) and Zeus's dormant `omniroute-data` held
  only a 0-byte `storage.sqlite` — copied to `zeus_omniroute-data` first. Rollback
  is now the copied volumes alone.
- **Do not `up -d` Monarch without a service list.** Its compose also defines
  `requestrr`, `flaresolverr`, `clipbucket`, `monarch-recs`, `monarch-health`,
  `watchtower` and the `legacy`/`npm` profiles, none of which run on this host;
  a bare `up -d` would start seven extra containers.

Also fixed on the way: the Capstone project's recorded compose file used to live at
a path that no longer existed (18 containers were unmanageable) and Docker had
re-created that path **as empty directories** where three config *files* are
mounted — `n8n-grader-workflow.json` was mounted as a directory, so the import job
could not read it. The five legacy root paths remain as **symlinks** to the
canonical dirs (`cerulean-dns-platform`, `zeus-pbx-platform`,
`capstone-voice-aiagent-platform`, `monarch-media-platform`,
`rizzaura-platform`) — Rizz Aura still records its working dir through one, which
is harmless, and the rest are now unused aliases.

The one host that is still on a non-canonical image mix on purpose: Capstone's
`dograh-api` runs the published `ghcr.io/innotelinc/dograh-api:latest` while
`dograh-ui` runs the locally built `dograh-local/dograh-ui:capstone`, because
`docker-compose.dograh-build.yml` is only applied to the UI. Recreating the API
from that override swaps it to the local build — pass `--no-build` if you ever do,
or the override triggers a full source build (15–25 min, and it OOMs without
`--build-arg NODE_BUILD_HEAP_MB`).
