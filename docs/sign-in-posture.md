# Sign-in posture — Authentik is the only identity

**Rule.** Identity lives in Cerulean's **Authentik** (IdentityOps). A platform
may *consume* identity; it may not keep a second user store or a second password
path. Every deployed surface is either **Authentik-only** or explicitly listed
below as an exception with the reason.

This document is the inventory of every login on the host, what enforces it, and
the break-glass path for each. It exists because "we use SSO" is not a fact you
can audit — a password form left in the template is a second identity store even
when nobody uses it.

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
| **Infisical** | local admin | Legacy SecretOps, retiring in favour of **Vault**. Not published through the edge (bound to `:8383` on the host); its replacement is the gate that matters — `secrets.cerulean.innotel.us` (Vault) is fronted by the **Cerulean-zone forward-auth** provider |
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
`pbx.capstone`, `pbx.zeus`, `secrets.cerulean` — while leaving the identity
provider's own hosts alone.

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

Nothing reaches those two hosts programmatically — both are interactive UIs, so
gating them cannot break an integration. The snippet is now rendered by the
provisioners themselves: Zeus's and Cerulean's `scripts/npm-proxy-hosts.py` carry
the same `FORWARD_AUTH_SNIPPET` as Capstone's, so re-running any of them
re-applies the gate instead of wiping it. `forward_auth: False` opts a host out —
`auth`/`dns`/`certs`/`admin` are the Cerulean app itself, and gating `auth` would
lock the zone out of its own IdP.

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
- **Dograh verified rather than assumed** — issued a real `authorize` request with
  its client id and confirmed its client secret matches the provider.

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
