# Service & Package Audit — the whole stack

**Status: current** · updated September 14, 2026

The stack-wide audit: every service and package that appears in more than one
platform repo, whether the duplication is *required* by that stack's shape or
removable, and the memory-efficiency posture. The golden rule this audit
enforces is the standard's rule 1 — **one job per platform; consume the
platform services, never re-implement them.**

Verdicts:

- **KEEP — required**: the duplication exists because the stack runs
  standalone (air-gapped / single-box) by design; removing it would break the
  documented deployment shape.
- **KEEP — profile-gated**: the duplicate exists but only starts under a
  compose profile, so it costs nothing in the default topology.
- **CONSOLIDATE — done**: the duplicate was removed or pointed at the shared
  instance as part of this audit.
- **WATCH**: acceptable today, revisit on a trigger.

## 1. Cross-stack service duplication

| Service | Appears in | Verdict | Rationale |
|---|---|---|---|
| **Authentik** | cerulean (owner), + `auth.<platform>` NPM aliases for every stack | KEEP — required | IdentityOps is owned once (cerulean's compose); every other stack only fronts it via an NPM alias. No repo ships a second Authentik container. |
| **Infisical** | retired — no repo ships a `compose.infisical.yml` any more | REMOVED | SecretOps is **Cerulean Vault** (HashiCorp Vault, KV v2). The instance at `secrets.cerulean.innotel.us` survives only as a migration *source* for `scripts/vault-migrate.py`; a leftover `infisical://` value in a `.env` is a deployment error, not a fallback. |
| **Postgres** | capstone, zeus(platform), monarch, signara(dev/prod), oasis, atheniq, atlas, npm (upstream CI), onyx, distro(redis-only) | KEEP — required | Each platform owns its own application database — data isolation is the tenant boundary (standard §Security 4). A shared Postgres would couple tenant data across platforms. Version skew (16/16-alpine/16.4/17) is intentional: pin per repo, upgrade independently. |
| **Redis** | capstone, zeus(platform), monarch, oasis, signara | KEEP — required | Same data-isolation rationale: queues/cache per platform. The retired Infisical profiles were the last Redis consumers to go. |
| **MinIO** | capstone, signara(dev/prod), + platform-stack group 1 | KEEP — required | Object storage per platform for its own media/transcripts. ONYX remains the StorageOps layer for platform-level storage; these are application-internal buckets, not a second storage platform. |
| **Coturn (TURN)** | capstone (profile `standalone`), zeus (primary) | KEEP — profile-gated | Zeus owns the shared TURN relay; capstone's copy only starts in standalone mode (`profiles: ["standalone"]`). In add-on mode the zeus instance is primary. |
| **FreePBX/Asterisk** | capstone (profile `standalone`), zeus (owner) | KEEP — profile-gated | The convergence target: one shared PBX (zeus). Capstone's bundled PBX is now behind the `standalone` profile (`CAPSTONE_PBX=zeus` skips it). Dev/offline keeps the bundled copy by design (§6.4 Q1: kept). |
| **OmniRoute** | zeus (shared gateway), platform-stack group 2 | CONSOLIDATE — done | One gateway serves the ecosystem. Distro's bundled `local-gateway` profile and Group 5's `distro-gateway` were removed; every platform points at the shared instance ([build-plane convergence](convergence-onyx-olympus-distro-atlas.md) §4). |
| **Nginx Proxy Manager** | npm (EdgeOps owner, shared at .71), monarch (bundle), onyx (profile `npm`), platform-stack group 3 | KEEP — profile-gated | One shared edge fronts every public host. The in-compose copies exist for self-contained appliance installs only and are profile-gated. |
| **BIND** | cerulean (`cerulean-bind`, owner), platform-stack mesh | KEEP — required | TrustOps owns DNS; nothing else ships a nameserver. |
| **SigNoz stack (ClickHouse/otel)** | capstone (owner), zeus (`compose.observability.yml`, optional profile) | KEEP — profile-gated | Capstone owns the observability topology; zeus's copy is the optional mirror documented in the convergence doc (Phase 1). App-side instrumentation stays optional. |
| **n8n** | capstone only | KEEP — required | Workflow automation is part of AgentOps; nobody else ships it. |
| **Kokoro / Speaches (TTS/STT)** | capstone only; zeus consumes via ARI agents | KEEP — required | Speech stack is AgentOps-owned; Zeus never hosts agents. |
| **Grist** | capstone only | KEEP — required | Dashboards for the control center; single consumer. |
| **Jellyfin / *arr family** | monarch only | KEEP — required | MediaOps-owned end to end. |
| **Vault** | cerulean only | KEEP — required | Part of the trust plane's PKI story. |
| **Zimbra** | oasis only | KEEP — required | MailOps-owned. |
| **Gitea / Convex / Chef** | atlas only (chef also consumed by distro via Atlas) | KEEP — required | CodeOps-owned; distro consumes Atlas's Chef, does not ship its own. |

**No removable cross-stack duplicates remain.** Every remaining duplication is
either the documented standalone shape (profile-gated, zero cost in the default
topology) or a per-platform data store required by tenant isolation. The
duplicates that *were* structurally redundant have been consolidated:
capstone's always-on PBX and coturn (profile-gated by the `CAPSTONE_PBX` work),
and the three OmniRoute deployments (now one — see
[build-plane convergence](convergence-onyx-olympus-distro-atlas.md) §4).

## 2. Package-level duplication (scripts/libraries)

| Package | Appears in | Verdict | Rationale |
|---|---|---|---|
| `npm-proxy-hosts.py` (per-repo provisioner) | 9 repos | KEEP — required | Each repo provisions its own zone's hosts; the scripts are intentionally stdlib-only and self-contained (standard: "self-contained Compose file plus a stdlib-only provisioner"). The *patterns* they share are now centralized (below). |
| **`stack-lib.sh` (NEW — centralized)** | ips (canonical) + verbatim mirror in all 15 repos | CONSOLIDATE — done | Common tasks (env resolution, LAN-IP/forward-host detection, NPM API helpers, output helpers) now live once in the canonical repo; `scripts/sync-stack-lib.sh` mirrors it. Repos source their local copy so scripts stay offline/CI-safe. |
| LAN-IP detection logic | was ad-hoc in distro/oasis/monarch/cerulean; missing in rizzaura/capstone | CONSOLIDATE — done | rizzaura + capstone provisioners gained `detect_lan_ip()` (mirroring `stack_lib_forward_host`); oasis switched its default from `host.docker.internal` to auto-detected LAN IP with explicit-env precedence. Docker-bridge/loopback addresses are never used as NPM upstreams. |
| `asterisk_converge.py` (twinned) | zeus + capstone | KEEP — required | Deliberately twinned per the convergence doc (G1): each repo's CI runs its own copy's unit tests; a cross-repo import would couple release trains. |
| attribution guard (`guard-lib` + hooks) | all repos, verbatim | KEEP — required | Standard §5 mandates verbatim copies; `conform-project.sh` is the distribution mechanism. |

## 3. Memory-efficiency posture

Every compose now caps its memory-relevant services with `mem_limit` +
`memswap_limit` (swap disabled so pressure is visible and OOM-kills stay
inside the container). Caps are sized from measured `docker stats` on the
live box, with headroom:

| Stack | Capped services | Limit |
|---|---|---|
| capstone | minio 1g · kokoro 2g · speaches 2g · n8n 1g (+ `NODE_OPTIONS=--max-old-space-size=768`) · grist 768m · signoz-clickhouse 2g | ~8.8g worst case (profile-gated services excluded from default) |
| zeus | omniroute 1g | 1g |
| monarch | jellyfin 4g (transcode bursts) | 4g |
| distro | web 1536m | 1.5g |
| magnate | magnate 1g | 1g |
| rizzaura | api 1g | 1g |
| cerulean | cerulean 768m · authentik-server 1500m · vault 512m | 2.8g |
| onyx | onyx-api 512m · onyx-objectstore 1g | 1.5g |
| atlas | gitea 2g | 2g |
| oasis | postgres 1g | 1g |
| atheniq | openmaic-postgres 1g | 1g |

Uncapped remainders are small one-shot/init containers (migrators, importers,
builders) and upstream-bundled sidecars where a cap would fight the image's
own tuning. The heaviest measured consumers (local-lms/cms workers at
~1.5 GiB) belong to repos outside this portfolio's compose files and are
unaffected.

## 4. Follow-ups (WATCH)

- Signara pins `postgres:16-alpine` in dev/prod and `postgres:16.4-alpine` in
  oasis — harmless skew; converge on one minor at the next joint upgrade.
- Zeus's optional observability profile duplicates capstone's SigNoz stack;
  if both run on one box, point zeus's OTel endpoint at capstone's collector
  instead of running a second ClickHouse (trigger: observability enabled on
  the shared server).
- `compose.infisical.yml` profiles are gone from every repo (the store is
  Cerulean Vault). The surviving shared secret tooling is
  `scripts/vault-migrate.py`, mirrored verbatim like `stack-lib.sh` — if it ever
  gains a repo-specific branch, promote it to a stack-lib-generated template
  instead.
