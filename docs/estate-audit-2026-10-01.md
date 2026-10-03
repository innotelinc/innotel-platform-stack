# Estate audit — 2026-10-01

Consolidated audit of the Innotel Platform Stack across all root servers: host
and container placement, SecretOps (Cerulean Vault) migration status, resource
limits, storage pressure, and the incidents resolved this pass.

Provenance: everything below was re-verified live on 2026-10-01 unless a line is
marked _(prior pass)_ — those figures come from the 2026-09-27/09-30 sessions and
were not re-measured here.

---

## 1. Host / container map

| Host | Address | Role | Notes |
|---|---|---|---|
| **i1** | `192.168.1.51` | bare metal, Incus cluster (`incus` zfs pool) | 15.3 GiB RAM. Containers: `proxy` (`.71`, the Cerulean edge — NPM, Authentik, Vault, Technitium, OmniRoute, metrics), `ontrak` (`.20`), `monarch` (`.56`, media) |
| **i2** | `192.168.1.52` | KVM guest on pm3 (VM 200) | 8 vCPU / 13 GiB. Pools: `tank` (zfs, 290 GiB) + `main-pool` (btrfs USB, 465 GiB). Containers: `atlas`, `capstone`, `dev`, `genesis`, `rizzaura`, `terminal`, `vault`, `www` |
| **i3** | `192.168.1.53` | KVM guest on pm4 (VM 200) | ~5.3 GiB, ~1.9 GiB free. Containers: `distro`, `magnate`, `mail`, `onyx`, `pi`, `signara`, `subscribe`, `vpn`; stopped `acme`, `olympus-archived-20260930`, `patchmon` |
| **i4** | `192.168.1.54` | bare metal, Incus | ~7.8 GiB. One test container `lantest` (`.214`, routed NIC). Wi-Fi-only uplink |
| **pm3** | `192.168.1.3` | Proxmox VE 9.2 | runs VM 200 (`i2`) |
| **pm4** | `192.168.1.4` | Proxmox VE 9.2 | runs VM 200 (`i3`) |

Run-time note: every service container runs its compose stack from **its own
copy** of the monorepo checkout (e.g. zeus from `/usr/src/projects/complete/2-voice/zeus`
inside the `capstone` container, monarch from the `3-media/monarch` checkout in
`monarch`). `/usr/src/projects/complete` is **not** a shared bind mount — each
container has its own clone, so the dev box's repo and the live checkout can
diverge. Where that mattered this pass, the live checkout was edited and the
dev-box repo updated to match.

## 2. SecretOps — Cerulean Vault migration status

Vault: `http://192.168.1.71:8200`, KV v2 mount `cerulean`, unsealed. Each product
holds a path-scoped token minted by `cerulean/scripts/vault-entrypoint.sh` from
`VAULT_PRODUCT_TOKENS` and delivered to `<repo>/data/vault/token/<product>.token`.

| Product | Status | Detail |
|---|---|---|
| Magnate | ✅ done _(prior pass)_ | 8 runtime keys resolve; entrypoint bind-mount |
| ONYX | ✅ done _(prior pass)_ | `cerulean/onyx`, 5 keys |
| Genesis | ✅ done _(prior pass)_ | 2 keys, path-scoped token |
| **Zeus** | ✅ **done this pass** | `.env` → 10 `vault://` refs; portal resolves at boot (`resolved 10 secret reference(s)`). Migrated `VOIPMS_API_PASSWORD`, `OMNIROUTE_API_KEY`, `AUTHENTIK_CLIENT_SECRET`, `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_TOKEN`, `AUTHENTIK_POSTGRES_PASSWORD`, `AUTHENTIK_REDIS_PASSWORD`, `AUTHENTIK_BOOTSTRAP_PASSWORD` into `cerulean/zeus` (14 keys total after the shared-key pass, unioned with `SESSION_SECRET`/`VOIPMS_SIP_PASS`). Portal image rebuilt `zeus/portal:gateway-vault-20261001b` with `VAULT_KEYS` extended. The `check-vault-refs.py` sweep also caught five `change-me-…` placeholders (comment-contaminated) that had been migrated verbatim; all five were re-minted, and the checker now reports all 26 estate references resolve |
| Capstone | ⚠️ repo onboarded, **not deployed** | No `capstone` compose project runs anywhere; the `capstone` container runs the **zeus** stack only. Its 36-key `.env` is therefore not live; migration deferred until it is deployed |
| Rizzaura | ⏭️ **skipped (user decision)** | Exists only on i2 (`.62`); not propagated across the root servers. No Cerulean token, no resolver. Skipped per instruction |
| Atlas | ✅ **done this pass** | `cerulean/atlas` holds 6 keys; `.env.example` declares all six as `vault://` refs and the live `/opt/atlas/.env` resolves them via `vault-resolve.py` (`--check` + `--write`). `git.innotel.us`/Convex verified healthy. Retired `CHEF_*` + `OPENAI_API_KEY` remain undeclared leftovers in the live `.env` (nothing reads them) |

Zeus (fully migrated this pass — see `2-voice/zeus/docs/stack.md`): the portal
resolves ten references at boot, and the shared keys that a sibling reads through
compose interpolation — `TURN_CREDENTIAL` (coturn), `FREEPBX_AMI_SECRET` /
`PBX_DB_PASS` / `AVANTFAX_DB_PASS` (freepbx) — are now references too. A new
`scripts/compose-vault.sh` resolves the whole `.env` through
`scripts/vault-env-file.mjs` into `data/.env.resolved` and passes it as
`--env-file`, so compose interpolates real values for freepbx/coturn while the
portal's own `env_file` still carries refs for its entrypoint. `cerulean/zeus`
now holds 14 keys. Every `docker compose` run for this stack must go through the
wrapper; `compose-vault.sh … config` confirms only the portal keeps references.

A final `ips/scripts/check-vault-refs.py` sweep over the whole estate surfaced one
class of defect it exists to catch: five `cerulean/zeus` values
(`AUTHENTIK_SECRET_KEY`, `AUTHENTIK_TOKEN`, `AUTHENTIK_POSTGRES_PASSWORD`,
`AUTHENTIK_REDIS_PASSWORD`, `AUTHENTIK_BOOTSTRAP_PASSWORD`) were the
`change-me-…` placeholders **plus their trailing `# openssl rand …` comments**,
copied out of `.env.docker.example` by the 2026-09-16 migration. They feed only
`docker-compose.platform.yml` (the optional self-hosted Authentik, not deployed —
`auth.zeus.innotel.us` is served by Cerulean's Authentik), so nothing was live-
broken; they were still "present but not usable". All five were re-minted per the
template's documented `openssl rand` shapes, and the checker now passes clean.

`NPM_*`, `AVA_*` and the like stay plaintext: `scripts/setup.sh` reads `.env`
directly and does not resolve references.

## 3. Resource limits / memory posture

| Change | Where | Value |
|---|---|---|
| i1 `monarch` | `limits.memory` | **6 GiB → 8 GiB (2026-10-01)** — a leaking qBittorrent OOM-looped the cgroup; see §6 |
| i1 `ontrak` / `proxy` | `limits.memory` | 2 GiB / 6 GiB _(prior pass)_ |
| i2 `atlas` | `limits.memory` | 1 GiB _(prior pass)_ |
| i2 `rizzaura` | `limits.memory` | 1 GiB _(prior pass)_ |
| i2 `terminal` / `capstone` / `dev` / `genesis` / `vault` / `www` | `limits.memory` | 2 / 6 / 4 / 2 / 4096MiB / 6144MiB _(prior pass)_ |
| i3 `magnate` | `limits.memory` | 1 GiB _(prior pass)_ |

Incus `limits.memory` is a **cap**, not a reservation, so raising monarch did not
consume host memory. i1 sat at ~8.3 GiB used / ~6.9 GiB available after the rise.

## 4. Storage

| Risk | Detail |
|---|---|
| **pm3 `local-lvm` ~92 %** _(tightest)_ | The i2 VM's virtual disks (50 G root + 300 G `tank`) sit on it; `vm-200-disk-2` ~99.6 % allocated. `tank` `autotrim` is `on` and `discard=on` is now set on pm3 VM 200's `scsi0`/`scsi1`, so freed zfs blocks can return once the VM restarts — **the reclaim is pending that restart** (a live `fstrim` trimmed 13 GiB with no Data% change, proving the running QEMU drops TRIMs) |
| **i2 `main-pool`** (new 2026-10-01) | 465 GiB USB-attached btrfs; `capstone` (28 G) + `www` (13 G) moved off `tank`; `default`/`docker` profiles repointed. Single device, no redundancy — bulk capacity only |
| `tank` | ~48 GiB used after the moves (was ~90) |
| i2 root | ~72 % _(prior pass)_ |

## 5. Networking

- **i4 routed NICs — persisted this pass.** `lantest` (`.214`) uses an incus
  `routed` NIC on `wlp1s0` with host proxy ARP. The forwarding sysctls and the
  `FORWARD wlp1s0 ↔ veth+` rules are now installed by
  `ips/hosts/i4/incus-routed-firewall{,.service}` (enabled, idempotent). Proven
  across a real reboot: unit ran at boot, rules present, reachable both ways.
- **i1 rebooted** (§6). `proxy`, `ontrak`, `monarch` all `boot.autostart=true`;
  they came back and Cerulean/Vault/OmniRoute/edge recovered.

## 6. Incidents resolved this pass

### qBittorrent OOM crash loop → monarch wedge → i1 reboot

- Symptom: drift check `infra: qbittorrent restarted N times (>= 10)`; 242 in the
  report text, 28 on the live container.
- Root cause chain: **qBittorrent 5.2.x memory leak** (upstream
  qBittorrent/qBittorrent#24618, ~10 GB/day) plus a legacy
  `Session\DiskCacheSize=131072` (128 GiB) in the live config → RSS climbed
  ~8 MiB/s → hit the **monarch cgroup** cap (6 GiB; `memory.events: oom_kill 15`)
  every ~8 min → OOM-killed → `restart: unless-stopped` looped it → eventually
  the process wedged as a zombie whose threads stayed **in-kernel in `R` state**
  (SIGKILL ignored). Docker reported it `Up` with a **dead WebUI and no
  healthcheck**, so nothing recovered it.
- Escalation: `incus stop --force` could not kill the kernel-stuck task; it then
  wedged monarch's init and only a **reboot of i1** cleared it.
- Fixes:
  - Live `qBittorrent.conf`: `Session\MemoryWorkingSetLimit=512`,
    `DiskCacheSize=512`, `DiskIOReadMode=DisableOSCache`,
    `DiskIOWriteMode=WriteThrough`. RSS now plateaus at ~600 MiB.
  - monarch `limits.memory` 6 → 8 GiB.
  - `3-media/monarch/docker-compose.yml`: image pinned `5.2.4-1` (was `:latest`)
    and a WebUI **healthcheck** added, so a dead-but-running container reports
    `unhealthy` instead of silently staying `Up`.
  - Recreated qbittorrent (restart count reset). Drift check now passes:
    `all live-stack invariants OK`.
- Follow-up: if the 5.2.x leak persists, downgrade the pinned tag (pre-5.2) once
  a fixed release lands. Documented in `3-media/monarch/docs/operations.md`.

### i1 recovery

Post-reboot, `proxy`'s Authentik took a few minutes to become ready, during which
`gateway-sso` (the OmniRoute oauth2-proxy front on `:20128`) crash-looped on OIDC
discovery (`503 authentik starting`) and self-healed once Authentik was up.
Verified: Vault `200`, OmniRoute `200`, NPM edge `200`, `auth.cerulean` `302`,
`gateway-sso` healthy.

## 7. Dead NPM proxy hosts _(prior pass)_

NPM edge (`192.168.1.71:81`) had 170 proxy hosts; **12 dead ones were disabled**
(ids 2, 13, 30, 54, 61, 63, 91, 99, 134, 139, 160, 165). `cola.innotel.us`
(id 62) was left enabled deliberately.

## 8. Open items

1. ~~**Atlas** remains the largest plaintext surface~~ — **done 2026-10-01**
   (`cerulean/atlas`, 6 keys). The largest remaining plaintext surface is
   `5-dev/atlas`'s retired `CHEF_*` / `OPENAI_API_KEY` leftovers, which nothing
   reads.
2. ~~**Zeus shared-key literals**~~ — **done 2026-10-01**: `scripts/compose-vault.sh`
   + `scripts/vault-env-file.mjs` resolve the whole `.env`, so
   `TURN_CREDENTIAL`, `FREEPBX_AMI_SECRET`, `PBX_DB_PASS` and
   `AVANTFAX_DB_PASS` are references and freepbx/coturn still get real values.
   Also fixed the same day: five `change-me-…` placeholders (with inline
   comments) that the 2026-09-16 migration had copied into Vault were re-minted,
   so `check-vault-refs.py` now reports every estate reference resolves.
3. **Capstone** has no running deployment; its 36-key `.env` cannot be migrated
   until it is deployed.
4. **i2 divergence**: live checkouts (zeus, monarch, …) are separate clones from
   the dev box and have drifted onto independent commit lines (both sides carry
   wanted work). Surveyed 2026-10-01 in
   `docs/checkout-drift-2026-10-01.md`; the real fix is a shared remote + a
   deliberate merge, not a copy.
5. **pm3 thin pool at ~92 %**: `discard=on` is configured on VM 200's disks and
   `tank` `autotrim` is on. **Pending: restart i2, then `zpool trim tank`** to
   actually return the free extents (deferred to a maintenance window; the
   restart drops the dev box).
6. **onyx `privd`/`storaged`** still broken (pulled image is bad; rebuild OOMs
   without memory headroom).
7. **`main-pool` is single-device USB btrfs** — no redundancy; keep `tank`
   snapshots for important roots until it has a backup path.
8. **i4** has no wired NIC; routed NICs are persisted, but a wired uplink remains
   the durable fix.
9. **magnate / zeus portal images** are rebuilt/pinned locally only — no registry
   push, so a rebuild is required on a new host.
