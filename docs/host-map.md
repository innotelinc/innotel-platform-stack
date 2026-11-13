# Host map — the addresses that answer

Verified live on **2026-11-07** over SSH to each address. This page records where
each product actually runs, because a placement written in a roadmap is only worth
what a login to that host confirms.

| Address | Hostname | Kind | What runs there | Verified |
|---|---|---|---|---|
| `192.168.1.51` | `i1` | Incus host (bare metal) | `ontrak` container: the OnTrak family, 17 containers healthy (`training` on `:3000`, Genie on `:3400`). Other estate containers sit on the same host. | `incus exec ontrak` |
| `192.168.1.56` | `monarch` | container on `i1` | 36 containers: project `monarch` (Monarch landing/health/recs, the media services and 11 `*-sso` oauth2-proxy sidecars) and project `innotel-metrics` (grafana `127.0.0.1:3301`, prometheus, node-exporter). | SSH, `docker ps` |
| `192.168.1.61` | `distro` | container | project `distro`: `distro-control-plane` on `:20140` (`/health` → 200). | SSH, `docker ps` |
| `192.168.1.71` | `proxy` | container on `i1` | 23 containers: `cerulean`, `npm-edge` (project `proxy`, profile `npm`), `innotel-gateway` (omniroute + gateway-sso + redis), `innotel-metrics`, `ntfy`, `orion-sso`, `orion-locker-sso`, `innotel-deploy`. | SSH, `docker ps` |

Every one of these addresses is reached as **root over SSH** with the estate
credential held in the operator store. The credential itself never belongs in this
repository, in a roadmap, or in a committed `.env`. The hosts do not all share one
password, so the checker accepts a per-address override.

## Published names

The public name an app answers on is not always the container's name. Read the
edge before concluding a product is unreachable:

| App | Public name | Upstream | Notes |
|---|---|---|---|
| clipbucket | `tube.innotel.us` | `192.168.1.56:14011` | NPM proxy host 295, wildcard cert, not a `*.monarch` name |
| iptv | `tv.monarch.innotel.us` | `192.168.1.56:14012` | NPM proxy host 162 |
| radarr | `movies.innotel.us` | `192.168.1.56:14002` | 301 to the alias |
| lidarr | `mp3.innotel.us` | `192.168.1.56:14003` | 301 to the alias |
| whisparr | `xxx.innotel.us` | `192.168.1.56:14004` | 301 to the alias |
| learner portal | `lms.innotel.us` | *(retired)* | **Folded into `atheniq.courses` 2026-11-13.** The name had neither a record nor a loaded vhost; `227.conf` on the proxy host was an **orphan** (NPM's live data dir is `/usr/src/proxy/data`, so nothing ever loaded that file). Fixed: `A 73.68.203.71` added to Technitium (zone `innotel.us`, SOA 676 → 677, TTL 300) and redirection host `36` 301s to `https://atheniq.courses`. Verified: `301 → https://atheniq.courses/`, follow-through **200** at `/catalog/`, LE cert `CN=lms.innotel.us` valid to 2027-01-15, `:80` → 301 https. Legacy prefixes are mapped rather than left dead: `/learn`, `/home` → `/catalog/`; `/student`, `/my-learning` → `/dashboard/` (both verified 200 after follow-through), while `/courses`, `/catalog`, `/dashboard` keep the pass-through. |

`apps.lms.innotel.us` is this estate's superseded-name pattern: a **CNAME to the apex**
(`innotel.us` → `73.68.203.71`) plus a redirection host (`35`) that 301s to
`https://apps.atheniq.courses`. Verified 2026-11-12: the CNAME resolves and the name
answers `301`.

Two failure modes are worth knowing, because both look like "the edge is configured":

1. **A record missing while the vhost exists.** The name does not resolve at all.
2. **A vhost file that nothing loads.** `proxy_host/227.conf` sat on the proxy host for
   `lms.innotel.us` while NPM's live data dir is `/usr/src/proxy/data` — the file was
   outside it, so `nginx -T` never contained the name, and 443 fell through to the
   default server, which has `ssl_reject_handshake on`. The client symptom is
   `TLS alert, unrecognized name`, not a proxy error.

Check the *loaded* configuration, not the file on disk: `nginx -T | grep server_name <name>`
must match. `docker exec cerulean-npm nginx -T` is the only trustworthy answer.

All eleven sidecars authenticate against the single Authentik provider
**`Monarch-media` (PK 37, client `monarch-media`)**. Its redirect list is the
authority on which callback is legal: in this pass it held 19 URIs and was missing
`radarr`, `lidarr`, `whisparr`, `clipbucket` and `iptv`, so those five answered
`400 invalid, or mismatching redirection URI`. Those five names were added (24
total), so each sidecar's own hostname is legal, and every one now redirects into
the login flow. The two real public names, `tube.innotel.us` and
`tv.monarch.innotel.us`, were already registered: the aliases worked before this
pass, and it was the `*.monarch` names that did not.

## The gateway is behind two doors, and only one is Authentik

`192.168.1.71:20128` is `gateway-sso` (oauth2-proxy) in front of OmniRoute, which
itself is published on loopback only. The proxy already exempts the routes that
must answer without a session:

```
--skip-auth-route=^/v1/
--skip-auth-route=^/healthz$
--skip-auth-route=GET=^/api/system/version(\?.*)?$
--skip-auth-route=^/api/auth/login(\?.*)?$
--skip-auth-route=^/api/keys(\?.*)?$
```

So `/api/system/version` never touches Authentik — the `401 AUTH_001` it returns is
**OmniRoute's own** auth. `/api/health` and `/api/monitoring/health` answer JSON
without a session, but neither carries a version, so a version probe cannot avoid
the gateway login. Distro's probe authenticates with `GATEWAY_ADMIN_PASSWORD` and
now records `gateway version: 3.8.53 — pin none → unpinned`.

## Re-apply, not rebuild

Redeploying a project here means re-applying its own compose file from its own
working directory, with the env file that project needs:

```bash
# 192.168.1.56
cd /usr/src/projects/complete/3-media/monarch
docker compose -p monarch --env-file .env -f docker-compose.yml up -d --no-build
cd /usr/src/projects/complete/ips/extensions/monitoring
docker compose -p innotel-metrics --env-file .env.host -f docker-compose.host.yml up -d --no-build

# 192.168.1.61
cd /opt/distro
docker compose -p distro --env-file .env -f docker-compose.yml up -d --no-build

# 192.168.1.71 (the gateway needs its host secrets or both SSO containers crash-loop)
cd /usr/src/projects/complete/ips/extensions/llm
docker compose -p innotel-gateway --env-file .env.host -f docker-compose.host.yml up -d --no-build
```

`--env-file` is not optional where a stack keeps its real values there: omitting it
on `innotel-gateway` leaves `cookie-secret`, `client-id` and the Redis password
empty, and the containers restart with `missing setting: cookie-secret`. When
nothing in the config changed, `up -d` leaves the containers running — that is a
healthy re-apply, not a rebuild.

## Checking this page

`scripts/check-host-map.py` reads the table above and logs in to each address,
failing when the hostname that answers is not the one written down:

```bash
scripts/check-host-map.py                       # key-based SSH as root
scripts/check-host-map.py --ssh 'sshpass -p "$ESTATE_PASSWORD" ssh' \
  --ssh-for 192.168.1.51='sshpass -p "$I1_PASSWORD" ssh'
```

Credentials come from the environment or an agent, never from this repository. The
check verifies reachability, the hostname, and that dockerd answers; it does not
compare container counts, which move with normal operation. SSH to these hosts
sheds sessions under load, so a host is retried (`--retries`, default 3) before it
is reported as a mismatch — a reset is a network event, not a placement error.

## Placement corrections from this pass

- **There is no `192.168.1.30` and no `192.168.1.74`.** Neither address answers, and
  neither is a host in the estate. `docs/versions.md` previously recorded Zeus and
  Capstone as "server 2 (192.168.1.30)"; that cell now reads "server 2 (address
  unverified)" until a login confirms the real address.
- **`.56`, `.61` and `.71` are Incus containers with their own LAN address and their
  own dockerd**, not separate machines. `hostname` reports `monarch`, `distro` and
  `proxy` respectively.
- **`gateway.innotel.us` and `192.168.1.71:20128` are the SSO edge, not the gateway
  API.** The OmniRoute dashboard is published on loopback only
  (`127.0.0.1:20128`); `:20128` on the LAN is `gateway-sso`, which answers
  `/api/system/version` with `401 AUTH_001` and redirects `/api/health` to the
  Authentik flow. A control plane that probes the gateway must either log in and
  reuse the cookie, or talk to a route that answers JSON without the proxy.
