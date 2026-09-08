<div align="center">

[![CI](https://github.com/innotelinc/innotel-platform-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/innotelinc/innotel-platform-stack/actions/workflows/ci.yml)
[![Conformity](https://github.com/innotelinc/innotel-platform-stack/actions/workflows/conform.yml/badge.svg)](https://github.com/innotelinc/innotel-platform-stack/actions/workflows/conform.yml)
[![Release](https://github.com/innotelinc/innotel-platform-stack/actions/workflows/release.yml/badge.svg)](https://github.com/innotelinc/innotel-platform-stack/actions/workflows/release.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-brightgreen.svg)](LICENSE)

# 🏛️ Innotel Platform Stack

**INNOTEL V1 Enterprise Architecture Bundle — the canonical single-responsibility platform ecosystem.**

One stack. Every platform owns exactly one job. Identity, secrets, trust, storage, revenue,
and edge are platform services; everything else is a business function that consumes them.

</div>

---

## Why

| Problem | Innotel Platform Stack answer |
| --- | --- |
| Fragmented self-hosted platforms with no canonical integration | One stack, one owns/consumes map, every platform documents its role in `docs/stack.md` |
| Identity/session sprawl across platforms | Cerulean Authentik is the single identity source; disable a user and they lose every platform |
| Secret drift across `.env` files | Infisical is the only secrets store; `.env` is derived and gitignored |
| TLS/PKI managed per-platform | Cerulean issues certs and DNS; NPM Edge fronts public hosts only |
| Revenue fragmentation | Magnate is the single billing plane; paid seats flow everywhere |
| No release discipline | Every product repo has a `release.yml` that tags, builds GHCR images, and cuts a GitHub Release with artifacts |
| Attribution sprawl | The attribution guard runs locally + in CI; only Darnel Hunter <dhunter@innotel.us> is credited |
| Docs/standard drift across repos | A single conformity standard (`docs/standard.md`) + `scripts/conform-project.sh` audit every product repo |

> **About this repo** — the source of truth for the Innotel Platform Stack: who owns what,
> who consumes whom, how the platforms integrate, the order they deploy in, and the
> security boundaries between them. Every product repository carries a
> [docs/stack.md](docs/stack.md)-style section that points back here, so the ecosystem
> definition lives in exactly one place. **Landing page:** [innotelinc.github.io/innotel-platform-stack](https://innotelinc.github.io/innotel-platform-stack)

---

## Executive summary

The Innotel ecosystem is a portfolio of self-hostable platforms that follow strict
**single-responsibility** principles: every platform owns one primary domain of
responsibility, and no platform duplicates another's. Six of them are **platform
services** — horizontal capabilities everything else consumes:

| Golden rule | Platform | Classification |
|---|---|---|
| Authentik = Identity | Authentik | IdentityOps |
| Infisical = Secrets | Infisical | SecretOps |
| Cerulean = Trust | Cerulean | TrustOps |
| ONYX = Storage | ONYX | StorageOps |
| Magnate = Billing Platform | Magnate | RevenueOps |
| NPM Edge = Edge | NPM Edge | EdgeOps |

The remaining platforms are **business functions** built on top:

| Platform | Classification | Consumes |
|---|---|---|
| Monarch | MediaOps | Authentik · Infisical · ONYX · Magnate · Cerulean · NPM Edge |
| Zeus | VoiceOps | Authentik · Infisical · Magnate · Cerulean · NPM Edge |
| Oasis | MailOps | Authentik · Infisical · Cerulean · Magnate · NPM Edge |
| Signara | DocumentOps | Authentik · Cerulean · Infisical · ONYX · Magnate · NPM Edge |
| Capstone | AgentOps | Zeus · Authentik · Infisical · Magnate · NPM Edge |
| Rizz Aura | CommunityOps | Authentik · Magnate · NPM Edge |
| zapit | TransferOps | Authentik (optional) |
| AthenIQ | LearningOps | Authentik · Infisical · Cerulean · ONYX · Magnate · Signara · NPM Edge |
| Atlas | CodeOps | Authentik · Infisical · Cerulean · Magnate · NPM Edge |
| Distro | BuilderOps | OmniRoute · NPM Edge · Infisical · Authentik · Magnate · Cerulean |
| PLUTUS | VideoOps | OmniRoute · NPM Edge · Infisical · Authentik · Cerulean |

## Documents

- [**Capstone ↔ Zeus convergence**](docs/convergence-capstone-zeus.md) — the
  deep look at AgentOps/VoiceOps: Capstone standalone, Zeus as the VoIP
  platform, Capstone as a Zeus add-on.
- [**Service & package audit**](docs/service-audit.md) — the duplication
  audit across every stack: which services/packages appear in more than one
  repo, which duplicates are required by that stack's shape, and the memory
  posture.

## Central shared library — `scripts/stack-lib.sh`

Common operator tasks live **once**, here: env resolution, LAN-IP / NPM
forward-host detection, NPM API helpers, and output helpers. Every platform
repo carries a verbatim mirror at `scripts/stack-lib.sh` (synced by
`scripts/sync-stack-lib.sh` — the copy in this repo is the source of truth)
and sources it from its own scripts:

```bash
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/stack-lib.sh"
upstream="$(stack_lib_forward_host)"        # explicit env → LAN IP → fallback
token="$(stack_lib_npm_login "$NPM_API_URL" "$email" "$password")"
```

Conventions encoded in the library (enforced portfolio-wide):

- **LAN IPs only for NPM upstreams** — `stack_lib_forward_host` prefers an
  explicit `NPM_FORWARD_HOST`/`NPM_UPSTREAM_HOST`, then auto-detects the
  host's primary LAN IPv4 (default-route source address); docker bridge
  (172.x) and loopback addresses are never used because a remote NPM edge
  cannot resolve them. `host.docker.internal` is a last-resort fallback for
  single-box installs.
- **One wildcard cert per platform zone** — `*.<platform>.innotel.us` via
  DNS-01 against the shared BIND, attached to every proxy host in the zone.
- **Idempotent provisioning** — every script GETs before it writes and only
  updates when state differs.

## Architecture principles

1. **One job per platform.** Identity, secrets, trust, storage, and revenue are each owned
   by exactly one platform — never re-implemented in a business platform.
2. **Consume, don't embed.** A business platform integrates with the platform services it
   needs; it never ships a second copy of identity, secrets, billing, or storage.
3. **Self-hosted first.** Every platform runs on your own hardware (bare metal, VPS, or
   homelab) with no cloud dependency for the core function.
4. **Single tenant of record.** Users, groups, roles, and permissions live in Authentik;
   subscriptions and entitlements live in Magnate; credentials live in Infisical.
5. **Opt-in integration.** Platform services are provisioned additively — a stack runs
   without them, and integration scripts are idempotent.
6. **No third-party attribution.** All repositories enforce the attribution guard: only
   the project owner may be credited in commits, footers, or headers.

## Ecosystem diagram

```
                           USERS
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                        AUTHENTIK                            │
│                       IdentityOps                           │
├─────────────────────────────────────────────────────────────┤
│ OIDC • OAuth2 • SAML • MFA • RBAC • SCIM • SSO            │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                        INFISICAL                            │
│                        SecretOps                            │
├─────────────────────────────────────────────────────────────┤
│ Secrets • Keys • API Tokens • Credentials • PKI Keys      │
└─────────────────────────────────────────────────────────────┘
                             │
     ┌───────────────────────┼───────────────────────┐
     │                       │                       │
     ▼                       ▼                       ▼
┌──────────────┐    ┌──────────────┐    ┌─────────────────┐
│   CERULEAN   │    │    ONYX      │    │    MAGNATE      │
│   TrustOps   │    │  StorageOps  │    │   RevenueOps    │
├──────────────┤    ├──────────────┤    ├─────────────────┤
│ DNS          │    │ Object Store │    │ Billing         │
│ ACME         │    │ Backups      │    │ Plans           │
│ PKI          │    │ Snapshots    │    │ Subscriptions   │
│ Trust Score  │    │ Files        │    │ Invoices        │
│ Certificates │    │ Media Assets │    │ Entitlements    │
└──────────────┘    └──────────────┘    └─────────────────┘
     │                       │                       │
     └───────────────┬───────┴───────────────┬───────┘
                     │                       │
                     ▼                       ▼
       ┌──────────────────────────────────────────┐
       │        NPM EDGE — EdgeOps (edge)         │
       └──────────────────────────────────────────┘
                             │
                             ▼
       ┌──────────────────────────────────────────┐
       │            BUSINESS PLATFORMS            │
       └──────────────────────────────────────────┘

    ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
    │ Monarch │ │  Zeus   │ │  Oasis  │ │ Signara │
    │ MediaOp │ │ VoiceOp │ │ MailOp  │ │ DocOps  │
    └─────────┘ └─────────┘ └─────────┘ └─────────┘
           │          │          │           │
           └──────────┼──────────┼───────────┘
                      │
                      ▼
              ┌─────────────┐
              │  Capstone   │
              │  AgentOps   │
              └─────────────┘

              Rizz Aura (CommunityOps) rides on Authentik + Magnate
              zapit (TransferOps) rides on Authentik (optional) — files
              never touch storage: transfer is ephemeral, ONYX stays StorageOps
              AthenIQ (LearningOps) rides on Authentik · Infisical · Cerulean ·
              ONYX · Magnate and sends completion evidence to Signara for
              signed course certificates
              Atlas (CodeOps) holds every repo + the AI app builder; it rides
              Authentik · Infisical · Cerulean · Magnate and never stores
              application data for other platforms
```

## Multi-Server Deployment

The stack runs across 5 independent servers connected by a WireGuard mesh.
Each group runs on its own server and discovers others via Consul.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    WireGuard Mesh (10.10.0.0/16)                        │
│                                                                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
│  │  Server 1  │  │  Server 2  │  │  Server 3  │  │  Server 4  │  │  Server 5  │
│  │  10.10.1.1 │  │  10.10.2.1 │  │  10.10.3.1 │  │  10.10.4.1 │  │  10.10.5.1 │
│  │            │  │            │  │            │  │            │  │            │
│  │  Cerulean  │  │  Capstone  │  │  Monarch   │  │  Rizzaura  │  │   Atlas    │
│  │  AthenIQ   │  │   Zeus     │  │  Jellyfin  │  │   ONYX     │  │   Oasis    │
│  │  Magnate   │  │ OmniRoute  │  │    *arr    │  │            │  │   Gitea    │
│  │  Signara   │  │            │  │    NPM     │  │            │  │   Chef     │
│  │            │  │            │  │            │  │            │  │  Distro    │
│  │  ~10 GiB   │  │  ~6 GiB    │  │  ~8 GiB    │  │  ~6 GiB    │  │  ~8 GiB    │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘  └────────────┘
│                                                                          │
│  Consul Registry (10.10.1.1:8500) — every group registers here          │
└──────────────────────────────────────────────────────────────────────────┘
```

### Quick Start

```bash
# Clone on each server
git clone https://github.com/innotelinc/innotel-platform-stack.git
cd innotel-platform-stack
cp .env.example .env   # fill in server IPs, WG keys, etc.

# Server 1 — starts mesh + Consul + Group 1
./stack.sh up 1

# Servers 2-5 — join mesh + start their group
./stack.sh up 2   # on server 2
./stack.sh up 3   # on server 3
# ...

# Single-server mode (everything on one box)
./stack.sh up all

# Any combination on any server — groups are independent projects
# that join the same shared mesh:
./stack.sh up 2        # group 2 alone
./stack.sh up 3 4      # groups 3+4 together on one server
./stack.sh up 2 4      # voice + social on one box, media elsewhere
```

### Universal Extension System

Extensions attach to any group and auto-discover cross-group dependencies:

```bash
# Enable monitoring (Prometheus + Grafana) on all servers
./stack.sh enable monitoring all

# Enable Authentik on a specific server
./stack.sh enable cerulean-auth 4

# Discover any service across the mesh
./stack.sh discover omniroute   # → 10.10.2.1:20128
./stack.sh discover authentik   # → 10.10.1.1:9000
```

See [docs/Architecture.md](docs/Architecture.md) for the full architecture reference
and [extensions/README.md](extensions/README.md) for the extension authoring guide.

## Platform responsibilities

### Platform services (horizontal layers)

#### Authentik — IdentityOps
- **Owns:** SSO, OIDC, OAuth2, SAML, MFA, RBAC, SCIM, user accounts, organizations,
  groups, roles, permissions, tenant provisioning.
- **Provides identity to:** Cerulean · ONYX · Magnate · Monarch · Zeus · Oasis · Signara ·
  Capstone · Rizz Aura.
- **Does not own:** billing, secrets, certificates, storage, media.

#### Infisical — SecretOps
- **Owns:** secrets, API keys, DNS credentials, TLS private keys, CA keys, service
  credentials, SMTP secrets, OAuth secrets, webhook tokens, secret rotation, secret
  auditing.
- **Provides secret management to:** Cerulean · ONYX · Magnate · Monarch · Zeus · Oasis ·
  Signara · Capstone.
- **Does not own:** identity, billing, DNS records, certificates (lifecycle), storage.

#### Cerulean — Auth & Trust stack
- **Owns:** certificate lifecycle, ACME automation, PKI, DNS automation, certificate
  discovery, certificate deployment, trust monitoring, DNS health, compliance reporting,
  trust scoring.
- **Hosts the shared identity and secrets plane.** Cerulean runs the stack's Authentik
  and Infisical instances — it is the **single login point for every platform**. All
  sign-in, signup, and password flows go through its Authentik at
  `https://auth.cerulean.innotel.us`; secrets live in its Infisical at
  `https://secrets.cerulean.innotel.us`.
- **Per-platform auth aliases.** Every platform's documented login endpoint
  (`auth.<platform>.innotel.us` — e.g. `auth.magnate.innotel.us`, `auth.zeus.innotel.us`,
  `auth.capstone.innotel.us`) is an edge alias that fronts the same Cerulean Authentik,
  so each platform keeps a stable, branded login URL while identity stays centralized.
  The legacy `auth.innotel.us` host was consolidated into Cerulean and serves the same
  instance.
- **Consumes:** Authentik (identity), Infisical (secrets).
- **Does not own:** users, passwords, payment processing.

#### ONYX — StorageOps
- **Owns:** file storage, object storage, backups, snapshots, replication, application
  storage, media storage, NAS features, virtualization storage.
- **Does not own:** identity, billing, certificates.

### Edge platform

#### NPM Edge — EdgeOps
- **Owns:** public HTTP/S routing, TLS termination at the edge, proxy hosts, access lists,
  generated Nginx configuration, and recoverable NPM state.
- **Consumes:** Cerulean (DNS and certificate lifecycle), Infisical (secrets), and
  Authentik/application platforms indirectly through proxied services.
- **Does not own:** authoritative DNS, identity, billing, application data, or long-term storage.

**DNS & TLS convention (one wildcard per platform zone).** Every platform owns its own
second-level zone under the apex, and TLS is **one wildcard cert per zone** —
`*.magnate.innotel.us`, `*.monarch.innotel.us`, `*.zeus.innotel.us`, `*.signara.innotel.us`,
`*.capstone.innotel.us`, and so on — never `*.innotel.us`, which matches only a single label
and cannot cover `app.<platform>.innotel.us`-style hosts. Wildcards are issued with a
**DNS-01 challenge** against the shared BIND (RFC 2136 / nsupdate, `cerulean` TSIG key),
so no `_acme-challenge` records need manual management, and every proxy host under the zone
attaches the same wildcard (exact-match per-host certs are only used for deeper multi-label
names a one-label wildcard cannot reach, e.g. `backend.api.capstone.innotel.us`). DNS for each proxy-host name
is a **CNAME to the apex** (`<host>.<zone>.innotel.us → innotel.us.`) in the same BIND zone.
Bring-up scripts (`scripts/npm-proxy-hosts.py` per repo, or Cerulean's provisioning) follow
this pattern so re-running them never re-issues or detaches certs.

### Business platforms

#### Monarch — MediaOps
- **Owns:** streaming, media libraries, user profiles, watch history, recommendations,
  collections, live TV, playback, media discovery.
- **Consumes:** Authentik · Infisical · ONYX · Magnate · Cerulean · NPM Edge.
- **Does not own:** storage, billing, identity.

#### Zeus — VoiceOps
- **Owns:** VoIP, SIP, SMS, PBX, phone numbers, call routing, mobile PWA, communications.
- **Consumes:** Authentik · Infisical · Magnate · Cerulean · NPM Edge.

#### Oasis — MailOps
- **Owns:** email, calendars, contacts, collaboration, mail security, mail routing, team
  communications.
- **Consumes:** Authentik · Infisical · Cerulean · Magnate · NPM Edge.

#### Signara — DocumentOps
- **Owns:** document signing, agreements, templates, audit trails, signature workflows,
  compliance evidence, identity verification.
- **Consumes:** Authentik · Cerulean · Infisical · ONYX · Magnate · NPM Edge.

#### AthenIQ — LearningOps
- **Owns:** course catalog and enrollment, courseware delivery, assessments, learner
  records, AI interactive classrooms (multi-agent lessons, quizzes, simulations),
  course completions and completion evidence.
- **Provides:** learning delivery and completion records that feed Signara's signed
  course certificate workflows.
- **Consumes:** Authentik · Infisical · Cerulean · ONYX · Magnate · Signara · NPM Edge.
- **Does not own:** identity, secrets, certificates/DNS, storage, billing, or signing.
  AthenIQ emits completion evidence only — Signara remains the sole signer.

#### Atlas — CodeOps
- **Owns:** repositories, forks, pull requests, code review, issues/boards, wikis,
  releases, package registry, Actions CI/CD, and AI-assisted application generation
  (Chef on a self-hosted Convex backend, models via one OmniRoute gateway).
- **Provides:** the canonical git remote and CI for the other platforms' code, and
  the AI app builder that scaffolds new platform applications.
- **Consumes:** Authentik · Infisical · Cerulean · Magnate · NPM Edge.
- **Does not own:** identity, secrets, certificates/DNS, storage, or the
- **Integrates with:** Atlas (CodeOps) — Distro builds apps live in the browser;
  Atlas ships them via Gitea + Chef/Convex. Distro exports to Atlas git remaps;
  Atlas scaffolds Convex backends for Distro-built apps. Both share OmniRoute,
  Magnate, Authentik, Infisical, Cerulean, and NPM Edge.
  production runtime of the platforms it helps build — Atlas holds the source.

#### Distro — BuilderOps
- **Owns:** the in-browser AI app builder (fork of bolt.diy): chat-to-code agent UI,
  WebContainer sandbox, live preview, file tree, terminal; multi-tenant builder
  accounts (Distro control plane) with per-user OmniRoute gateway keys, quota/usage
  enforcement, and an admin console.
- **Provides:** the interactive AI app-building front door — browser → Distro →
  OmniRoute → upstream models.
- **Consumes:** OmniRoute (AI plane) · NPM Edge · Authentik · Infisical · Magnate (billing) · Cerulean (DNS + certs).
- **Does not own:** identity, secrets, certificates/DNS, storage, or the
- **Integrates with:** Atlas (CodeOps) — Distro builds apps live in the browser;
  Atlas ships them via Gitea + Chef/Convex. Distro exports to Atlas git remaps;
  Atlas scaffolds Convex backends for Distro-built apps. Both share OmniRoute,
  Magnate, Authentik, Infisical, Cerulean, and NPM Edge.
  LLM gateway itself (OmniRoute is a shared platform service / extension).

#### Capstone — AgentOps
- **Owns:** voice AI agents, call screening, AI receptionists, speech processing, voice
  workflows, telephony automation.
- **Consumes:** Zeus · Authentik · Infisical · Magnate · NPM Edge.

#### Rizz Aura — CommunityOps
- **Owns:** leaderboards, reputation, rankings, achievements, communities, competition
  engine.
- **Consumes:** Authentik · Magnate · NPM Edge.

#### zapit — TransferOps (edge utility)
- **Owns:** ephemeral peer-to-peer transfer (WebRTC data channels), room codes, relay
  fallback, Zings (text snippets), QR pairing.
- **Consumes:** Authentik (optional SSO; zero-login is the default).
- **Explicitly does not own:** storage (ONYX), identity (Authentik), billing (Magnate).

## Dependency graph

```
Identity (Authentik) ────────► Secrets (Infisical)
        │                           │
        ▼                           ▼
   Trust (Cerulean)          Storage (ONYX)
        │                           │
        └──────────┬────────────────┘
                   ▼
            Revenue (Magnate)
                   │
                   ▼
            Edge (NPM Edge)
                   │
        ┌──────────┼──────────┬──────────────┐
        ▼          ▼          ▼              ▼
     Monarch     Zeus      Oasis         Signara
                   │
                   ▼
              Capstone
        (Rizz Aura ──► Authentik, Magnate)
        (AthenIQ ──► Authentik · Infisical · Cerulean · ONYX · Magnate · Signara)
        (Atlas ──► Authentik · Infisical · Cerulean · Magnate)
```

Deployment order:

1. **Authentik** (identity must exist first — everyone consumes it).
2. **Infisical** (secrets next — Cerulean, ONYX, Magnate, NPM Edge and every business platform read
   credentials from it).
3. **Cerulean · ONYX · Magnate · NPM Edge** (trust, storage, revenue, and edge in parallel — nothing above
   them works without at least one).
4. **Business platforms** (Monarch, Zeus, Oasis, Signara — then Capstone on top of Zeus;
   Rizz Aura can ride anywhere after Authentik + Magnate, AthenIQ after
   Authentik + Infisical + Magnate — it also consumes Signara for signed course
   certificates, and Atlas after Authentik + Infisical + Magnate to host this
   ecosystem's code and CI).

### One stack vs split deployment

Every platform ships a self-contained Compose file plus a stdlib-only
`scripts/npm-proxy-hosts.py` provisioner, so the same code runs **either as one
all-encompassing stack on a single host or as individual platforms on separate hosts**
behind the shared edge — no code changes either way, only DNS/forward addresses:

| Topology | Layout | How it's wired |
| --- | --- | --- |
| **Unified (all-in-one)** | Every platform's Compose stack on one box; NPM Edge
  (or the bundled NPM) and BIND can run on the same host | Platform `setup.sh` runs
  locally; proxy hosts forward to `127.0.0.1`/compose service names; wildcard DNS-01 +
  CNAMEs target the local BIND. This is the model for a self-contained appliance
  (e.g. offline/USB deploys) |
| **Split (per-platform hosts)** | Each platform on its own machine (or VPS), all
  pointing at one shared NPM Edge + BIND | Platform `.env` sets `NPM_MODE=remote`,
  `NPM_BASE_URL=https://proxy.innotel.us`, `NPM_FORWARD_HOST=<its own IP>` and the
  `DNS_TSIG_*`/`BIND_*` credentials; the provisioner writes that host's subdomain
  CNAME/A records via nsupdate and forwards from the shared edge. Required when a
  platform must be reachable from another network or runs on dedicated hardware |

Rules that hold in both:

- **Wildcards are per platform zone** (`*.magnate.innotel.us`, …), never `*.innotel.us` —
  see the NPM Edge convention above.
- DNS records and TLS are provisioned by the platform's own idempotent script, so a host
  can move between topologies by re-running `setup.sh`/`npm-proxy-hosts.py` after changing
  `.env` — nothing is hand-edited in NPM or BIND.
- Certificates (Cerulean), secrets (Infisical) and identity (Authentik) stay centralized
  in both models; only the app + its data move.

## Service ownership matrix

| Capability | Owner | Platform |
|---|---|---|
| Identity / SSO / MFA | IdentityOps | Authentik |
| Secrets / keys / tokens | SecretOps | Infisical |
| Certificates / PKI / DNS | TrustOps | Cerulean |
| Public routing / TLS / proxy recovery | EdgeOps | NPM Edge |
| Storage / backups / snapshots | StorageOps | ONYX |
| Billing / subscriptions / entitlements | RevenueOps | Magnate |
| Streaming / media | MediaOps | Monarch |
| VoIP / SIP / SMS / PBX | VoiceOps | Zeus |
| Email / calendar / contacts | MailOps | Oasis |
| Signing / agreements / audit | DocumentOps | Signara |
| Voice AI agents / telephony automation | AgentOps | Capstone |
| Leaderboards / reputation / community | CommunityOps | Rizz Aura |
| Ephemeral P2P transfer / relay | TransferOps | zapit |
| Learning / courses / AI classrooms | LearningOps | AthenIQ |
| Source control / CI / AI app building | CodeOps | Atlas |

## Integration flows

- **Identity flow:** user → Cerulean's Authentik (`auth.cerulean.innotel.us`, or the
  platform's `auth.<platform>.innotel.us` alias) → OIDC → service issues session → every
  platform trusts the same identity. Disable the user in Authentik and every consuming
  platform loses them instantly. Any login goes through Cerulean — no platform runs its
  own login page or password store.
- **Registered OIDC providers (Cerulean Authentik).** One OAuth2/OIDC provider + application
  per consuming service, all carrying the default `openid profile email` (+ `groups`) scope
  mappings so userinfo/token claims are populated:

  | Client ID | Application | Redirect |
  |---|---|---|
  | `cerulean` | Cerulean portal | `cerulean.innotel.us/api/auth/oidc/callback` |
  | `capstone-dashboard` | Capstone Dashboard | `dashboard.capstone.innotel.us/api/auth/callback` |
  | `magnate-admin` | Magnate Admin | `admin.magnate.innotel.us/api/auth/authentik/callback` |
  | `monarch-web` | Monarch (Homarr) | `monarch.innotel.us/api/auth/callback/oidc` |
  | `signara-web` | Signara | `app.signara.innotel.us/api/auth/callback` |
  | `oasis-app` (public) / `oasis-admin` / `oasis-files` / `oasis-mail` / `oasis-api` | Oasis | per `docs/SSO.md` |
  | `onyx-platform` | ONYX Platform | regex `app./admin.onyx.innotel.us/*` |
  | `rizz-aura-web` | Rizz Aura | `api.rizz.innotel.us/api/auth/callback` |
  | `zapit` (public PKCE) | ZapIt | `zapp.innotel.us/api/auth/callback` |
  | `atlas-gitea` | Atlas (Gitea) | `git.innotel.us/user/oauth2/authorize` (OIDC) |
  | `atlas-chef` | Atlas (Chef, after the auth fork) | `chef.innotel.us/api/auth/callback` |
  | `pm3` `pm4` `incus` `mail` `monit` | migrated legacy apps | per legacy config |

  Monarch additionally consumes Cerulean via the **`jellyfin-ldap` LDAP outpost** (Jellyfin
  logins resolve against Cerulean users — `paid_users` gates access, `jellyfin_admins` get
  admin), so disabling a user in Cerulean blocks their media login too.
- **Secret flow:** credentials live in Cerulean's Infisical (`secrets.cerulean.innotel.us`)
  and are pulled into each platform's `.env` at setup; service credentials, API keys, and
  TLS private keys are written to Infisical, never committed.
- **Secret flow:** setup pulls credentials from Infisical (project-scoped, per-environment)
  into the stack; service credentials, API keys, and TLS private keys are written to
  Infisical, never committed. `infisical://` references resolve at runtime where supported.
- **Trust flow:** Cerulean issues ACME certificates and DNS records into your own BIND,
  provisions nginx proxy manager hosts, and (with Infisical) stores the private keys in
  SecretOps.
- **Revenue flow:** Magnate Checkout → webhook → Authentik group membership
  (`paid_users`) → access granted in the consuming platform; cancellation deactivates the
  user and access dies.
- **Edge flow:** NPM Edge fronts every public host — proxy hosts are provisioned
  idempotently through the NPM API by setup scripts and Cerulean, and the certificates
  NPM attaches come from Cerulean's trust lifecycle.

## Security boundaries

1. **Identity boundary** — Authentik is the only place users, groups, roles, and
   permissions exist. Business platforms store no passwords (LDAP/OIDC bind only).
2. **Secret boundary** — Infisical is the only place credentials live at rest; `.env`
   files are derived (generated/synced from Infisical), gitignored, and never committed.
3. **Trust boundary** — Cerulean is the only issuer of certificates and PKI material;
   TLS terminates at nginx proxy manager, provisioned automatically.
4. **Tenant isolation** — multi-tenant platforms scope data by Authentik groups /
   organization; secrets in Infisical are project-scoped.
5. **Network boundary** — internal APIs (recommendations, health, secrets) stay
   unpublished; only canonical subdomains are proxied.
6. **Commit boundary** — the attribution guard runs locally and in CI on every
   repository: no credit to anyone but the project owner.

## Repositories

| Platform | Repository | Stack doc |
|---|---|---|
| Authentik | upstream [goauthentik](https://github.com/goauthentik/authentik) | provisioned per stack |
| Infisical | upstream [Infisical](https://github.com/Infisical/infisical) | provisioned per stack |
| Cerulean (TrustOps) | [innotelinc/cerulean](https://github.com/innotelinc/cerulean) | [docs/stack.md](https://github.com/innotelinc/cerulean/blob/main/docs/stack.md) |
| ONYX (StorageOps) | [innotelinc/onyx-oss-platform](https://github.com/innotelinc/onyx-oss-platform) | [docs/stack.md](https://github.com/innotelinc/onyx-oss-platform/blob/main/docs/stack.md) |
| Magnate (RevenueOps) | [innotelinc/jellyfin-subscription-platform](https://github.com/innotelinc/jellyfin-subscription-platform) | [docs/stack.md](https://github.com/innotelinc/jellyfin-subscription-platform/blob/main/docs/stack.md) |
| Monarch (MediaOps) | [innotelinc/monarch](https://github.com/innotelinc/monarch) | [docs/stack.md](https://github.com/innotelinc/monarch/blob/main/docs/stack.md) |
| Zeus (VoiceOps) | [innotelinc/zeus](https://github.com/innotelinc/zeus) | [docs/stack.md](https://github.com/innotelinc/zeus/blob/main/docs/stack.md) |
| Oasis (MailOps) | [innotelinc/oasis](https://github.com/innotelinc/oasis) | [docs/stack.md](https://github.com/innotelinc/oasis/blob/main/docs/stack.md) |
| Signara (DocumentOps) | [innotelinc/signara](https://github.com/innotelinc/signara) | [docs/stack.md](https://github.com/innotelinc/signara/blob/main/docs/stack.md) |
| Capstone (AgentOps) | [innotelinc/capstone](https://github.com/innotelinc/capstone) | [docs/stack.md](https://github.com/innotelinc/capstone/blob/main/docs/stack.md) |
| NPM Edge (EdgeOps) | [innotelinc/npm](https://github.com/innotelinc/npm) | [ABOUT.md](https://github.com/innotelinc/npm/blob/develop/ABOUT.md) |
| Rizz Aura (CommunityOps) | [innotelinc/rizzaura-platform](https://github.com/innotelinc/rizzaura-platform) | [docs/stack.md](https://github.com/innotelinc/rizzaura-platform/blob/main/docs/stack.md) |
| zapit (TransferOps) | [innotelinc/zapit](https://github.com/innotelinc/zapit) | [docs/stack.md](https://github.com/innotelinc/zapit/blob/main/docs/stack.md) |
| AthenIQ (LearningOps) | [innotelinc/atheniq](https://github.com/innotelinc/atheniq) | [docs/stack.md](https://github.com/innotelinc/atheniq/blob/main/docs/stack.md) |
| Atlas (CodeOps) | [innotelinc/atlas](https://github.com/innotelinc/atlas) | [docs/stack.md](https://github.com/innotelinc/atlas/blob/main/docs/stack.md) |
| Distro (BuilderOps) | [innotelinc/distro](https://github.com/innotelinc/distro) | [docs/stack.md](https://github.com/innotelinc/distro/blob/main/docs/stack.md) |

## `stack.sh` Commands

| Command | Description |
|---|---|
| `./stack.sh up all` | Start everything (single-server mode) |
| `./stack.sh up <1-5>` | Start a specific group |
| `./stack.sh down all` | Stop everything |
| `./stack.sh status` | Show all groups and services |
| `./stack.sh list` | List groups, extensions, and discovered services |
| `./stack.sh enable <ext> [group]` | Enable an extension |
| `./stack.sh disable <ext> [group]` | Disable an extension |
| `./stack.sh discover <service>` | Find a service by name across all groups |
| `./stack.sh register <svc> <addr> <port> [tags]` | Register a service manually |
| `./stack.sh mesh` | Start only the WireGuard mesh |
| `./stack.sh logs <group>` | Tail logs for a group |

---


---

## License

Innotel Platform Stack is licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

*Innotel Platform Stack — INNOTEL V1 Enterprise Architecture. One job per platform, platform services consumed by business functions.*