<div align="center">

# 🏛️ Innotel Platform Stack

**INNOTEL V1 Enterprise Architecture Bundle — the canonical single-responsibility platform ecosystem.**

One stack. Every platform owns exactly one job. Identity, secrets, trust, storage, and
revenue are platform services; everything else is a business function that consumes them.

</div>

> **About this repo** — the source of truth for the Innotel Platform Stack: who owns what,
> who consumes whom, how the platforms integrate, the order they deploy in, and the
> security boundaries between them. Every product repository carries a
> [docs/stack.md](docs/stack.md)-style section that points back here, so the ecosystem
> definition lives in exactly one place. **Landing page:** [github.com/innotelinc/innotel-platform-stack](https://github.com/innotelinc/innotel-platform-stack)

---

## Executive summary

The Innotel ecosystem is a portfolio of self-hostable platforms that follow strict
**single-responsibility** principles: every platform owns one primary domain of
responsibility, and no platform duplicates another's. Five of them are **platform
services** — horizontal capabilities everything else consumes:

| Golden rule | Platform | Classification |
|---|---|---|
| Authentik = Identity | Authentik | IdentityOps |
| Infisical = Secrets | Infisical | SecretOps |
| Cerulean = Trust | Cerulean | TrustOps |
| ONYX = Storage | ONYX | StorageOps |
| Magnate = Revenue | Magnate | RevenueOps |

The remaining platforms are **business functions** built on top:

| Platform | Classification | Consumes |
|---|---|---|
| Monarch | MediaOps | Authentik · Infisical · ONYX · Magnate · Cerulean |
| Zeus | VoiceOps | Authentik · Infisical · Magnate · Cerulean |
| Oasis | MailOps | Authentik · Infisical · Cerulean · Magnate |
| Signara | DocumentOps | Authentik · Cerulean · Infisical · ONYX · Magnate |
| Capstone | AgentOps | Zeus · Authentik · Infisical · Magnate |
| Rizz Aura | CommunityOps | Authentik · Magnate |
| zapit | TransferOps | Authentik (optional) |

## Documents

- [**Capstone ↔ Zeus convergence**](docs/convergence-capstone-zeus.md) — the
  deep look at AgentOps/VoiceOps: Capstone standalone, Zeus as the VoIP
  platform, Capstone as a Zeus add-on.

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
```

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

#### Cerulean — TrustOps
- **Owns:** certificate lifecycle, ACME automation, PKI, DNS automation, certificate
  discovery, certificate deployment, trust monitoring, DNS health, compliance reporting,
  trust scoring.
- **Consumes:** Authentik (identity), Infisical (secrets).
- **Does not own:** users, passwords, payment processing.

#### ONYX — StorageOps
- **Owns:** file storage, object storage, backups, snapshots, replication, application
  storage, media storage, NAS features, virtualization storage.
- **Does not own:** identity, billing, certificates.

#### Magnate — RevenueOps
- **Owns:** subscriptions, plans, billing, invoices, payments, revenue analytics, coupons,
  trials, entitlements, customer accounts, usage metering.
- **Provides billing to:** Monarch · Zeus · Oasis · Signara · Cerulean · Capstone ·
  Rizz Aura.
- **Does not own:** identity, media, storage.

### Business platforms

#### Monarch — MediaOps
- **Owns:** streaming, media libraries, user profiles, watch history, recommendations,
  collections, live TV, playback, media discovery.
- **Consumes:** Authentik · Infisical · ONYX · Magnate · Cerulean.
- **Does not own:** storage, billing, identity.

#### Zeus — VoiceOps
- **Owns:** VoIP, SIP, SMS, PBX, phone numbers, call routing, mobile PWA, communications.
- **Consumes:** Authentik · Infisical · Magnate · Cerulean.

#### Oasis — MailOps
- **Owns:** email, calendars, contacts, collaboration, mail security, mail routing, team
  communications.
- **Consumes:** Authentik · Infisical · Cerulean · Magnate.

#### Signara — DocumentOps
- **Owns:** document signing, agreements, templates, audit trails, signature workflows,
  compliance evidence, identity verification.
- **Consumes:** Authentik · Cerulean · Infisical · ONYX · Magnate.

#### Capstone — AgentOps
- **Owns:** voice AI agents, call screening, AI receptionists, speech processing, voice
  workflows, telephony automation.
- **Consumes:** Zeus · Authentik · Infisical · Magnate.

#### Rizz Aura — CommunityOps
- **Owns:** leaderboards, reputation, rankings, achievements, communities, competition
  engine.
- **Consumes:** Authentik · Magnate.

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
        ┌──────────┼──────────┬──────────────┐
        ▼          ▼          ▼              ▼
     Monarch     Zeus      Oasis         Signara
                   │
                   ▼
              Capstone
        (Rizz Aura ──► Authentik, Magnate)
```

Deployment order:

1. **Authentik** (identity must exist first — everyone consumes it).
2. **Infisical** (secrets next — Cerulean, ONYX, Magnate and every business platform read
   credentials from it).
3. **Cerulean · ONYX · Magnate** (trust, storage, and revenue in parallel — nothing above
   them works without at least one).
4. **Business platforms** (Monarch, Zeus, Oasis, Signara — then Capstone on top of Zeus;
   Rizz Aura can ride anywhere after Authentik + Magnate).

## Service ownership matrix

| Capability | Owner | Platform |
|---|---|---|
| Identity / SSO / MFA | IdentityOps | Authentik |
| Secrets / keys / tokens | SecretOps | Infisical |
| Certificates / PKI / DNS | TrustOps | Cerulean |
| Storage / backups / snapshots | StorageOps | ONYX |
| Billing / subscriptions / entitlements | RevenueOps | Magnate |
| Streaming / media | MediaOps | Monarch |
| VoIP / SIP / SMS / PBX | VoiceOps | Zeus |
| Email / calendar / contacts | MailOps | Oasis |
| Signing / agreements / audit | DocumentOps | Signara |
| Voice AI agents / telephony automation | AgentOps | Capstone |
| Leaderboards / reputation / community | CommunityOps | Rizz Aura |
| Ephemeral P2P transfer / relay | TransferOps | zapit |

## Integration flows

- **Identity flow:** user → Authentik (OIDC) → service issues session → every platform
  trusts the same identity. Disable the user in Authentik and every consuming platform
  loses them instantly.
- **Secret flow:** setup pulls credentials from Infisical (project-scoped, per-environment)
  into the stack; service credentials, API keys, and TLS private keys are written to
  Infisical, never committed. `infisical://` references resolve at runtime where supported.
- **Trust flow:** Cerulean issues ACME certificates and DNS records into your own BIND,
  provisions nginx proxy manager hosts, and (with Infisical) stores the private keys in
  SecretOps.
- **Revenue flow:** Magnate Checkout → webhook → Authentik group membership
  (`paid_users`) → access granted in the consuming platform; cancellation deactivates the
  user and access dies.

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
| Rizz Aura (CommunityOps) | [innotelinc/rizzaura-platform](https://github.com/innotelinc/rizzaura-platform) | [docs/stack.md](https://github.com/innotelinc/rizzaura-platform/blob/main/docs/stack.md) |
| zapit (TransferOps) | [innotelinc/zapit](https://github.com/innotelinc/zapit) | [docs/stack.md](https://github.com/innotelinc/zapit/blob/main/docs/stack.md) |

---

*Innotel Platform Stack — INNOTEL V1 Enterprise Architecture. One job per platform, platform services consumed by business functions.*