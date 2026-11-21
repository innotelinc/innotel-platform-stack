# Coolify apps on the NPM edge — no bundled proxy, one `*.innotel.site` zone

Coolify ships a proxy (Traefik, and a Caddy template) that it wants to start on the
deploy host and drive from the app's own `fqdn`. **This estate does not run it.** The
edge is already owned by [NPM Edge](stack.md#edge-platform), and two proxies in front of
one app means two places that think they terminate TLS, two certificates that can
disagree, and a domain that only resolves if the *second* one was told about it.

So the split is:

| Layer | Owner | What it does for a Coolify app |
|---|---|---|
| **DNS** | Cerulean → Technitium | `*.innotel.site` is a wildcard record in zone `innotel.site`, A → `73.68.203.71`. A new app needs **no DNS work at all**. |
| **TLS** | Cerulean (ACME DNS-01) → NPM | one wildcard certificate, NPM id **186** `wildcard-innotel-site` for `*.innotel.site`, attached to every proxy host in the zone. |
| **Routing** | NPM Edge (`cerulean-npm`, `:80`/`:443` on `192.168.1.71`) | one proxy host per app name → the app's published host port. |
| **Deploy** | Coolify (`coolify` instance on `i2`, `192.168.1.85`) | builds and runs the container, publishes the port, and knows the app only as a name. |

## Deploy-host prerequisites

`i2` runs Docker from the Ubuntu package (`29.1.3`). The engine has BuildKit, but Coolify
probes for the **`buildx` CLI plugin** and refuses a Dockerfile build with *"does not
support BuildKit"* when it is missing. Install it from the distribution rather than a
hand-downloaded binary, so `apt` keeps it in step with the engine:

```bash
apt-get install -y docker-buildx-plugin      # older images name it: docker-buildx
docker buildx version
docker buildx ls                            # driver "docker", BUILDKIT column filled
```

A deploy that used it says so in its log — `Docker <ver> with BuildKit and Buildx
detected on deployment server (i2-local)`. If that line is absent, the build fell back to
the legacy builder.

Builds run on the **`i2-local` server record** (`root@192.168.1.52`), which Coolify reaches
over SSH — so the plugin has to exist on `i2` itself, and it does. `docker buildx` inside
the `coolify` container is absent by design (`docker: unknown command: docker buildx`);
that only matters for a *localhost* destination, which would build in the panel container
instead. All five apps here are `dockerfile` apps on `i2-local`, so one host install covers
every one of them.

## Panel API authentication

Coolify's own nginx does not forward the `Authorization` header to PHP (`fastcgi_params`
defines no `HTTP_AUTHORIZATION`), so **every** `/api/v1` call answers 401 even with a
token that has the right abilities. The missing piece is one line after the include in
`/etc/nginx/site-opts.d/http.conf`:

```nginx
fastcgi_param HTTP_AUTHORIZATION $http_authorization;
```

That path sits in the container's writable layer, so the `coolify` instance carries
`coolify-nginx-authfix.service`, which re-applies the line and reloads nginx on every
panel container start (it watches `docker events` for `start`):

```bash
incus exec coolify -- systemctl is-active coolify-nginx-authfix
incus exec coolify -- docker exec coolify grep -c HTTP_AUTHORIZATION /etc/nginx/site-opts.d/http.conf  # 1
```

That unit lives in an instance with **no persistent disk** (`incus config show coolify` reports
`devices: {}`), so a rebuild of the instance erases it. The host therefore carries
`coolify-authfix-provision.service` plus a ten-minute timer: it keeps a copy of the three files
in `/var/lib/coolify-authfix`, re-pushes them, and re-enables the unit, so a rebuilt instance
heals itself within minutes of boot without anyone re-running the install.

Coolify's proxy must stay **absent** — not started, not configured. Verified 2026-11-19:
no `coolify-proxy`, `traefik` or `caddy` container exists in the `coolify` instance's
dockerd or in `i2`'s host dockerd. `instance_settings.fqdn` is
`https://deploy.innotel.us`, and the panel itself is served the same way as the apps
(NPM host 342 → `192.168.1.85:8000`), which is the proof the pattern scales to the
panel as well as to what it deploys.

## The apps, as configured

| App | Public name | Coolify `ports_mappings` | NPM upstream |
|---|---|---|---|
| `servicedesk` | `https://servicedesk.innotel.site` | `8081:8000` | NPM 376 → `192.168.1.52:8081` |
| `ontrak-bolt` | `https://bolt.innotel.site` | `3500:5173` | own cert (185) |
| `clearpath` | `https://clearpath.innotel.site` | `8911:3000` | cert 163 (+ preview wildcard 184) |
| `opencompany` | `https://opencompany.innotel.site` | `3001:3000` | cert 156 |
| `artex` | `https://artex.innotel.site` | `8790:8787` | NPM 377 → `192.168.1.52:8790` |

**`opencompany` runs without a door** (2026-11-20). Coolify reports the app
`running:healthy`, but both of the names it was registered under are NPM rows with
`enabled=0` — `opencompany.innotel.site` (row 332, http-only) and `openco.innotel.tech` — so
`check-sso-redirects` called its four sign-in registrations dead and they are removed
(`https://<name>/api/auth/sso/callback` for auth, `/signin` for logout). Re-publishing it
means giving the app an `https` `fqdn`, enabling one door, and adding both URIs back; the
check reports the mismatch until all three are done, which is why removing them is safe.
Every row has a `*.innotel.site` name. **No application may be left on a generated
`sslip.io` name**: that name is Coolify's fallback when `fqdn` is empty, it resolves to
whatever address the deploy host had at the time, it is not covered by the zone
certificate, and it is not a name anyone can be given. If an app shows one, it was
created without a domain — set the domain, do not add DNS for the sslip name.

Coolify publishes an app's port on **its own docker host**, which is the `coolify`
instance at `192.168.1.85`, while `docker ps` here shows the bind on `i2`'s host
(`192.168.1.52`) because the app is deployed by the `i2-local` target. Check which
address actually holds the listener before writing the NPM upstream:

```bash
ssh root@192.168.1.52 'docker ps --format "{{.Names}}|{{.Ports}}" | grep <uuid-prefix>'
ssh root@192.168.1.52 'ss -ltn | grep :<hostport>'
```

Env values are encrypted at rest: `environment_variables.value` in Coolify's database reads
back as a Laravel blob (`eyJpdiI6…`), not the setting. Read them through
`GET /api/v1/applications/<uuid>/envs` (the API decrypts), never by `psql`.

ARTEX keeps its admin password in `/root/artex.env` on `i2` (mode 0600) and its database is
its own container on the host, so a rebuild of the `coolify` instance does not touch either.
`/root/artex-reset-admin.sh` re-establishes access from that file: clear `auth.%`, restart the
app, read the fresh setup token from its log, and re-init. It is the recovery path for a lost
or drifted credential, and it verifies itself with `init=` and `login=` codes.

## Keeping the evidence Coolify throws away

A redeploy replaces the broken container with a working one, so the only thing left to
diagnose is whatever was written down first. Two host-side timers on `i2` do that:

- `ontrak-bolt-watch.timer` (every minute) logs each state transition of the app's container
  and, the first time it stops being `running`/`healthy`, saves the exit code, OOM flag,
  error and last 300 log lines under `/var/log/ontrak-bolt-watch/`. Polls are deduplicated,
  so one long outage yields one capture rather than a line a minute.
- `artex-db-backup.timer` (03:15 daily, `Persistent=true`) writes a custom-format
  `pg_dump` to `/var/backups/artex/`, keeps 14 days, and fails loudly if the dump does not
  parse. Restores are verified against a scratch `postgres:18-alpine` before trusting a
  backup directory.
- `artex-planner-watch.timer` (every 2 minutes) looks for a task still `created` or
  `running` that has emitted no tool activity for 12 minutes — the shape of the stock
  exposure scan that sat for 600s without ever starting a scanner. It writes the task row,
  last activity, container state and 120 log lines to `/var/log/artex-planner-watch/`, then
  pauses and resumes that one task through `POST /api/tasks/control/batch`, which cancels its
  in-flight context and starts a fresh planning round while every other task keeps running.
  Only if the task records no new activity within 90s does it fall back to restarting the app
  container. A 30-minute per-task cooldown keeps it from looping; `DRY_RUN=1` reports without
  acting. On a task stalled for 191s it recovered in ten seconds with `pause=ok resume=ok`,
  without touching the other tasks.

Containers are named by Coolify UUID, not by app name, so any tooling must match on the
UUID prefix (`qrtcbyvzl4yojtb8hflil9t5-…`) — `docker ps | grep <app-name>` finds nothing
and reads as "no container" when one is running.

### Stock templates work, and a direct disclosure now reaches findings

On an owned throwaway nginx exposing `/.git/config`, `/.env` and `/backup/dump.sql`, the
stock library detects the exposures: `http/exposures/configs/git-config.yaml` returns one
match, and the whole `http/exposures/configs/` directory returns three (`git-config`, plus
`codeigniter-env` and `laravel-env` on `/.env`). `http/exposures/logs/` returns nothing,
which is correct for a target with no log files.

What ARTEX does with a match is a separate question. By default it filed them as facts: the
doctrine refuses findings built from version/CVE matching and from low-yield
misconfigurations, so a confirmed `/.git/config` hit became an exploration conclusion
(`record_fact`) and the findings table stayed empty. `artex-findings-rule.sh` adds one narrow
exception to the `planner` and `worker` prompts — a match that proves the target served a
sensitive file, with the returned content in hand, is a demonstrated disclosure and is
registered with `report_finding`. Agent prompts are Postgres rows, so the rule survives image
rebuilds and is covered by the database backup; `--check` reports whether it is present, and
nothing else in the prompt is modified. `artex-planner-watch.service` runs the script as
`ExecStartPost`, so a prompt reset is re-asserted within two minutes.

Verified after applying it: the same single-template run that used to file a fact closed with
finding `sensitive_file_disclosure` ("Unauthenticated Git configuration disclosure") at
medium, evidence `executed-live`.

Caveat from the same session: one run of that scan was refused outright — "the requested
template set automates retrieval of potentially sensitive configuration" — and filed nothing.
The model's willingness is not deterministic, so a zero-finding task is not evidence that the
target is clean. Read the activity trace for a refusal before treating a scan as negative.

## Onboarding an app (the whole procedure)

1. **Name it in the zone.** In Coolify, set the app's domain to
   `https://<name>.innotel.site` (the column is `applications.fqdn`).
2. **Publish a host port.** Set `ports_mappings` to `<free host port>:<container port>`.
   The port must be free on the deploy host — `8081` for `servicedesk`, because the
   container listens on `8000`.
3. **Redeploy.** A DB edit alone changes nothing: the container only gains the
   publishing flag when Coolify regenerates its compose file. The API calls in this step
   need the panel's `Authorization` forwarding fix (see *Panel API authentication*).

   ```bash
   # POST /api/v1/deploy?uuid=<app uuid>; the token needs the "deploy" ability.
   curl -s -X POST -H "Authorization: Bearer <token>" \
     http://127.0.0.1:8080/api/v1/deploy?uuid=<app uuid>
   ```

4. **Add the NPM proxy host**, through Cerulean's own NPM client so the certificate is
   the zone wildcard rather than a per-host reissue:

   ```bash
   incus exec proxy -- docker exec cerulean node -e "
   const m=require('/app/server/dist/services/npm.js');
   m.npm.createProxyHost({domainNames:['<name>.innotel.site'],forwardScheme:'http',
     forwardHost:'192.168.1.52',forwardPort:<host port>,certificateId:186,
     sslForced:true,http2Support:true,websocketSupport:true})
     .then(h=>console.log(h.id)).catch(e=>console.log('ERR',e.message));"
   ```

5. **Prove the loaded config, not the row.** NPM only writes nginx config when its API
   is asked to, and the host-map page is explicit that a file on disk is not a vhost:

   ```bash
   incus exec proxy -- docker exec cerulean-npm nginx -T | grep -n 'server_name <name>.innotel.site'
   curl -sI https://<name>.innotel.site/ | head -1
   echo | openssl s_client -connect <name>.innotel.site:443 \
     -servername <name>.innotel.site 2>/dev/null | openssl x509 -noout -subject -dates
   ```

   The certificate must read `CN=*.innotel.site`. A per-host certificate here is a
   regression: it means the wildcard was not attached.

## Failure modes

| Symptom | Cause |
|---|---|
| `502 Bad Gateway` from NPM | Nothing listening on the upstream port — the app is stopped, the redeploy never ran, or `ports_mappings` is missing so no port was published. |
| `TLS alert, unrecognized name` | The name has no proxy host; `nginx -T` is the authority, not `/data/nginx/*.conf`. |
| App answers on `sslip.io`, not on its name | `fqdn` is empty, so Coolify fell back to a generated name. Set the domain; do not create DNS for the sslip name. |
| `https://<name>` serves a certificate for another host | The proxy host was created without `certificateId: 186` and NPM picked its default. Reattach 186. |

`instance_settings` has no `wildcard_domain` column in Coolify 4.7.5, so the zone is not
something the panel can be told once: the wildcard lives in Technitium (DNS) and in NPM
(certificate 186), and per-app names are what keep every site on `*.innotel.site`.
