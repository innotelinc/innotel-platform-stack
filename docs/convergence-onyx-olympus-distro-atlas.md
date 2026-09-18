# ONYX · Olympus · Distro · Atlas — Build-Plane Convergence

**Status: proposed** · updated September 15, 2026

> **What this is.** The deep look at the four platforms that surround *building
> software* in this ecosystem — **ONYX** (StorageOps), **Atlas** (CodeOps),
> **Distro** (BuilderOps), **Olympus** (FactoryOps) — and the target architecture
> for the builder surface. It answers one question the portfolio has drifted away
> from: there are **three app-builder front doors, two of them forks of the same
> upstream**, and the stack's golden rule is one job per platform.
>
> The target: **one web UI, one terminal UI, one full-stack app builder, one
> OmniRoute.** This document is the plan; the Capstone ↔ Zeus doc
> ([convergence-capstone-zeus.md](convergence-capstone-zeus.md)) is the same
> exercise for the voice plane.

---

## 1. Current state

### 1.1 The surfaces that exist today

| Surface | Web UI | Terminal UI | Builder engine | Where a build executes |
|---|---|---|---|---|
| **Olympus** | `web/studio/` — Next.js (App Router), Authentik OIDC (PKCE + id_token verification), plan → stream → six deliveries | `scripts/olympus-tui.py` (`make tui`) — curses, queues and streams | `scripts/project_plan.py` → Archon `archon-greenfield` → Codex → `package-app.py` / `package-website.py` → `app-runtime.py` | **Host** — one container, loopback port, SQLite, nginx vhost, name published via Cerulean + NPM |
| **Distro** | `apps/control-plane` (accounts, per-user gateway keys, quotas, admin console) — the tenancy service; the bolt.diy front door (`apps/web`) retired §8 Phase 3 | — | builder surface: Studio (Olympus), pointed at this control plane | — (tenancy is not an execution surface) |
| **Atlas** | `services/chef` — **get-convex/chef, also a bolt.diy fork** (port 4310), Authentik auth fork landed | — | Chef agent | **Convex** — self-hosted reactive backend |
| **ONYX** | `web/landing/` only; the Prism SPA (`onyx-web`) is v0.2 roadmap | — | none today (`onyx-appd` hosts, `onyx-ai` advises) | — |

> Also the state the plan started from. Atlas's row — `services/chef`, port 4310 —
> is the one that has since changed: Chef retired as a builder in Phase 3, and the
> front door count is down to two (§8). Distro's row has since changed too: the
> `apps/web` surface is retired, and the repo ships the control plane only.

Two facts fall out of that table:

1. **There are three app-builder front doors and two of them are bolt.diy
   forks.** Distro forked bolt.diy to rebrand it; Atlas cloned
   `get-convex/chef`, itself a bolt.diy fork, to add a Convex backend. They now
   carry the same upstream shape, rebranded twice.
2. **Only one of the three engines ends with a running, named application.**
   WebContainer cannot run a server-side process and Chef's output is bound to
   Convex; Olympus's runner installs, builds, starts and publishes a container.

### 1.2 The AI plane today

| Repo | Gateway | Notes |
|---|---|---|
| Olympus | `omniroute` in `docker-compose.yml`, profile `gateway`, **loopback only** (`127.0.0.1:20128`), state in the `omniroute-data` volume | Dashboard behind `compose.gateway-sso.yml` (oauth2-proxy + Authentik); `make gateway-vault-backup` / `-check` |
| Distro | **remote by default** (Consul service `omniroute`); a bundled `gateway` + `redis` behind the `local-gateway` profile | `5-dev/docker-compose.yml` also bundles `distro-gateway` + `distro-redis` |
| Atlas | **remote only** — `make gateway-check` verifies the shared gateways; Atlas runs none | Chef receives `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `XAI_API_KEY` **directly** |
| ONYX | none | `onyx-ai` is local-first / BYO-key |

So there are up to **three** OmniRoute deployments in play (Olympus's profile,
Distro's profile, Group 5's, plus the shared Group 2 instance) for one AI plane.

> This table is the state the plan started from. §4 is the change list and §4.3
> / §4.4 are applied: Olympus and Group 5 no longer run a gateway at all, and
> Chef's model calls go through Group 2's.

### 1.3 The secrets plane today

| Repo | SecretOps today | Artifacts |
|---|---|---|
| Olympus | **Cerulean Vault** (KV v2), path-scoped, `vault://` references | `.env` `VAULT_*`; `scripts/vault-bootstrap.py`, `vault-renew.sh`; `omniroute` policy |
| ONYX | Infisical, `infisical://` runtime resolution in Go | `compose.infisical.yml`, `scripts/infisical-setup.{sh,py}`, `services/infisical/` |
| Atlas | Infisical | `.env.example` §"Infisical (SecretOps)"; no compose profile |
| Distro | **none yet** (convergence target) | `.env` on the host only |

The canonical definition is behind the repos: `README.md` and `docs/standard.md`
both still declare **"Infisical = Secrets"**, and `scripts/conform-project.sh`
scaffolds `INFISICAL_*` keys — while the platform Vault
(`cerulean-vault`) is what Olympus actually consumes.

> Also the state the plan started from. §6 is the change list: the canonical
> definition now reads **Cerulean Vault = Secrets** (§6.2), ONYX and Distro
> resolve `vault://` in their own services (§6.1), and Infisical — which §6
> first kept as a labelled legacy path — has since been **retired outright**:
> every `compose.infisical.yml`, `INFISICAL_*` block, `infisical-setup.{sh,py}`
> and `services/infisical/` package is deleted, and `scripts/vault-migrate.py`
> is the one surviving reader of the old instance, as a *source*.

---

## 2. Target model

```
USERS
  │
  ▼
┌──────────────────────────────────────────────────────────────┐
│  STUDIO — the one web UI  (Olympus web/studio, :3001)         │
│  plan → stream → build → preview → publish → export          │
├──────────────────────────────────────────────────────────────┤
│  OLYMPUS TUI — the one terminal UI  (scripts/olympus-tui.py)  │
├──────────────────────────────────────────────────────────────┤
│  THE BUILDER — the one engine  (project_plan → runner →       │
│  package-app/website → app-runtime)                          │
└──────────────────────────────────────────────────────────────┘
  │            │                │                 │
  ▼            ▼                ▼                 ▼
OMNIROUTE   AUTHENTIK       CERULEAN VAULT     ATLAS
(one,       (one,           (one,              (Gitea + Convex
Group 2)    Cerulean)       Cerulean)          + CI — CodeOps)
                                                 │
                                                 ▼
                                                ONYX
                                        (object storage / NAS)
```

Rules that keep it single-responsibility:

- **One web UI — Studio.** The browser surface for building an app.
- **One terminal UI — the Olympus TUI.** The Claude-Code-shaped front end; it
  queues and watches, it does not plan or generate.
- **One builder — Olympus's engine.** Planning, generation, packaging, running
  and publishing are one path with one input (a spec) and one runtime (a
  container on the host).
- **One OmniRoute.** `2-voice` owns the gateway; every
  platform points at it.
- **One identity, one secrets store.** Cerulean's Authentik and Cerulean Vault.
- **Atlas keeps CodeOps** — Gitea, Convex, CI, and the canonical git remote.
  Chef retires *as a builder*; Convex becomes a deploy target the plan may pick.
- **ONYX stays StorageOps** — it stores what the builder publishes; it does not
  become a builder and is not a second compute plane.

### 2.1 Why Studio and not Distro or Chef

The decision is not taste; it is the execution model.

| Question | Olympus | Distro (bolt.diy) | Atlas (Chef) |
|---|---|---|---|
| Can it run a server-side app? | **Yes** (container, SQLite, port) | No (WebContainer is client-side) | Only on Convex |
| Can it publish to a real name? | **Yes** (Cerulean + NPM, per-app vhost) | No | Static sites only (`chef-sites`) |
| Stack chosen by the request? | **Yes** (`project_plan.py`, confirmed before code) | Partly (template-driven) | Convex-shaped |
| Does it have a terminal UI? | **Yes** | No | No |
| Per-user keys / quotas? | No — **take this from Distro** | **Yes** (control plane) | No |
| Runs unattended builds? | **Yes** (systemd build runner, queue, cancel) | No | No |

Distro and Chef win on *interactivity and tenancy*; Olympus wins on *producing
a working artifact*. The plan keeps Olympus's runtime and **adopts Distro's
control plane** rather than discarding it — see §5.

---

## 3. Options considered

| Option | Shape | Verdict |
|---|---|---|
| **A — Studio-first** | Studio is the one web UI, the Olympus TUI the one terminal UI, the Olympus engine the one builder; Distro's control plane is absorbed; Chef retires as a builder; one OmniRoute | **Chosen** — the only shape where all four "one"s hold *and* output is a running app |
| **B — One engine, three thin clients** | Extract the builder into a service; Studio, the TUI and the bolt.diy shell all call it | Deferred — "one" holds at the engine layer only; three web surfaces remain, and Bolt/WebContainer still cannot publish |
| **C — Distro-first** | Distro is the web UI *and* the builder; Olympus shrinks to the factory; the TUI moves into Distro | Rejected — WebContainer cannot produce a host process, so Distro would end up embedding Olympus's runner anyway; the builder is Olympus's either way |

Option B is the fallback if the bolt.diy forks must be kept running. Nothing in
§4–§6 depends on which is chosen except the front-door retirement in §4.3.

---

## 4. Change list — one OmniRoute

**Owner of the gateway: `2-voice` (Server 2,
`10.10.2.1`, Consul service `omniroute`).** Everything below removes a second
copy and points at that one.

**The gateway's address is `http://10.10.2.1:20128/v1` — one port, not three.**
OmniRoute serves its OpenAI-compatible API and its dashboard on 20128, which is
what the live container publishes and what `GET :20128/v1/models` answers on.
The `20129` (API) and `20132` (live WS) ports that the entries below name were
Distro's *own* deleted gateway's layout; carrying them onto the shared gateway
pointed every consumer at a port nothing listens on. Corrected in §4.1, §4.2 and
§4.4 — verified against the running container, not inferred.

### 4.1 `distro` — drop the `local-gateway` profile — **applied**

`docker-compose.yml` carried `gateway` + `redis` behind
`profiles: ['local-gateway']`. Removed, and with them:

- [x] services `gateway`, `redis`; volumes `gateway-data`, `redis-data`
- [x] the `mem_limit: 4g` / `NODE_OPTIONS` gateway tuning block, and the
      `GATEWAY_MAX_OLD_SPACE_MB` / `LIVE_WS_ALLOWED_ORIGINS` knobs
- [x] `.env.example`: `JWT_SECRET`, `API_KEY_SECRET`, `OMNIROUTE_IMAGE_TAG`,
      `GATEWAY_BIND_HOST`, `GATEWAY_MAX_OLD_SPACE_MB`, and the whole "LOCAL
      gateway fallback" section. **`INITIAL_PASSWORD` stays**, re-documented as
      the *shared* gateway's dashboard password — the control plane still mints
      per-user keys through that API
- [x] `docker-compose.yml` header comment (it documented the profile)
- [x] the `web` service's `OPENAI_LIKE_API_BASE_URL` default, which pointed at
      the deleted `http://gateway:20129/v1` — now the mesh gateway
- [x] `control-plane`: the `gateway-data:/gateway-data:ro` mount only existed to
      read the local gateway's SQLite ledger. With a remote gateway the M4 sync
      needs a *mounted* data dir, so `CONTROL_SYNC_INTERVAL_MS=0` is right, and
      `docs/gateway-api-inventory.md` documents the path
- [x] every other consumer of the profile: the `gateway-up` Make target,
      `scripts/bootstrap.sh` (it *started* the profile — it now fails with
      instructions instead of self-healing), `scripts/healthcheck-gateway.sh`,
      `scripts/backup.sh` (it ran `docker compose exec gateway`), and
      `docs/ops.md` · `docs/architecture.md` · `docs/upstream.md` ·
      `docs/stack.md` · `README.md` · `THIRD_PARTY_NOTICES.md` ·
      `apps/control-plane/README.md` and its `docs/`
- [x] kept `OPENAI_LIKE_API_KEY`, `OPENAI_LIKE_API_BASE_URL`, `GATEWAY_API_URL`,
      `GATEWAY_DASHBOARD_URL`, `CONTROL_CONSUL_URL` — those *are* the remote
      wiring, and `DISTRO_GATEWAY_ONLY=true` already forbids direct upstream
      providers

### 4.2 `5-dev` — the same removal, one layer up — **applied**

- [x] deleted `distro-gateway`, `distro-redis`; volumes `distro-gateway-data`,
      `distro-redis-data`
- [x] `distro-control-plane`: dropped the `distro-gateway-data:/gateway-data:ro`
      mount, repointed `GATEWAY_DASHBOARD_URL` at the mesh
      (`http://${MESH_GATEWAY_HOST:-10.10.2.1}:20128`), and added
      `CONTROL_CONSUL_URL` so the same service works with discovery instead of a
      pin
- [x] `distro-web`: `OPENAI_LIKE_API_BASE_URL` → the mesh gateway
      (`http://10.10.2.1:20128/v1`) instead of `http://distro-gateway:20129/v1`,
      and its `depends_on` no longer waits on a service that does not exist
- [x] `consul-reg-g5`: dropped the `distro-gateway|…|20128|llm,ai,openai,distro`
      registration — Group 5 no longer runs a model plane
- [x] header block: RAM/ports line (removed `20128`, `20129`, `20132`) and a
      note that Group 5 consumes Group 2's gateway
- [x] root `.env.example` ("Distro (Group 5)"): dropped `DISTRO_GATEWAY_*`,
      added `MESH_GATEWAY_HOST` / `MESH_CONSUL_ADDR`
- [x] `docs/service-audit.md`: OmniRoute's verdict moves to **CONSOLIDATE —
      done**, distro's Redis row loses distro, and the memory table drops the
      removed 4 g gateway cap
- [x] the last two `20129` leftovers the port correction missed: `5-dev`'s
      `distro-web` default (`OPENAI_LIKE_API_BASE_URL`) and
      `distro/apps/web/.env.example`'s two example URLs both still named the
      deleted local gateway's API port, so a deployment that took the default
      was pointed at a port nothing listens on

### 4.3 `atlas` — Chef model calls route through the gateway

> **Superseded in Phase 3.** Chef is retired as a builder (§8), so the
> per-provider shims this section leaves open will not land — there is no Chef to
> route. The OpenAI case below and the placeholder-key note stay as the record of
> what was applied before the retirement; Convex, which the shims would have
> served, remains as the plan-selectable target.

Atlas runs no gateway, which is correct — but the `chef` service receives
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `XAI_API_KEY`
directly, so upstream provider keys live in two places for one platform.

- [x] Chef's provider config points at OmniRoute's OpenAI-compatible endpoint
      with a gateway key (`CHEF_OMNIROUTE_BASE_URL` + `CHEF_OMNIROUTE_API_KEY`)
      — the fork's OpenAI case now reads
      `baseURL: getEnv('CHEF_OMNIROUTE_BASE_URL') || undefined`; an unset value
      is upstream behavior, so a checkout with no gateway is unchanged. The
      Anthropic/Google/XAI cases still build vendor clients: a shim per provider
      is the remaining half, and until it lands a `claude-*`/`gemini-*` choice
      reaches the vendor directly (docs/Integrations.md)
- [x] keep **one** placeholder provider key: Chef's `depscheck` fails without
      one, and `docs/chef-auth-fork.md` records that constraint. Marked as a
      build-system placeholder, not a live credential
- [x] `.env.example`: the direct provider keys moved under a "PLACEHOLDERS
      ONLY" comment; the OmniRoute pair is above them
- [x] if Option C was ever revisited, this is the item that would make it work
      — as written, Chef needs the gateway for any non-Convex model

### 4.4 `olympus` — retire the `gateway` profile

Olympus's own gateway exists for a documented reason (the container it used to
depend on belonged to a project whose compose file had vanished), but it is
still a second gateway.

- [x] point `OMNIROUTE_BASE_URL` at the shared gateway — default
      `http://${MESH_GATEWAY_HOST:-10.10.2.1}:20128/v1` in `.env.example`, in
      both compose services and in `setup.sh`; the loopback case is kept
      documented, and `compose.host-gateway.yml` with it, for the deployment
      where the shared gateway is published on *this* host's loopback
- [x] remove the `omniroute` service (profile `gateway`) and the
      `omniroute-data` volume — Olympus's compose now renders olympus +
      studio + autoheal (+ sites under its profile), with no gateway service
      and no gateway volume. The provider connections were moved first: see the runbook below
- [x] `make gateway-vault-backup` / `-check` / `-restore` stay in this repo as
      the *dashboard's* tooling but are documented as running on the gateway's
      host, and the script's default container is now the gateway's own
      `omniroute` — it asks Docker for that container's own mount, so the volume
      it copies is the gateway's, not this checkout's. (`g2-omniroute`, the name
      this said when it was written, is not a container on any host: a group
      compose is generated from the member repos, so it carries their
      `container_name` and not a group prefix. Fixed 2026-09-18 — see
      `docs/stack-migration-gaps.md`.)
- [x] kept `compose.gateway-sso.yml`, `scripts/gateway-auth-mode.py` (container
      default `omniroute`) and `gateway-edge-check.py`: the *dashboard* is
      still one surface needing one gate, wherever the gateway runs
- [x] the `localhost:20128` caveats are kept (`docker-compose.yml` header,
      `.env.example`, `docs/stack.md`, `compose.host-gateway.yml`) because they
      are why this was a migration and not a one-line change

**Runbook — the live half, on the host that ran the old gateway.** The code
side is done; these steps are deployment state and cannot be done from a
checkout. In order:

1. Move the provider connections into the shared gateway
   (`scripts/omniroute-restore-providers.py`, with `--container omniroute` or
   against the mesh URL) and confirm with `make build-model-check` from the
   Olympus checkout. Nothing is removed until that check is green.
2. Put the old gateway's state in Cerulean Vault —
   `make gateway-vault-backup` on the gateway's host — so a rebuilt gateway is
   possible at all. `server.env` and the provider export are two halves of one
   thing; see `docs/stack.md`.
3. `docker compose --profile gateway rm -sf omniroute` is no longer possible
   (the service is gone), so stop the container directly and then
   `docker volume rm olympus_omniroute-data` **only after** step 2 has been read
   back. 
4. Bring Studio up on the mesh URL (`make docker-studio-up` only where the
   loopback case applies) and confirm `make build-model-check` still passes.

Where the volume and its backup *live* long-term is §10.4: the backup belongs
with whichever group owns the gateway, and until that is decided the targets stay
in Olympus pointed at Group 2's container.

### 4.5 `onyx` — consume the AI plane instead of being one — **applied**

- [x] `onyx-ai` (Storage Advisor / Backup Intelligence) gets an
      `OMNIROUTE_BASE_URL` + key path, keeping local-first behavior as the
      fallback — ONYX never ships provider keys. `services/ai/advisor.go`: the
      gateway is the plane, `AI_PROVIDER`/`AI_API_KEY`/`AI_MODEL` stay as the
      documented BYO-key hook, and `s.narrate` computes the local sentence first
      and only replaces it when the model answers — an unreachable, slow or
      empty model (or an empty answer) falls back, never errors. `OMNIROUTE_API_KEY`
      resolves through `services/vault` at boot (the `onyx-objectstore` pattern),
      so `.env` may carry a `vault://` reference; a reference that cannot be
      resolved, including when no store is configured at all, **stops the boot**
      rather than being sent to the gateway as a literal bearer token. Only the
      findings already computed leave the box. Tests: `services/ai/advisor_test.go`
      (plain/refused/unreachable/empty gateway, BYO fallback, key never in an error)
- [x] no gateway service is added to ONYX; StorageOps does not own inference

### 4.6 End state

- one OmniRoute deployment, owned by Group 2, with one provider pool
- one build model chain configured once (see Olympus `docs/build-model.md`)
- no repo other than Group 2 pulls `diegosouzapw/omniroute`

---

## 5. Distro's control plane → Studio's tenancy layer

Studio today is single-tenant in the ways that cost money: it holds **one**
gateway key in `.env`, resolves it server-side, and has no per-identity
attribution or quota. Distro's control plane already solved exactly that, and
its own README calls the design out: OmniRoute accounts per **API key**, so
mapping `user ↔ gateway_key` gives attribution and enforcement without forking
the gateway.

### 5.1 What each side has

| Capability | Distro control plane | Studio today | Converged |
|---|---|---|---|
| Accounts | `users` + scrypt passwords (`src/passwords.js`) | — | control plane |
| SSO | OIDC via Authentik (`src/oidc.js`) | OIDC PKCE + id_token verify (`lib/auth.ts`) | control plane issues the session; Studio verifies as today |
| Session | bearer token, `sessions` table | signed cookie (`STUDIO_SESSION_SECRET`) | control-plane token, Studio cookie as the edge session |
| **Per-user gateway key** | `GET /api/me/gateway-key`, minted/rotated via the gateway dashboard API | one static `OMNIROUTE_API_KEY` | **per user** |
| **Quota gate** | `GET /api/internal/quota-check` + `POST /api/internal/usage-report` | — | **called around every `/api/plan` and `/api/generate`** |
| Usage ledger | `usage_cache` synced from the gateway's `usage_history` (`api_key_id`) | — | dashboard-API sync (no volume with a remote gateway) |
| Admin console | `GET /admin` (`src/admin.html`, no build step) | — | same, extended with the build queue |
| Audit log | `audit_log` + `GET /api/admin/audit` | — | plus build/publish/export events |
| Alerts | webhook on quota / sync failure | — | unchanged |
| Backups | `make backup` (online SQLite `.backup()`) | — | unchanged |
| Saved apps | — | per OIDC subject on `studio-data` | per **control-plane user id** |

### 5.2 Migration checklist

- [x] **Identity key.** Studio's library keys on the OIDC `sub`. Re-key
      `lib/projects.ts` on the control-plane user id, and store the `sub` beside
      it so existing libraries can be re-homed without renames. Applied as
      `lib/identities.ts`: the namespace is resolved in one place (all nine
      saved-app routes call `libraryNamespace(gate)`), the mapping lives in
      `$STUDIO_DATA_DIR/identities.json` (user id → sub, email, directory), and a
      library already sitting in the subject's directory is **adopted** in place,
      so switching tenancy on cannot orphan it. With no control plane configured
      the answer is still `namespaceFor(sub)` — the single-operator behaviour.
- [x] **Key resolution.** Studio's `/api/plan` and `/api/generate` stop reading
      `OMNIROUTE_API_KEY` from the environment and read the caller's key from the
      control plane. Applied as `lib/tenancy.ts` (`beginTurn`) on top of
      `lib/controlplane.ts`; both routes now call it before dispatch. The key
      stays server-side — it is put into the gateway config for the turn and
      never returned to the client (pinned by a test on the refusal body).
      Strict, not fail-open: with a plane configured, no turn falls back to the
      shared key, because that would move one user's spend onto the operator's.
- [x] **Quota around the turn.** `quota-check` before dispatch; `usage-report`
      after. Applied. Token counts come from the gateway's own tail: the plan route
      reads `usage` out of the completion body, and the generate route reads it out
      of the SSE tail (`sseToTextStream({ onUsage })`) and reports when the stream
      ends (`withStreamEnd`). A gateway that sends no usage still gets the request
      recorded, and the quota check is deliberately fail-open — the gateway key's
      own hard cap is the backstop, the same posture Distro's web app takes.
- [x] **Admin surface.** Point operators at the control plane's `/admin`; add a
      read-only build-queue view there so Studio's queue is visible where users
      and quotas already are. Applied as `src/buildQueue.js` + `GET /api/admin/build-queue`
      + a panel in `src/admin.html`: the control plane reads the directory in
      `BUILD_QUEUE_DIR` (Studio's `STUDIO_BUILD_QUEUE_DIR`, mounted `:ro`) and
      renders each job's id, slug, title, state, model and timestamps. Read-only by
      construction — nothing on that path writes, cancels or reorders a build, so
      the runner is still the only thing that touches the queue; an unset or
      unreadable directory renders an empty list with the reason, never an error
      page. Studio's own `make build-runner-list` remains the terminal answer.
- [x] **Audit.** Emit control-plane audit rows for build, publish and
      "export to factory" — those are the three actions that touch a public
      name or the repo. Applied: `POST /api/internal/audit` (service token),
      emitted from the build route (`build.start` / `build.publish`) and the
      export route (`build.export`). Best-effort — an audit write that fails must
      not fail a publish the user already made.
- [x] **Package it.** The control plane is plain Node with a SQLite file and no
      framework, and Studio reaches it over `CONTROL_PLANE_INTERNAL_URL` +
      `CONTROL_INTERNAL_TOKEN` (both wired through `docker-compose.yml` and
      `.env.example`; empty = single-operator). Two new routes were needed for
      it, both documented in the control plane's README:
      `POST /api/internal/identity` (subject → account + that account's key,
      provisioning on first sight, `users.oidc_sub` is the join, 409 on a
      conflicting email rather than a silent rebind) and `POST /api/internal/audit`.
      `CONTROL_INTERNAL_TOKEN` is generated by Distro's `bootstrap.sh`, and an unset
      token answers `503` on those routes rather than leaving them open.
      The account column is added by an idempotent `ALTER TABLE` in `src/db.js`,
      because `CREATE TABLE IF NOT EXISTS` never touches an existing database.
- [x] **Retire, don't fork.** The bolt.diy front door retires as a *surface*;
      its control plane does not. Applied: `apps/web` (the rebranded bolt.diy
      fork) is deleted — compose `web` service, GHCR web image, `VITE_*` /
      `WEB_BIND_HOST` / `DISTRO_GATEWAY_ONLY` / `DISTRO_CONTROL_PLANE` env
      blocks, `sync-upstream` tooling, the umbrella `distro-web` service and its
      Consul registration, and every doc surface rewritten for the tenancy
      service. The fork keeps its record: git history, `docs/upstream.md`,
      `THIRD_PARTY_NOTICES.md`, and the retained MIT license at
      `licenses/bolt.diy.LICENSE`. The control plane remains the repo's only
      deliverable, `docs/stack.md` narrowed to it — and its `/api/internal/*`
      service API is exactly what Studio consumes (identity → quota → turn →
      usage/audit), so nothing the builder needs was lost with the front door.

---

## 6. Change list — Cerulean Vault (SecretOps)

Reference implementation: **Olympus**. Contract to copy:

```
VAULT_ADDR / VAULT_TOKEN (or VAULT_TOKEN_FILE) / VAULT_PREFIX=cerulean / VAULT_PATH=<product>
vault://<mount>/<path>#<key>        # references resolved at startup
policy <product>  →  <prefix>/data/<product>* + metadata (read/list)
scripts/vault-bootstrap.py          # idempotent; refuses a KV v1 mount
scripts/vault-renew.sh              # periodic token renewal, --check for monitoring
```

### 6.1 Per repo

| Repo | Work |
|---|---|
| **ONYX** | **Done — legacy path retired.** `VAULT_*` in `.env.example` and in both Go services' compose env; `services/vault/` is the KV v2 client (`vault://<mount>/<path>#<key>` grammar, TLS options, `vault_test.go`) and `ResolveEnv` resolves every `vault://` reference, so `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `CERULEAN_API_TOKEN` move by editing `.env`; `compose.vault.yml` is the dev-mode Vault (Olympus's template); `GET /api/v1/status` reports `vault: ok \| not-configured \| error`. The `infisical://` half did not survive the migration: `compose.infisical.yml`, `scripts/infisical-setup.{sh,py}` and `services/infisical/` are deleted and `ResolveEnv` no longer reads the old form |
| **Atlas** | **Done.** `VAULT_*` landed; the `INFISICAL_*` block is replaced by a legacy note (Atlas ships no Infisical service — no profile, no `services/infisical/`); `GITEA_DB_PASSWORD`, `CONVEX_INSTANCE_SECRET` and `CHEF_SESSION_SECRET` are live `vault://` references seeded by a new `scripts/vault-bootstrap.py` (mirrors Olympus's, but unions the three keys instead of replacing one, and **never invents** an Authentik-issued secret); `OIDC_CLIENT_SECRET` / `CHEF_OIDC_CLIENT_SECRET` ship as a commented reference and are stored with `vault-migrate.py`; `scripts/vault-resolve.py` + a `setup.sh` step resolve every reference at setup (`make vault-sync` / `vault-check`), because Atlas is compose-and-images only, and an unresolvable reference fails setup rather than becoming an empty credential |
| **Distro** | **Done.** No Infisical to remove — this was greenfield. `VAULT_*` landed in `.env.example` and the shared migrator is mirrored in `scripts/`; any `vault://` value in `.env` is now resolved before a credential is read — the control plane at import (`apps/control-plane/src/vault.js` + `secrets.js`, which covers `OIDC_CLIENT_SECRET`, `MAGNATE_ENTITLEMENTS_TOKEN`, `INITIAL_PASSWORD` → `GATEWAY_ADMIN_PASSWORD`) and the web container while building its wrangler bindings (`apps/web/bindings.sh` → `apps/web/scripts/vault-resolve.mjs`, which covers `OPENAI_LIKE_API_KEY`); `compose.vault.yml` is the dev-mode Vault |
| **Olympus** | Done — nothing to change; it is the source of the pattern |

Each repo gets its own `<product>` policy on the Cerulean Vault host, minted
with `VAULT_PRODUCT_TOKENS=<product>` and handed over as
`data/vault/token/<product>.token`. Never the platform-wide `cerulean` policy,
and never the root token.

**The shared tool.** `scripts/vault-migrate.py` lives once in this repo and is
mirrored verbatim into `4-social/onyx/scripts/`, `5-dev/atlas/scripts/` and
`5-dev/distro/scripts/` — the same pattern as `stack-lib.sh` and `guard-lib`. It reads
the source (the Infisical v3 API, or a plain `.env` via `--from-env-file`),
writes **one** KV v2 secret at `<prefix>/<path>` containing the migrated keys,
and:

- reads the target first and **unions** the write, so keys already in Vault are
  preserved and a second run reports `already in sync` without writing
- skips empty values and anything already a `vault://` / `infisical://`
  reference — a reference is not a secret to copy
- refuses a KV **v1** mount, and creates the KV v2 mount when the token can see
  `sys/mounts`
- reads every write back and fails if a key did not land, because a write that
  silently did nothing is worse than one that errored
- prints **key names only**, never values

```bash
python3 scripts/vault-migrate.py --dry-run                          # what would move
python3 scripts/vault-migrate.py                                    # Infisical → Vault
python3 scripts/vault-migrate.py --from-env-file .env \
    --keys S3_ACCESS_KEY,S3_SECRET_KEY,CERULEAN_API_TOKEN           # seed from .env
```

`--dry-run` is verified against a fixture; the live round-trip is the same
read-back check Olympus's bootstrap uses, so it is exercised the first time a
real Vault is reachable.

### 6.2 Reconcile the canonical wording

`README.md` and `docs/standard.md` said **Infisical = Secrets** while the
platform's own Vault is what Olympus runs. The canonical definition is now:

> **Cerulean Vault = Secrets** — HashiCorp Vault, KV v2, hosted by Cerulean.
> Infisical is **retired**: the per-repo `compose.infisical.yml` profiles and
> `infisical://` resolvers are gone (`docs/service-audit.md` §1), and a leftover
> `infisical://` value is a deployment error, never a fallback.

(The first draft of this rule kept the profiles "profile-gated until each
repo's `vault://` path lands". Once every repo had landed its path they were
deleted rather than left dormant — a second secret store on disk is a second
credential store, whether or not it is running.)

Applied — every statement that *defines* SecretOps or describes the secret
flow/boundary:

- [x] `README.md` — the "Why" row, the golden-rule table row, the ecosystem
      diagram label, the tenant-of-record line, the Cerulean + NPM Edge
      "Consumes" lines, the owning section (retitled
      `Cerulean Vault — SecretOps`, with the legacy note), the deployment-order
      step, the dependency-graph line, the ownership matrix row, the secret flow
      (both bullets), the trust-flow parenthetical, the secret boundary, the
      tenant-isolation line, and the repositories table row
- [x] `docs/standard.md` — the §1 ownership table row, the §8 `.env.example`
      posture, and both golden-rules summaries (top and §12)
- [x] `docs/stack.md` — the "who owns what" line and the assembled-upstreams
      list
- [x] `web/landing/index.html` — the meta description, the lede, the golden-rules
      card, the foundation-layers card, the terminal block, and the SecretOps
      surface tile
- [x] `scripts/conform-project.sh` (and the root `scripts/` mirror) — the
      scaffold now emits `VAULT_*` / `VAULT_PATH=<product>`, the stack-doc
      template says Cerulean Vault, and the audit accepts either posture so a
      repo mid-migration still passes

**Wave 2 — the per-platform lines — applied.** The golden rule is now stated as
`Cerulean Vault = Secrets` everywhere it appears:

- [x] `README.md`: every per-platform "Consumes" row/bullet
      (`· Infisical ·` → `· Cerulean Vault ·`), the inter-platform integration
      prose, the deployment-order steps, and the dependency-graph diagram
- [x] the golden-rule line in `atheniq`, `oasis`, `rizzaura`, `onyx`,
      `capstone`, `monarch`, `magnate`, `signara` and `zeus` `docs/stack.md`
- [x] `onyx` and `atlas` `docs/stack.md`: the secrets *description* now names
      Cerulean Vault — the repos whose `.env.example` gained `VAULT_*` in §6.1.
      The legacy note in both was superseded by retirement: `onyx` deleted its
      profile, setup scripts and Go package, and `atlas` never shipped one
- [x] the remaining *rule* statements outside `docs/stack.md` — the `atheniq`,
      `atlas`, `magnate`, `onyx` and `npm` README "Secrets" rows and stack
      prose, their `docs/Architecture.md` / `docs/Integrations.md` /
      `docs/Deployment.md` / `docs/chef-auth-fork.md` lines, and the `atlas` +
      `atheniq` landing pages — now name Cerulean Vault, and no repo carries a
      `compose.infisical.yml`, an `INFISICAL_*` block or a `services/infisical/`
      any more (the labels this list originally called "legacy" are gone)
- [x] `convergence-capstone-zeus.md` now reads **Cerulean Vault = Secrets**
      throughout (its §2 diagram, §3 parity row, Phase 1 item and §5), matching
      the rule statements in `capstone` and `zeus` themselves; Zeus's resolver
      is `scripts/vault-env.mjs` and it **refuses** the retired `infisical://`
      form rather than reading it

The rule names the platform's SecretOps — Cerulean Vault. The legacy note that
used to justify keeping an `infisical://` resolver "as an implementation detail"
is gone with the resolver: the canonical docs name one store, and the migration
source is a script (`vault-migrate.py`), not a supported runtime path.

---

## 7. Repository conformity delta

Against `docs/standard.md`, for the four repos in scope. All four **pass**
`./scripts/conform-project.sh <repo>` today (the convergence link in §6.2 was
added to each `docs/stack.md`); the deltas below are the finer, §-level ones the
mechanical audit does not fail on:

| Repo | Gap |
|---|---|
| `distro` | closed by this change: `VAULT_*` + the migrator landed (§6.1), the `local-gateway` documentation is gone (§4.1), and the README's Quickstart now states that `.env` is gitignored and must never be committed or pasted into an issue |
| `atlas` | **closed.** `.env.example`, `docs/stack.md` (+ a new Secrets section), `docs/Architecture.md`, `docs/Deployment.md`, `docs/Integrations.md` and `docs/chef-auth-fork.md` name Cerulean Vault, the duplicated §Boundaries paragraph is gone, and the `vault://` machinery landed: `scripts/vault-bootstrap.py`, `scripts/vault-resolve.py`, the `setup.sh` step that runs them (`make vault-bootstrap` / `vault-sync` / `vault-check`), and the dead `INFISICAL_*` block is replaced by a legacy note — Atlas ships no Infisical service to configure. Deviation worth knowing: only the three keys Atlas *generates* are live `vault://` references; the two issued by Authentik ship as a commented reference, because a locally random value for a credential two systems must agree on is a wrong value that looks like a working one |
| `olympus` | conforms; the golden-rule line already says Cerulean Vault and the `VAULT_*` posture is the reference, and §4.4 is applied (the gateway service, its volume and its targets are gone). Studio's tenancy wiring is §5.2 |
| `onyx` | **closed.** The README's secrets line names Cerulean Vault, `docs/stack.md` gained the service map + "In the ecosystem" table, the README's Quickstart now states that `.env` is gitignored and must never be committed or pasted into an issue, and the repo's Infisical half — `compose.infisical.yml`, `scripts/infisical-setup.{sh,py}`, `services/infisical/` — is deleted |

---

## 8. Phased plan

**Phase 0 — decisions (this doc)**
- [x] Options A/B/C written, A chosen, with the execution-model comparison
- [x] The one-gateway target named (Group 2) and the removals listed

**Phase 1 — one AI plane (no surface changes)**
- [x] Distro: drop `local-gateway` (§4.1)
- [x] Group 5: drop `distro-gateway` / `distro-redis` (§4.2)
- [x] Atlas: Chef through OmniRoute (§4.3) — the OpenAI case reads
      `CHEF_OMNIROUTE_BASE_URL`; the per-provider shims and a live `depscheck` run
      need a provider key and remain
- [x] Olympus: retire the `gateway` profile (§4.4) — service, volume, the
      `gateway-up` / `gateway-down` targets and every doc reference are gone; the
      live provider move is the runbook in §4.4 (deployment state, not code)
- [ ] `make gateway-check` / `build-model-check` green from all four — needs the
      live gateway, so it is the first thing to run after the §4.4 runbook

**Phase 2 — one identity + one secrets store**
- [x] The shared migrator + `VAULT_*` in `.env.example` for ONYX, Atlas, Distro (§6.1)
- [x] Canonical wording reconciled in README / standard / stack / landing /
      conform-project, and wave 2's per-platform lines flipped (§6.2)
- [x] ONYX/Distro: `vault://` resolution in the services themselves (§6.1) —
      `services/vault/` (Go) and the two Node resolvers; a reference that cannot
      be resolved is a hard failure, never an empty credential
- [x] Atlas: `vault://` resolution at setup (§6.1) — Atlas is compose-and-images
      only, so its references resolve in `setup.sh` rather than in a service;
      `scripts/vault-bootstrap.py` + `scripts/vault-resolve.py` are mirrored from
      Olympus and the setup step runs both
- [x] All four sign in through Cerulean Authentik; Distro's control-plane
      accounts become SSO accounts. Applied and **enforced server-side, not by
      hiding a form**: Distro's `/api/auth/signup` and `/api/auth/login` answer
      403 unless `BREAKGLASS_LOGIN=1` (`src/oidc.js` `localLoginEnabled()`,
      `src/http.js`), the admin console offers Authentik only, and the missing
      **`distro` application + provider were provisioned in Authentik** — they
      did not exist, so Distro had no SSO to switch to. Olympus Studio was
      brought up against the `studio` app and this control plane
      (`CONTROL_PLANE_INTERNAL_URL` + `CONTROL_INTERNAL_TOKEN`); ONYX and Atlas
      consume the same Authentik. The upstream apps that cannot do OIDC (n8n,
      Grist, SigNoz, OmniRoute, FreePBX) are fronted by **domain-level Authentik
      forward-auth providers**, one per zone, all attached to the embedded
      outpost and verified to 401 every protected host; the `atlas-chef`
      application is gone; and Magnate's Authentik-only login was proven
      **end-to-end** through the real PKCE flow over the public hostnames. The
      full inventory of every login on the host, the break-glass convention, and
      the live edge state is in [`sign-in-posture.md`](sign-in-posture.md): the
      forward-auth gates now cover **all five zones** — `pbx.zeus.innotel.us` and
      `secrets.cerulean.innotel.us` were the last two ungated hosts and both now
      302 into their zone's outpost — and the snippet is rendered by the Zeus and
      Cerulean provisioners as well as Capstone's, so a re-run re-applies the
      gate instead of wiping it. Dograh was **verified** OIDC-only rather than
      assumed (its client secret matches provider 28 and `/api/v1/auth/oidc/login`
      starts a real PKCE authorize); Magnate's Authentik **admin API** was found
      dead and repaired (`AUTHENTIK_BASE_URL` was never set and its bootstrap
      token was expired, so the Stripe webhook that provisions accounts had been
      silently skipping); and that login flow is now a committed regression test
      (`scripts/verify-sso.py`, `npm run verify:sso`) which asserts the password
      routes stay closed. Its §5 records the host reconcile: every compose
      project now runs under the name its canonical file pins —
      `capstone-voice-aiagent-platform`, `cerulean-dns-platform`,
      `monarch-media-platform` and `zeus-pbx-platform` were renamed to
      `capstone`, `cerulean`, `monarch` and `zeus` with their named and anonymous
      volumes carried across, so a plain `docker compose up -d` from a canonical
      directory no longer creates fresh empty volumes; the 18 pre-migration
      volumes were then deleted once every canonical counterpart was confirmed to
      hold the data.
      Two findings worth keeping: Cerulean's `scripts/authentik-setup.py` could
      never provision a **new** application (Authentik's `/core/applications/`
      ignores the `slug=` filter, so it always took the update branch and 404'd),
      and Magnate's compose never passed `AUTHENTIK_*` into the container, so the
      admin panel's OIDC was configured in `.env` and dead in the process.
- [x] **Infisical retired outright**, the step §6 originally left open. No repo
      in the stack ships a `compose.infisical.yml`, an `INFISICAL_*` block, an
      `infisical-setup.{sh,py}` or a `services/infisical/` package any more; the
      `conform-project.sh` audit accepts the `vault://` posture only, and the
      instance at `secrets.cerulean.innotel.us` survives as a **migration
      source** for `scripts/vault-migrate.py` (see `sign-in-posture.md` §2).
      Two host-local `.env` files still carry the old `INFISICAL_*` block with no
      `VAULT_*` counterpart — the Zeus portal and Atlas, 11 and 4 keys, both
      running on plaintext values today — so the operator's `vault-migrate.py` +
      `VAULT_*` pass has to land there before the block can be deleted. That is
      deployment state, not repo state, and it is the only half of §6 still
      open.

**Phase 3 — one builder, one web UI, one terminal UI**
- [x] Studio consumes the control plane (§5.2): per-user keys, quota, audit, and
      the read-only build-queue view in the control plane's own `/admin`
- [x] Chef retires as a builder; Convex becomes a plan-selectable target.
      Atlas: the `chef` service, `chef-provisioner`, `chef-sites` and the profile
      are gone from `docker-compose.yml` with the `chef-up` / `chef-down` targets,
      the `CHEF_*` env block is a retirement note, `setup.sh` no longer clones an
      upstream checkout, CI no longer syntax-checks one, and Gitea + Convex are what
      remains. `chef-provisioner/` and `docs/chef-auth-fork.md` stay as the record.
      Olympus: a plan now carries a **target** (`lib/targets.ts`, `lib/plan.ts`) —
      `container` by default, `convex` when the planner chooses the self-hosted
      Convex Atlas runs — and the whole chain honours it: the generation contract
      builds Convex-shaped code that reads `CONVEX_URL` instead of inventing a
      hardcoded URL or a database of its own, the export spec names the target, and
      `scripts/package-project.py` points the client at the deployment under
      `CONVEX_URL` / `VITE_CONVEX_URL` / `NEXT_PUBLIC_CONVEX_URL`, passing a
      deployment-scoped `CONVEX_DEPLOY_KEY` as a build argument (never an `ENV`).
      A Convex plan with no `CONVEX_URL` is refused rather than packaged, a static
      site may not target it, and `build-runner.py` carries those two variables by
      exact name so the backend's admin key stays out of every build. What is still
      open is the *live* half: deploying the functions needs a deployment key minted
      on the Atlas Convex (`docs/site-publishing.md` carries the three consequences)
- [x] Distro's bolt.diy front door retires; the control plane survives as the
      tenancy service (§5.2 — `apps/web` deleted, control plane remains the
      repo's deliverable, `:5173` gone from every compose and doc)
- [ ] Studio gains the Distro-only affordances worth keeping: the file tree and
      a real terminal pane, pointed at the host runtime rather than a
      WebContainer

**Phase 4 — ONYX as the storage answer**
- [ ] Studio's published `sites` tree and app data archive to ONYX (the
      integration `olympus/docs/stack.md` currently holds under an explicit
      "on hold" heading)
- [x] `onyx-ai` consumes the AI plane (Gateway first, BYO hook, local fallback)

---

## 9. What explicitly does NOT move

- **Identity** stays Authentik inside Cerulean — all four consume, none owns.
- **Trust / DNS / TLS** stays Cerulean + NPM Edge.
- **Revenue** stays Magnate; Distro's entitlement checks stay as they are.
- **Storage** stays ONYX. It is not a builder and not a compute plane.
- **CodeOps stays Atlas** — Gitea, PRs, CI, package registry, and the canonical
  git remote for every other platform. Retiring Chef retires a *builder*, not
  Atlas's reason to exist.
- **The factory stays Olympus** — issue → validated PR, harness, protected
  paths. Studio and the TUI are the *build* surface; they do not absorb the
  factory's governance, and the harness is still never edited to make a check
  pass.

## 10. Open questions for the owner

1. **Does Distro survive as a repo? — RESOLVED: yes, as the tenancy service.**
   The front door retired (§5.2) and the control plane stayed, keeping a
   BuilderOps-labeled repo whose whole surface is tenancy. Revisit only if
   ownership of the control plane should follow the web UI into Olympus.
2. **Studio's in-browser execution.** Distro's genuine advantage is that the
   agent runs *in the browser* with no host process. Does that matter for
   untrusted output, or is the host sandbox (Codex bubblewrap, documented in
   `olympus/docs/stack.md`) sufficient?
3. **Chef's Convex target — RESOLVED: it stays, as a plan-selectable target.**
   Convex is a genuine product (a realtime DB with functions) that the container
   runtime does not replace, so it survives Chef. What retires is Chef *as a
   builder*, and the target is now the plan's: the planner may answer `convex`,
   and packaging, running and publishing are unchanged — the container hosts the
   client and Convex holds the schema and the functions (§8 Phase 3). The
   alternative — dropping Convex with Chef and standardizing on container +
   SQLite — was rejected because it would delete a working backend to simplify a
   plan field.
4. **Gateway ownership — RESOLVED: it stays in `2-voice` (Group 2).** The
   alternative was to move it beside the builder in Group 5. It stays, because
   it is there today (the live `omniroute` container runs on the same host as
   Zeus's FreePBX), `docs/Architecture.md` already places OmniRoute at
   `10.10.2.1:20128`, and §4 names Group 2 owner — whereas moving it would mean
   migrating the provider credentials *and* the `omniroute-data` volume
   (deployment state; see §4.4's runbook) and repointing every consumer, for no
   functional gain. Since the gateway is single, every other repo consumes it
   instead of running one: `atheniq` was still advertising `make gateway-up` for
   a service that had already been deleted from its compose, and that target -
   plus the docs, CI step and landing snippet that repeated it — is now gone in
   favour of `OMNIROUTE_BASE_URL=http://10.10.2.1:20128/v1`.
