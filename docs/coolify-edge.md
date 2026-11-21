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
| **Deploy** | Coolify (`coolify` instance on `i3`, `192.168.1.85`) | builds and runs the container, publishes the port, and knows the app only as a name. |

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

Builds run on whichever **server record** the app's destination points at, reached over SSH,
so the plugin has to exist on that host. Verified 2026-11-21 on all four: `i1` (`192.168.1.51`),
`i2`, `i3` and `i4` each report `docker buildx v0.39.1` — nothing needed installing.
`docker buildx` inside the `coolify` container is absent by design (`docker: unknown command:
docker buildx`), so a *localhost* destination (the panel's own dockerd) cannot build at all;
that destination is for the panel, not for apps.

**Moving the panel did not move the apps** (2026-11-20): `coolify` was migrated to `i3`, but
`servers` still held only `localhost` and `i2-local`, so every app kept building and running
on i2's dockerd — on 2026-11-21 03:00 `opencompany`'s container
(`rhw5wyfye0axrw6d35ryetol-20261121T022856`) was `Up (healthy)` at **i2's** `0.0.0.0:3001`
while i3's dockerd ran no app containers at all. A panel move is half a move: the app stays
where its destination points until the destination changes. `i3-local` is now server record 2
(2026-11-21) and `opencompany` and `signara` point at it. What is proven is the path, not yet
a container: deploys reach i3 over SSH, pass the network check, and build. `opencompany`'s own
build (Playwright/Chromium image, 10+ min cold) had not completed — its attempts were cancelled
by competing deploys — so its **live** instance is still the `i2` one.

## i3 cannot finish a build: Docker 29's buildkit session dies at export

Recorded 2026-11-21, because it looks exactly like a slow build. On `i3` a Dockerfile deploy
reaches `#15 exporting layers` after every layer is `CACHED`, the queue row's `updated_at`
freezes on that line, and it never advances — 20+ minutes, with `containerd` writing ~1 MB per
20 s and no image appearing in `docker images`. The daemon log names the moment:

```
level=error msg="healthcheck failed fatally"
error="session healthcheck failed fatally: Unavailable: connection error: desc = \"transport:
Error while dialing: only one connection allowed\""
```

That is BuildKit's client session dying as the export starts, so the build cannot complete
regardless of free disk (25 GB), free CPU (host emptied of containers), or a daemon restart.
It is specific to builds **driven from inside the Coolify helper container** against the
mounted `/var/run/docker.sock`: the same host exports a small image fine (`alpine` 12.1 MB,
`node:22-bookworm-slim` 329 MB both complete), while `opencompany`'s 5.18 GB image — which
builds and exports normally on `i2` — never leaves `exporting layers` on `i3`. `i3` runs
Docker **29.10.0** with the **containerd image store** (`driver-type:
io.containerd.snapshotter.v1`); `i2` runs Docker 29.1.3 with the classic graph driver, which is
where the image was built.

Until that is fixed on `i3`, an app whose destination is `i3-local` cannot be deployed, so
`opencompany` stays on `i2-local` and NPM 390 must point at `192.168.1.52:3001`. Two ways
forward: pin `i3`'s engine to the version that works on `i2`, or publish the image to a
registry and let the app use a prebuilt image instead of a Dockerfile build.

## Adding a deploy host without the UI

A server record is not just a row in `servers`: Coolify resolves `$server->settings` on
every deployment, and a host added by hand without that row fails with a bare
**500 `Server Error`** and no log line (the encrypted payload that actually breaks shows up
later as `DecryptException: The payload is invalid`). The working shape for `i3-local`
(2026-11-21), all four checks needed:

1. **`server_settings` row.** Copy i2's row rather than inventing one — it carries the
   sentinel token, timeout and cleanup defaults. Without it, `POST /api/v1/deploy` 500s.
2. **`private_keys.private_key` is encrypted at rest.** An OpenSSH PEM pasted into the row
   decrypts to nothing; every real row starts `eyJpdiI6…`. Either create the key through the
   panel, or reuse an existing key id and authorize *its* public half on the new host —
   `i3` runs the same `coolify` key as `i2` (fingerprint `SHA256:2u0Y…`).
3. **The docker network named in `standalone_dockers.network` must exist on the host.**
   Ours is `coolify`; on a bare host the deploy dies with
   `Error response from daemon: network coolify not found`. `docker network create
   --attachable coolify` is the fix (i2's copy is `10.0.1.0/24`).
4. **The deploy host must accept that key as the `servers.user` it is told.** Coolify logs
   `root@<ip>: Permission denied (publickey)` when the two disagree — the fingerprint in
   `/root/.ssh/authorized_keys` on the target is the thing to compare, not the key name.

`servers.proxy` stays `{"type":"NONE","redirect_enabled":true}`, like `i2-local`: no
Traefik is installed on a deploy host here, and the app publishes a host port instead.

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
| `opencompany` | `https://opencompany.innotel.site` | `3001:3000` | NPM 390 → `192.168.1.52:3001`, cert 156. Destination was moved to `i3-local` on 2026-11-21, but that host cannot finish the build (see below), so the door points back at the live `i2` copy. |
| `artex` | `https://artex.innotel.site` | `8790:8787` | NPM 377 → `192.168.1.52:8790` |

**`opencompany` is published again** (2026-11-20). It had been left `running:healthy` with
no door; row **390** now serves `https://opencompany.innotel.site` → `192.168.1.52:3001`
with cert **156**, and `/api/health` answers **200 in ~0.2 s** through the edge. The
`enabled=0` http-only row 332 was deleted, but NPM had left `332.conf` behind — a second
`server_name opencompany.innotel.site` that would shadow the live vhost on the next
reload — so the file was removed (copy at `/root/retired-332-opencompany.conf-20261120`).
Both sign-in URIs are back on provider 62: `…/api/auth/sso/callback` (authorization,
`PASS`) and `/signin` (logout, a `NOTE` beside it). `openco.innotel.tech` stays retired —
it has no A record at all, so there is nothing for a door to answer.

The app's Coolify record still says `fqdn=http://opencompany.innotel.site`: the API rejects
`fqdn` on PATCH ("Use the domains field") and accepts `domains` with a 200 that does not
rewrite it. So if the app builds its `redirect_uri` from that record, a sign-in can still
bounce to `http`. The route map says the registration matches reality — `/signin` 200,
`/api/auth/sso/callback` 307 without a code, `/api/auth/sso` 404 — but a completed browser
sign-in has not been observed, the same limitation the other apps here carry.
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

## Builds are the load risk, not the panel

A plain redeploy builds from cache and is cheap — deployment 99 on 2026-11-21 took **16 s**.
A **forced** rebuild is not: Coolify passes `--no-cache`, and on `i2` that is 24 minutes of
all-core compiling. On 2026-11-20 it drove load to 84 and OOM-killed five tasks, which is
why `sshd` could not send a banner and four estate checks failed at once. The record is
explicit — `application_deployment_queues.logs` contains `--no-cache` in deployments 93,
95 and 98, and in none of the cached ones (90, 91, 92, 94, 96, 99).

So the guard belongs on the build, not on the app:

```bash
ssh root@192.168.1.52 'systemctl set-property docker.service CPUQuota=400%'
```

BuildKit runs inside `docker.service`, so this caps a build at four of the eight cores while
the app containers, which live in `docker.slice` scopes of their own, keep their shares. A
forced rebuild then makes i2 slow instead of unreachable. Do not "fix" this by turning off
`disable_build_cache` in `application_settings` — it is already `f` (cache on); the
uncached path is the `force_rebuild` flag, and that is a choice made per deployment.

A rebuild on `i3` is not cheaper than one on `i2` — `opencompany`'s Dockerfile pulls
Playwright's Chromium and a desktop library set, so a cold build ran past six minutes of
apt and download. The CPU cap above is the guard until i3 has one too.

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
