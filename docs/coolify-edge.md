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
