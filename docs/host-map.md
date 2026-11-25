# Host map — the addresses that answer

Verified live on **2026-11-07** over SSH to each address, with the `proxy` row corrected on
**2026-11-14** (it was listed on `i1`; `incus list` there has no such instance, and the edge, its
vault and `ntfy` all run in that container). **`i2` was recovered on 2026-11-15 and is parked**: it booted with its eight instances, three
pools and six Docker stacks intact, so nothing on it was lost, but every instance is stopped with
`boot.autostart=false` while its addresses are served from `i3` and `i4`
(`docs/estate-rebalance-2026-11-15.md` §19). **`i2` rejoined on 2026-11-17**: it was never empty — it carries
nine instances including `monarch` (the whole media estate) and the metrics stack — so leaving it parked meant a
reboot took the media estate offline with nothing announcing it. All nine now carry `boot.autostart=true`, each
after its LAN address was checked against the other three hosts and found unique (the hazard §19 describes was
two instances of one address, not two instances of one name: `atlas`, `blueprint`, `clearpath`, `regcache`,
`rizzaura` and `sage` exist on `i1`/`i4` too, at *different* addresses). This page records where
each product actually runs, because a placement written in a roadmap is only worth
what a login to that host confirms.

| Address | Hostname | Kind | What runs there | Verified |
|---|---|---|---|---|
| `192.168.1.51` | `i1` | Incus host (bare metal) | `ontrak` container: the OnTrak family, 28 containers running (23 with healthchecks green) — `training` on `:3000`, the control-plane `app` on `:3002`, Genie on `:3400`. Other estate containers sit on the same host. | `incus exec ontrak` |
| `192.168.1.52` | `i2` | Incus host (QEMU guest on `pm3`) | **Rejoined the estate on 2026-11-17** after being parked since 2026-11-15. Eight instances, all `boot.autostart=true` and all running: `monarch` `.56` (the 36-container media stack and the `innotel-metrics` Prometheus/Grafana the alert rules run on), `nas` `.89`, `atlas` `.90`, `blueprint` `.76`, `clearpath` `.83`, `genie-preview` `.24`, `regcache` `.79`, `rizzaura` `.62`. (`sage` is on `i3`; the nine-instance count here had kept a copy that is no longer on this host.) Its six host-Docker stacks (`christ*`, `scif-*`) were never stopped. `/` is the estate's tightest filesystem and cannot be grown from inside the guest — there is no free LVM extent (§11.5 of the rebalance page). | `incus list`, `incus config get <name> boot.autostart` |
| `192.168.1.53` | `i3` | Incus host (bare metal) | **Reinstalled with Ubuntu 26.04.1 on 2026-11-15 and returned to the estate** after being retired on 2026-11-02. Incus 6.0.5, pool `incus` (zfs, 362 G on a dedicated LV), `incusbr0` 10.105.132.1/24. **It carries `coolify` `.85` (moved here 2026-11-20), plus `distro` `.61`, `magnate` `.57`, `signara` `.44`, `sage` `.94` and `copilotkit` `.114`** at unchanged addresses — `distro`/`magnate`/`signara` carry `boot.autostart: true` since 2026-11-25, because the host's reboot on 2026-11-22 had left all three `STOPPED` with autostart `false`, so `.44`'s Alertmanager and every NPM target behind `.44`/`.57`/`.61` were dark until they were started; `sage` and `copilotkit` are the only copies of their names (both on this host since 2026-11-21, `copilotkit` started by its owner on 2026-11-25) and both were given `boot.autostart: true` that evening; `pi` `.70` was removed by its owner on 2026-11-20, so no row and no address assert it (`subscribe` `.58` went back to `i2` on 2026-11-18 — the earlier note that `copilotkit` did too was wrong, and `check-container-addresses` already records `copilotkit` and `sage` here) — plus a LAN bridge **`br0`** (took over `enp2s0`, same MAC `10:78:d2:9a:0d:52`) and a `tank` pool (`source=incus/tank`) that `onyx`'s device needs. `ontrak-winpack-build` is still a STOPPED Windows build VM (`docs/estate-rebalance-2026-11-15.md` §12.2); `onyx` left this host on 2026-11-17 — it is `onyx-retired` here, and the live container runs on `i1` with its USB disk (see the `.60` row). | `incus storage list`, `zpool list`, `incus list` |
| `192.168.1.22` | `terminal` | container on **`i4`** | **The web terminal**: `termix` (`ghcr.io/lukegus/termix:26.11.1`) on **`:8080`**, plus `guacd` (`:4822`), `zapit` (`:5150`) and `zapit-landing` (`:8081`) in the same container. It stayed STOPPED through the 2026-11-15 rebalance and came back, but its row and its vhost had both been removed while it was down — so it ran healthy while `terminal.innotel.us` resolved to the edge and answered `TLS alert, unrecognized name`. Fixed 2026-11-18: NPM host 375 → `192.168.1.22:8080`, websockets on, Let's Encrypt cert, `ssl_forced`. | `incus exec terminal -- docker ps`, `https://terminal.innotel.us/` |
| `192.168.1.68` | `ontrak-tix-db` | container on **`i4`** | **The live OnTrak Tix database**: `ontrak_tix` 3 884 MB, `Ticket` 5 000 479 / `Message` 2 400 948 / `User` 60 000. Moved off `i4`'s host Docker on 2026-11-15 into a capped Incus container (`1 GiB`, `1 vCPU`, `swap=0`, `security.nesting=true` because Docker runs inside it). The host-Docker original is stopped, not deleted, and its volume is intact. | `psql` over `.68:5432`, `incus config show` |
| `192.168.1.60` | `onyx` | container on **`i1`** | **The estate's object store, returned to `i1` on 2026-11-17** with the USB disk it passes through as `/dev/sdc` (btrfs, label `main-pool`) — `i1` is the only host that disk is attached to now. It owns the `tank` pool's `onyx-data` volume at `/mnt/onyx`; `signara-backups/authentik/` mirrors land on `:2091` and the storage console answers on `:2080`, gated by `onyx-sso`. `boot.autostart` stays `false`: the `main-pool-usb` device is `required: "false"`, so starting without the disk would come up serving nothing. The copy minted a new MAC, so `volatile.eth0.hwaddr` was set back to `10:66:6a:60:d4:9d` to keep the address. | `incus list`, `incus exec onyx` |
| `192.168.1.56` | `monarch` | container on `i2` | 36 containers: project `monarch` (Monarch landing/health/recs, the media services and 11 `*-sso` oauth2-proxy sidecars) and project `innotel-metrics` (grafana `127.0.0.1:3301`, prometheus, node-exporter). | SSH, `docker ps` |
| `192.168.1.57` | `magnate` | container on **`i3`** | `magnate` (Next.js, `:3002`) plus its `docker.service`. Reachability to the proxy host is by **SSH tunnels**, the same pattern as `gateway-tunnel.service`: `vault-tunnel.service` forwards `0.0.0.0:8200` to `127.0.0.1:8200` on `.71`, and the app reads `VAULT_ADDR=http://172.18.0.1:8200`. Before 2026-11-21 it pointed at `192.168.1.71:8200`, where Vault publishes only loopback and `172.17.0.1`, so it crash-looped 111 times with `ECONNREFUSED`. | `incus exec magnate -- docker ps`, `systemctl is-active vault-tunnel` |
| `192.168.1.61` | `distro` | container | project `distro`: `distro-control-plane` on `:20140` (`/health` → 200). | SSH, `docker ps` |
| `192.168.1.85` | `coolify` | container on **`i3`** | **Moved from `i2` on 2026-11-20** (live `incus move`, 1.99 GB) and answering `.85:8000` there; the stopped copy on `i2` is the rollback. **The panel move did not move the apps on its own**: on 2026-11-21 `i3` was added as deploy target `i3-local` (server record 2, `user=root`, the same key as `i2-local`) and `opencompany` now runs on **i3** at `.53:3001` (NPM 390 → `.53:3001`, `/api/health` **200**; the `i2` copy is gone). It had to move as a **prebuilt image** pulled from a `registry:2` on i3 (`:5000`), because that host cannot finish a Dockerfile build — the BuildKit session dies at `exporting layers` (see `docs/coolify-edge.md`), and `scripts/publish-image-to-i3.sh` now does that push in one command. **The binding constraint on that host is the root LV, not the box**: 98 G at ~80 % while the `incus` zpool beside it holds 332 G idle, which is why ``chatterbox``'s 23.4 GB image still runs on `i2` and why the next step there is moving Docker's store onto the pool.  | **The deploy panel, in a container of its own since 2026-11-18** (it had been the `coolify` compose stack on `i2`'s host Docker, publishing `:8000` on this host's own address). `coollabsio/coolify:4.7.5` + `coolify-db` (postgres:15-alpine) + `coolify-redis`, `security.nesting=true`, 2 vCPU / 4 GiB. Its `instance_settings.fqdn` is `https://deploy.innotel.us`, which NPM row 342 serves with websockets and forced SSL; `:113:8000` is only the webhook forwarder in `workloads`, repointed here. Database and `/data` were moved across, verified row for row (1 project, 3 applications, 2 servers, 1 user). | `incus exec coolify -- docker ps`, `https://deploy.innotel.us/login` |
| `192.168.1.71` | `proxy` | container on **`i4`** (moved from `i2` 2026-11-15, `estate-rebalance` §15) | containers: `cerulean`, `npm-edge` (project `proxy`, profile `npm`), `innotel-gateway` (omniroute + gateway-sso + redis), `innotel-metrics`, `ntfy`, `orion-sso`, `orion-locker-sso`, `innotel-deploy`. `ntfy` is host-networked and answers on **`:2586`** (`:8080` on this address is nginx — see `docs/estate-alerts-ntfy.md`). | SSH, `docker ps` |

Every one of these addresses is reached as **root over SSH** with the estate
credential held in the operator store. The credential itself never belongs in this
repository, in a roadmap, or in a committed `.env`. The hosts do not all share one
password, so the checker accepts a per-address override.

## Name resolution

Every command in these docs says `ssh i1`, not `ssh root@192.168.1.51`, so the four
names have to resolve on whichever host you are standing on. `hosts/estate-hosts` is
that block:

    # Estate hostnames — docs/host-map.md
    192.168.1.51 i1
    192.168.1.52 i2
    192.168.1.53 i3
    192.168.1.54 i4

Append it to `/etc/hosts` on a rebuilt host — it is not something the stack install
writes, and those four addresses are the whole of it. `i2` stays in the block while
it is parked: its address still answers, and `i1`…`i4` are the names this page and
the checks use. A host missing the block fails every name-based `ssh` with
`Could not resolve hostname`, which reads like a network outage rather than a missing
line (`i4` was in exactly that state on 2026-11-17).

## Published names

The public name an app answers on is not always the container's name. Read the
edge before concluding a product is unreachable:

| App | Public name | Upstream | Notes |
|---|---|---|---|
| clipbucket | `tube.innotel.us` | `192.168.1.56:14011` | NPM proxy host 295, wildcard cert, not a `*.monarch` name |
| iptv | `tv.monarch.innotel.us` | `192.168.1.56:14012` | NPM proxy host 162 |
| genie wildcard | `*.genie.innotel.us` | `192.168.1.21:3400` | NPM proxy host **207**, `ssl_forced`, websockets. Pointed at `192.168.1.24:3400` (an empty `genie-preview`) until 2026-11-21, so every wildcard name answered **502** while `genie.innotel.us` itself was live; row and `207.conf` both moved, verified `200`. |
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
