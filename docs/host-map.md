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
repository, in a roadmap, or in a committed `.env`.

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
