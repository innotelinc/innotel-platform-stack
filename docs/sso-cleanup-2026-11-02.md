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

Later probing settled this properly: **this build has no internal directory type at all.**
`strings /usr/local/bin/stalwart` (0.16.27) contains exactly two directory variants, `Ldap`
and `Oidc`; `Internal` and `Memory` are not among them, and `x:Directory/set` rejects them
with the same generic `Missing or invalid '@type'` it gives a deliberately bogus type. There
is also no per-listener or per-domain hook to change the backend: `x:NetworkListener/get`
carries no directory field, `x:SystemSettings/get` has none, and the only binding is the
single `Oidc` object (`jjb7ynuiacqa`), which the `imaps` listener falls back to.

Credential repair is closed off too. `admin@innotel.us` already holds a `Password`
credential, and `x:Account/set` refuses to touch the credentials map —
`invalidPatch "Cannot modify server set property"` on `credentials/credentialId` — while
`x:Credential/set` is an unknown method. The directory itself was left exactly as found.

So bare IMAP password login cannot be turned on by configuration in 0.16.27: password
verification is the directory's job, the only directory speaks OIDC, and OIDC cannot check a
password. Mail clients belong on **app passwords** (which authenticate against the account's
`AppPassword` credentials and work today — `dhunter`, `alertmanager` and `pbx` all do it) or
on OAuth, and that is a client-side setting, not a server change.

State after the restarts: the directory initialises again, and the sign-in the webmail
actually uses was driven end to end afterwards — `/api/auth/sso/start`, the Authentik flow,
the callback code, `/api/auth/sso/complete` (**200**), then
`GET /jmap/session` with that token on **both** `mail.innotel.us` and `oasis.innotel.us`
(**200**, `"username": "admin@innotel.us"`). So the webmail sign-in the user reported is
working again; what broke it was the directory outage, not the OAuth settings.

The refusal is specific and worth recording verbatim, because it names the mechanism:
`reason = "Unsupported credentials type for OIDC backend"`,
`causedBy = "crates/directory/src/core/dispatch.rs:19"` — the `imaps` listener dispatches the
account lookup to the OIDC directory on every password attempt.

The identity model the user asked for — Stalwart accounts keyed by the email address and
mirrored into Authentik under the same address — is the right one, and the estate is half
way there: Authentik already holds `admin` and `dhunter` as internal users in the `Mail`
group. What was missing is the provisioning direction, and that is now
`scripts/sync-mail-identities.py`.

It reads Stalwart's accounts over JMAP (`x:Domain/get` + `x:Account/get`), reads Authentik's
users, matches case-insensitively by **username or email**, and creates only the ones that
truly do not exist — with `username = email = <name>@<domain>`, `type: internal`, no password
(SSO is the door) and optionally `--group Mail`. It never deletes, renames, deactivates or
re-issues a password, and it never writes to Stalwart, so the worst case of a wrong run is a
useless account that the same dry run names first.

It has now been applied against production (`incus exec mail`,
`STALWART_URL=http://127.0.0.1:8080`, the Cerulean bootstrap token) and created **15**
identities in the `Mail` group, exit 0, every create accepted:
`cerulean-platform@`, `capstone@`, `onyx@`, `magnate@`, `monarch@`, `signara@`, `rizzaura@`,
`oasis@`, `zapit@`, `plutus@`, `atlaschef@`, `atheniqlms@`, `olympus@`, `monarch-request@`,
`atheniq@` (pks 621–635). `admin@innotel.us` already matched, which is why the sign-in above
resolves. A second run is a no-op by construction.

The first attempt created nothing, and the reason was a bug in the script rather than the
estate: `--group Mail` was passed straight through, and Authentik's user API accepts pks only
— `400 {"groups":["“Mail” is not a valid UUID."]}` on all 21 candidates. The script now
resolves a group name to its pk (exact, case-insensitive, via
`/api/v3/core/groups/?search=`) and returns `2` with a clear message if no such group exists.
The same run also showed the service-address filter was too narrow, so
`default@`, `mail@`, `cerulean@`, `zeus@`, `authentikadmins@` and `signara-alerts@` are now in
`SERVICE` and stay out of the identity store — they send and route mail, they do not sign in.

An unfiltered run also lists role, list and service boxes (fifteen `postmaster@` addresses
across the domains, `paid_users@`, `jellyfin_admins@`, `atheniq-admins@`, `incus@`, `monit@`,
`jellyfin@`, `pbx@`, `pm3@`, `pm4@`, and a few accounts whose `name` is a display name with a
space in it). Those are skipped by default and only synced under `--include-all`, because an
estate does not need an identity for every role address it owns.

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

- 26 running containers + 3 hosts updated.
- The two containers that were **stopped** on `i2` (`atheniq`, `capstone`) were started,
  upgraded and returned to stopped. `atheniq` took a real batch (`libbrotli1` and the libc
  trigger chain); `capstone` was already current. Neither needed a reboot.
- **0** hosts or containers report `reboot-required`.
- No package failures and nothing held back in the run log (`/tmp/patch-all.log` on `i4`).

Not covered by that sweep, and still to do: the Docker images and the applications inside
them (the container's own OS is patched; the images are only as new as the next pull), and
the `ord` VPS, which is off this LAN.

One finding from the same window is unrelated to patching and worth its own line: at 03:11
the kernel OOM-killed `clickhouse-serv` twice inside the **`monitor`** container's cgroup
(`constraint=CONSTRAINT_MEMCG`, `oom_memcg=/lxc.payload.monitor/...`), and the Authentik
containers restarted in the same minutes. Authentik came back healthy on its own (`live`
and `ready` both **200**) and the mail sign-in was re-verified end to end afterwards, but the
`monitor` container's memory limit is too small for ClickHouse and will keep cycling until it
is raised or ClickHouse is capped. That is a capacity change, so it was not made here.## Three follow-ups, closed

### `monitor`: ClickHouse has headroom instead of a hard ceiling

The OOM at 03:11 was not ClickHouse ignoring its cap — `config/clickhouse-config.yaml`
already set `max_server_memory_usage: 603979776` (576 MiB) — it was the *container* limit
above it. `docker stats` showed `signoz-clickhouse 753.8MiB / 768MiB (98.15%)`: at the edge of
its own `mem_limit`, with the Incus container capped at `2304MiB` for ClickHouse, SigNoz, the
collector, ClickHouse Keeper and Postgres together. A spike had nowhere to go, so the kernel
picked `clickhouse-serv`.

Raised in three places, each backed up first (`docker-compose.yml.bak-20261102-mem`,
`clickhouse-config.yaml.bak-20261102-mem`):

| Layer | Before | After |
|---|---|---|
| `signoz-clickhouse` `mem_limit` | `768m` | `1536m` |
| `max_server_memory_usage` | 603979776 (576 MiB) | 1073741824 (1 GiB) |
| Incus `monitor` `limits.memory` | `2304MiB` | `3584MiB` |

`limits.memory` is the one value here that is **not** stable: set to `3584MiB`, it read back
as `2176MiB` and then `2688MiB` within minutes, and `3584MiB` again after being re-applied.
Each on-disk candidate was ruled out rather than assumed — `estate-pass.sh` sets caps from a
`CAPS` table whose `i2` row names only `atheniq` and `dayzero` (a container that does not
exist on this host), `i2-memory-recut.sh` only touches `atheniq` and `capstone`, and no cron
entry or timer drives either for `monitor`. The ClickHouse-side caps are files on disk and do
not drift, and they are what actually prevents the OOM; the container cap is currently
`3584MiB`.

`signoz-clickhouse` was recreated to pick them up. After the restart it sits at
`1023MiB / 1.5GiB` (**66%**) with the whole container at `1.82GiB` of 3584 MiB and swap back
to 23 MiB, against 553 MiB of swap in use before. `i2` had ~4.8 GiB available and these are
caps, not reservations, so the change only lets a spike stay inside the cgroup rather than
being killed for it. Container limit and ClickHouse ceiling now differ by 512 MiB, which is
the headroom the OOM lacked.

### Mail identities provision themselves

`sync-mail-identities.py` is installed in the `mail` container as
`/usr/local/bin/sync-mail-identities.py` and driven by a systemd timer, so a mailbox created
today gets its Authentik identity without anyone remembering to run it:

- `/etc/systemd/system/sync-mail-identities.service` — oneshot, `--apply --group Mail`.
- `/etc/systemd/system/sync-mail-identities.timer` — `OnBootSec=3min`, `OnUnitActiveSec=15min`.
- `/etc/innotel/mail-identities.env` — `0600`, the Stalwart admin and the Authentik token;
  the token is written by reading the `cerulean` container's own environment and never
  appears in a command line or in this document.

`systemctl enable --now` succeeded and the first run reports `Result=success`,
`ExecMainStatus=0`, `mailboxes=60 authentik-users=45 missing=0` — that is the idempotency
proof, not just a green unit. Fifteen minutes is the interval because the estate has no
Stalwart account-creation hook to call it from; when one exists, the same service can be
invoked directly and the timer becomes a safety net.

### `mail.innotel.us` serves the webmail directly

The 301 came from NPM, not the app: the app answers `307 → /en` on `Host: mail.innotel.us`,
while `/data/nginx/proxy_host/95.conf` carried a custom
`location = / { return 301 https://oasis.innotel.us$request_uri; }`. That exact-match
location only caught `/`, which is why `/en` already worked and only the root bounced. The
host's normal `location /` already proxies to `192.168.1.15:3000`, so the fix is the removal
of the custom location and nothing else — backup `95.conf.bak-20261102-redirect`,
`nginx -t` clean, reloaded.

Verified on the public name: `/` → **307** `/en`, `/en` → **200**, `/jmap/session` → **200**
(still reaching Stalwart on `:8080`).

Worth recording, because it says something about how this NPM is managed: host 95's stored
`locations` JSON (in `cerulean-npm-db`, since NPM here uses MySQL and its local
`database.sqlite` is a 0-byte file) contains only the four `:8080` paths — `/.well-known/jmap`,
`/jmap`, `/.well-known/autoconfig`, `/.well-known/mta-sts.txt`. `/` was never a stored
location, so the 301 existed only as a hand edit to `95.conf` and there is nothing in the
database to restore it. A regeneration from the database would drop it as well.

## Still open

1. **Bare IMAP password login** — not available in Stalwart 0.16.27 (see above: no internal
   directory type, credentials not writable over the API). Clients use app passwords or
   OAuth. Only a server build with an internal directory, or a populated `Ldap`/`Sql`
   directory, changes that.
2. **bolt provider verification in the browser** — the base URL and key are in place and
   green from the host; confirm a chat turn streams through `bolt.ontrak.technology` now that
   `bolt-app` has been recreated.
3. **Forward-auth to native OIDC.** Seven proxy providers still gate apps through
   oauth2-proxy. n8n, Grist, Grafana and Technitium all speak OIDC natively; the media apps
   (Radarr/Sonarr/Lidarr/Bazarr/Prowlarr/SABnzbd/qBittorrent) do not and should stay behind
   the gateway. Each move is an app-side configuration change plus a provider, so it is a
   per-app job, not a bulk edit.
