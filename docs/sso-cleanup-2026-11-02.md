# SSO cleanup and the two production login faults, 2026-11-02

Two reports came in together: the OnTrak bolt builder answered every turn with
*"The AI provider request failed. Check your provider settings and try again."*, and
`mail.innotel.us` / `oasis.innotel.us` showed *"Authentication Failed — Failed to
complete authentication"* after a correct sign-in. Both were chased to a cause, and the
Authentik estate was audited and pruned on the way through.

## Authentik: what the audit found

Twenty-two applications and fifty OAuth2 providers were live. The provider list had
accumulated duplicates, typo'd redirect URIs and callbacks on hosts that no longer exist.
Every change below was made through the API (`https://auth.cerulean.innotel.us`), and the
pre-change inventory is on `i4` at `/root/authentik-backup-20261102.json`.

**Deleted — no application referenced them, and each names hardware that is gone or a
provider another entry supersedes:**

| pk | Name | Why |
|---|---|---|
| 3 | incus | Duplicate of `Incus` (pk 46). Its URIs carry `/oicd/` typos, the retired `i3` (`.53`) and `.25`. |
| 23 | Zeus | Duplicate of `Zeus PBX` (pk 29): the same four redirect URIs. |
| 22 | PLUTUS SSO | Callback is `192.168.1.46`, which no longer answers; no application. |
| 4 | mail | Superseded by `Stalwart WebUI` (pk 50). |
| proxy 33 | Olympus NPM Forward Auth | `auth.olympus.innotel.us` does not resolve; no application. |

**Repaired — the URIs are now the ones that actually serve the app:**

- `Incus` (pk 46): `https://192.192.1.54:8443` → `https://192.168.1.54:8443`; dropped the
  retired `i3` host. 4 → 3 URIs.
- `Studio` (pk 25): dropped `studio.olympus.innotel.us` (NXDOMAIN). 5 → 4.
- `Distro` (pk 31): dropped the plain-HTTP container callback
  (`http://192.168.1.61:20140/...`). 4 → 3.
- `AthenIQ LMS` (pk 20): kept `atheniq.courses` and `studio.atheniq.courses`; dropped the
  superseded `studio.innotel.us` and `lms.innotel.us` names. 6 → 3.

After the change: 45 OAuth2 providers, 6 proxy providers, 22 applications, and every
provider still referenced by an application is untouched.

## mail.innotel.us / oasis.innotel.us: the mail server's directory, not the webmail

The webmail side is healthy, and this was measured rather than assumed. A full sign-in was
driven against production — `/api/auth/sso/start`, the Authentik flow, the callback code,
then `/api/auth/sso/complete` — and it returned **200 with a token** for
`admin@innotel.us`. Authentik's `stalwart-webui` provider (pk 50, public client) lists both
`https://mail.innotel.us/en/auth/callback` and
`https://oasis.innotel.us/en/auth/callback`, accepts either at the authorize endpoint, and
its token endpoint allows the webmail's origin over CORS. `mail.innotel.us` no longer sets
an `OAUTH_CLIENT_SECRET`, so no public-client/secret mismatch exists.

What is broken is **Stalwart's directory**, and its own log says so
(`/var/log/stalwart/stalwart.2026-11-02`):

```
ERROR Store not configured (store.not-configured) listenerId = "imaps", localPort = 993
  details = "Directory is unavailable because it failed to initialize"
  reason  = "Network error: Discovery fetch failed: error sending request for url
             (https://auth.cerulean.innotel.us/application/o/stalwart-webui/.well-known/openid-configuration)"

ERROR Authentication error (auth.error) listenerId = "imaps", localPort = 993
  reason = "Unsupported credentials type for OIDC backend"
```

Two consequences follow, and they are the user-visible failure:

1. The directory is configured against the **OIDC backend**, so Stalwart has no password
   store to check a password against. A correct password can only ever answer
   `AUTHENTICATIONFAILED`. App passwords still work (the log shows `dhunter` and
   `alertmanager` authenticating that way), which is why the alert path and some clients
   kept working while interactive sign-in did not.
2. Its discovery fetch fails from inside the container even though the same URL answers
   `200` from the host, so the directory cannot even initialise for app-password lookups.

A restart was applied at 02:23 EST and changed the symptom from "Directory is unavailable"
to "Unsupported credentials type for OIDC backend" — the directory now loads, and the
remaining refusal is the backend's, not a network one. **The fix is in Stalwart's directory
configuration, not in this repo**: authentication must be backed by the internal directory
(accounts named by full address, which is Stalwart's own requirement and what the estate
already uses), with Authentik as the SSO front door, not as the password verifier.

The configuration was read through Stalwart's own JMAP management API
(`x:Directory/get`, `x:Account/get`, `x:Domain/get`), which is the record of what the server
actually holds rather than what a file says:

- The **only** directory is `{"description":"Authentik","@type":"Oidc","issuerUrl":
  "https://auth.cerulean.innotel.us/application/o/stalwart-webui/","requireAudience":
  "stalwart-webui","claimUsername":"sub"}`. There is no internal directory object to fall
  back to, and **no domain carries a `directoryId`** — the 26 domains (`innotel.us`,
  `loomix.art`, `ontrak.technology`, `atheniq.*`, `telno.*`, …) are all unbounded, so
  nothing selects a password verifier per domain.
- Accounts do exist locally — `x:Account/get` shows them by name (`postmaster`, …) with
  `AppPassword` credentials and their aliases. So the account store is intact; what is
  missing is an authentication path that consults it.

Removing the OIDC directory would restore password checks but break SSO, because a JMAP
bearer token from `stalwart-webui` is validated through that same object. Adding the
internal one was attempted over the same API and **refused**, which is the useful result:

```
x:Directory/set create {"@type":"Internal","description":"Local password store"}
  -> notCreated: invalidPatch "Missing or invalid '@type' property in object"
```

So the internal directory has to be created by the server's own admin UI at
`https://admin.mail.innotel.us`, which knows the type names and the field set; the API does
not accept a hand-written object. The directory itself was left exactly as found (one
`Oidc` object, `jjb7ynuiacqa`).

State after the restarts, for whoever picks this up: the directory now loads, and an IMAP
password login as `admin@innotel.us` or `dhunter@innotel.us` answers
`[AUTHENTICATIONFAILED] Authentication error` — a credential verdict, not the earlier
"directory unavailable" outage. **Interactive mail sign-in is still broken**, and the
remaining step is creating and binding the internal directory in the admin UI, then
re-testing IMAP.

The identity model the user asked for — Stalwart accounts keyed by the email address and
mirrored into Authentik under the same address — is the right one, and the estate is half
way there: Authentik already holds `admin` and `dhunter` as internal users in the `Mail`
group. What is missing is the provisioning direction (an account created in Stalwart should
create/attach the Authentik user with that address). No such sync exists today; the
candidates are a Stalwart account-creation hook posting to Authentik's
`/api/v3/core/users/`, or creating mail accounts from Authentik instead.

## bolt.ontrak.technology: the provider base URL and key

`bolt.ontrak.technology` is NPM host 302 → `192.168.1.21:3500`, the `ontrak` container's
`bolt-sso` (oauth2-proxy, provider `OnTrak` pk 47), in front of
`innotel/ontrak-bolt:main` on `:5173`. The app is up and its websocket/SSE path works; what
fails is the model call, and the container log shows
`api.chat Streaming provider error` / `api.errors Streaming provider error`.

Its provider configuration points at the wrong gateway:

| Setting | Value | State |
|---|---|---|
| `OPENAI_LIKE_API_BASE_URL` | `https://gateway.telno.services/v1` | TLS handshake fails (`tlsv1 alert internal error`) from the container |
| `OPENAI_LIKE_API_KEY` | 46 characters | **401 `AUTH_002 Invalid API key`** against `https://gateway.innotel.us/v1` |
| `OPENAI_LIKE_API_MODELS` | `auto/coding`, `auto/best-chat`, `auto/cheap`, … | ids the estate gateway serves |

So two things were wrong at once: the base URL had moved to the estate gateway
(`https://gateway.innotel.us/v1`, the one `g0dm0d3` uses), and the key in the container was
not valid at that gateway. Both are now applied:

- `docker-compose.all.yml` carries `OPENAI_LIKE_API_BASE_URL: https://gateway.innotel.us/v1`
  (was the retired `https://gateway.telno.services/v1`), pre-change copy beside it as
  `docker-compose.all.yml.bak-20261102-bolt`.
- The key lives where the deployment already keeps it — `ontrak-bolt/.env` (the
  `env_file` the service reads, pre-change copy `.env.bak-20261102`) — because the compose
  file is tracked and a key does not belong in it. The installed key answers **200** on
  `/v1/models` and `/v1/chat/completions` (model `auto/coding`, answered by `gpt-6.1-sol`)
  when called from `i4`, and the container was recreated to pick it up.

A key that is valid at the gateway is the whole fix; the app's route, session and SSE path
were never at fault.

## Host and container patching, 2026-11-02

Every running Incus container on `i1`, `i2` and `i4`, plus the three hosts themselves, was
updated with its package manager (`apt-get upgrade -y`, `apk upgrade`, or `dnf -y upgrade`
where that is what the image uses):

- 24 containers + 3 hosts updated.
- **0** hosts or containers report `reboot-required`.
- No package failures and nothing held back in the run log (`/tmp/patch-all.log` on `i4`).

Not covered by that sweep, and still to do: the Docker images and the applications inside
them (the container's own OS is patched; the images are only as new as the next pull), and
the `ord` VPS, which is off this LAN.

## Still open

1. **Stalwart directory backend** — create the internal directory and bind the domains
   that should authenticate locally, leaving Authentik as the SSO door (above). Interactive
   mail sign-in is broken until this is done. It is a config change in the admin UI, not a
   code change: the management API rejects a hand-written `Internal` object, so this needs
   `https://admin.mail.innotel.us`.
2. **Mail account provisioning** — one direction of truth: Stalwart account ⇄ Authentik user
   with the same email address.
3. **bolt provider verification in the browser** — the base URL and key are in place and
green from the host; confirm a chat turn streams through `bolt.ontrak.technology` now that
`bolt-app` has been recreated.
4. **`mail.innotel.us` is a 301 to `oasis.innotel.us`.** NPM host 95 already forwards
   `mail.innotel.us` → `192.168.1.15:3000`, so the redirect is emitted by the app or a
   redirection host above it. It should serve the webmail directly: the address is a
   documented webmail name and is registered as an SSO callback.
5. **Forward-auth to native OIDC.** Seven proxy providers still gate apps through
   oauth2-proxy. n8n, Grist, Grafana and Technitium all speak OIDC natively; the media apps
   (Radarr/Sonarr/Lidarr/Bazarr/Prowlarr/SABnzbd/qBittorrent) do not and should stay behind
   the gateway. Each move is an app-side configuration change plus a provider, so it is a
   per-app job, not a bulk edit.
