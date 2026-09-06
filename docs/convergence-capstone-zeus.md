# Capstone ↔ Zeus Convergence

**Status: in progress** · updated September 6, 2026

> **Living tracker:** the Capstone repo's `docs/zeus-integration.md` is the
> authoritative, current status for the shared-PBX integration (gaps G1–G4
> and G7 resolved; G5 outbound caller ID and G6 SMS/fax for agents still
> open). This page is the deep look — read it for the target architecture
> and the phase plan, and cross-check open items against the Capstone doc
> before starting work.

This document is the deep look at how **Capstone** (AgentOps) and **Zeus**
(VoiceOps) relate, and the target architecture for the two platforms. It
answers three questions:

1. What does each platform own today?
2. What is the target model — Capstone standalone, Zeus as the VoIP platform,
   Capstone as a Zeus add-on?
3. What concrete work gets both platforms there, with no duplicated
   responsibility (the stack's golden rule)?

---

## 1. Current state

### Capstone — AgentOps (self-contained voice-AI stack)

Capstone is a complete, self-hosted voice-AI agent platform. It currently
**bundles its own telephony plane** rather than consuming Zeus:

| Layer | Component | Role |
|---|---|---|
| Telephony | Asterisk / FreePBX 17 + ARI (`pbx/`) | PBX, call routing, media |
| WebRTC | Coturn | TURN relay for clients behind NAT |
| Voice agent | Dograh (Pipecat) + `dograh-ui` (Next.js) | Real-time voice pipeline, agent workflows, telephony UI |
| Speech | Kokoro TTS · Speaches (Whisper STT) | Local speech generation / transcription |
| LLM | OmniRoute | OpenAI-compatible gateway to local/free models |
| Workflow | n8n + Workflow Studio + sandbox runners + SearXNG | Session webhooks, grading, AI-authored workflows |
| Data | postgres · redis · minio · grist (nocodb opt-in) | State, queues, objects, dashboards |
| Identity | Authentik | SSO |
| Observability | OpenTelemetry → SigNoz (ClickHouse) | Pipeline latency per call |
| Control Center | `dashboard` (React/nginx) + `dashboard-backend` (FastAPI) | Live ops UI |
| Ops | `scripts/` (setup, smoke, ISO/offline bundles), `systemd/`, `ansible/` | Bare-metal + container deploy, live/install ISO |

### Zeus — VoiceOps (communications portal + PBX)

Zeus is the ecosystem's **voice plane**: telephony, numbers, messaging, fax.
Today it is a Next.js 16 portal plus FreePBX:

| Surface | Component | Role |
|---|---|---|
| Portal | Next.js 16 (`src/app`) | Customer portal + API routes (admin, billing, contacts, fax, messages, phone, voicemail, webhooks, AMI, health). **Machine contract published:** `docs/portal-api.md` (messages/fax/voicemail + agent transfer-resolve) |
| PBX | FreePBX (Asterisk), `pbx/` fragment bootstrap | `pbx/asterisk/` fragments + `asterisk_converge.py` (converge-owned with Capstone's `[dograh]` in one live `ari.conf`, byte-idempotent), `bootstrap-zeus-pbx.sh` with unit tests in CI |
| Numbers | VoIP.ms API | Instant number provisioning |
| Softphone | SIP.js / WebRTC, WSS via `ws.<domain>` | In-browser + PWA dial pad |
| Messaging | portal API | SMS + unified messaging (`GET /api/messages`, `POST /api/messages/send`, thread + read endpoints) |
| Fax | AvantFax | Digital faxing + history (`POST /api/fax/account`, `POST/GET /api/fax/send`, `GET /api/fax/download`) |
| AI | voicemail transcription/summaries | Voicemail intelligence (`GET /api/voicemail`, summary/listened/audio/email) |
| Billing | Magnate (RevenueOps) front; legacy `STRIPE_*` self-billing deprecated, empty by default | Revenue — checkout, plans, reseller/white-label |
| Identity | Cerulean Authentik (unified auth) | SSO via `auth.cerulean.innotel.us` |
| Ops | `scripts/` (setup, smoke, npm-proxy-hosts, seed, migrations), `systemd/`, NPM wildcard TLS | Deploy + edge; `zeus-portal.service` + `zeus-pbx-sync.service/.timer`; `scripts/smoke-test.sh` (portal/edge/PBX/fax/numbers) |

---

## 2. Target model

**Capstone stays a standalone project** (own repo, own release train) whose
purpose is *agents*: voice AI agents, call screening, AI receptionists, and
personal-assistant capabilities. **Zeus stays the VoIP platform**: voice,
fax, and SMS portal features — what Zeus does today. The integration is
one-directional: **Capstone consumes Zeus as its voice plane**, becoming a
thin add-on layer on top of Zeus telephony instead of bundling Asterisk.

```
USERS
  │
  ▼
ZEUS  (VoiceOps — owns: voice · fax · SMS · numbers · softphone)
  │  PBX/ARI · AMI · portal API (SMS/fax/voicemail)
  ▼
CAPSTONE  (AgentOps — owns: agents · assistants · workflows · control center)
  │  Authentik SSO · Infisical secrets · Magnate billing (both)
  ▼
(identity · secrets · revenue · trust layers)
```

Rules that keep it single-responsibility:

- **Zeus owns telephony primitives** — extensions, routing, numbers, SIP,
  SMS, fax. Capstone never re-implements them.
- **Capstone owns agent behavior** — prompts, workflows, grading, dashboards,
  personal assistants. Zeus never hosts agents.
- **Both consume** Authentik (identity), Infisical (secrets), Magnate
  (revenue) and Cerulean (trust) — never own them.
- **Nothing is duplicated**: one PBX (Zeus's), one agent engine (Capstone's).

### Capstone as a Zeus add-on — the integration contract

| Capstone need | Zeus surface | Mechanism |
|---|---|---|
| Dial out / answer calls | PBX (Asterisk) | ARI application + extension registry against **Zeus's** FreePBX (Capstone's `pbx/` retires) |
| Live call state | AMI | Zeus already exposes AMI (`ASTERISK_AMI_*`); Capstone subscribes for screening/events |
| Send SMS / fax from an agent | Portal API | `POST /api/messages/send`, `POST /api/fax/send` on the Zeus portal — contract in zeus `docs/portal-api.md` |
| Voicemail intelligence | AI summaries | Capstone personal assistants read Zeus voicemail summaries |
| Number provisioning | VoIP.ms via Zeus | Capstone never talks to VoIP.ms directly |
| WebRTC softphone | Zeus PWA | Users call from the Zeus softphone; agents answer via ARI |

This makes Capstone installable in two shapes without changing its codebase:

1. **Standalone** (today): bundles its own PBX stack — useful for air-gapped
   or isolated deployments.
2. **Add-on** (target): `ZEUS_API_URL` + `ZEUS_API_TOKEN` (+ ARI creds to
   Zeus's PBX) in `.env`; the agent engine attaches to Zeus and its bundled
   PBX stack is skipped.

---

## 3. "Zeus mirrors Capstone" — structural parity checklist

Beyond the integration, Zeus should match Capstone's project structure and
ops bar. Gap analysis:

| Convention (Capstone has it) | Zeus today | Work |
|---|---|---|
| Layered compose (`docker-compose.yml` + platform compose for Authentik) | ✅ `docker-compose.platform.yml` | align naming/structure with Capstone's conventions |
| Setup one-shot (`./scripts/setup.sh`, idempotent, generates secrets) | ✅ `scripts/setup.sh` + `setup-portal.sh` | converge flags/UX (`--no-build`, `--skip-auth`) |
| Full PBX entrypoint (`pbx/` + ARI wiring scripts) | ✅ `pbx/` bootstrap shipped: fragments, AMI user, ARI converge (`asterisk_converge.py`), WSS — unit tests in CI | structural parity done; next is the *shared-box* run (Capstone fragments applied with `--owner capstone`) |
| Systemd units for bare-metal | ✅ `zeus-portal.service`, `zeus-pbx-sync.service/.timer` | Ansible playbook not ported (optional; Capstone still owns the pattern) |
| Ansible playbook for bare-metal PBX | ❌ none | port `ansible/dograh-ari.yml` pattern → `zeus-ari.yml` (optional) |
| Offline + live-USB ISO deploy | ❌ none | reuse `scripts/build-live-usb.sh` / `build-offline-bundle.sh` |
| Control Center ops dashboard (services/health/ports/alerts) | ✅ portal `/dashboard/health` ops view + `/api/health` | shared Control Center still open (Capstone dashboard is the recommended owner — §6 Q3) |
| Observability (OTel → SigNoz) | ❌ none | optional `compose.observability.yml` profile |
| Smoke tests (`smoke-test.sh` / `smoke-e2e.sh`) | ✅ `scripts/smoke-test.sh` (portal/edge/PBX/fax/numbers, mirrors Capstone convention) | — |
| Infisical profile + setup | ✅ | runtime `infisical://` resolution (like Cerulean/Onyx) still open |
| Attribution guard + Pages landing | ✅ | done (guard + landing shipped) |
| Stack doc + role page | ✅ `docs/stack.md` | link convergence doc (below) |

Nothing here changes what Zeus *is* (voice/fax/SMS portal) — it raises Zeus
to Capstone's structural bar so the two projects feel like one platform.

---

## 4. Phased plan

**Phase 0 — contract freeze (docs, no code)**
- [x] This analysis doc (landing, roadmap, stack pages updated)
- [x] Zeus portal API spec for `messages` / `fax` / `voicemail` published
      (`docs/portal-api.md` in the zeus repo, incl. a new `GET /api/voicemail`
      list endpoint so agents can poll messages/summaries M2M)

**Phase 1 — Zeus parity (structural mirror)**
- [x] `pbx/` bootstrap for Zeus's FreePBX — `pbx/` fragments, AMI/ARI users,
      WSS wiring, `asterisk_converge.py` + `bootstrap-zeus-pbx.sh` (tests in CI)
- [x] `systemd/` units (`zeus-portal.service`, `zeus-pbx-sync.service/.timer`)
- [x] Smoke suite — `scripts/smoke-test.sh` (portal, edge, PBX, fax, numbers)
- [x] Control Center surface — portal `/dashboard/health` ops view shipped;
      a shared Capstone-owned Control Center remains open (see §6 Q3)
- [ ] Optional OTel → SigNoz profile (unchanged — still open)
- [ ] `infisical://` runtime secret resolution in the portal (Go-style client
      or TS equivalent — same contract as Cerulean/Onyx)

> Ansible (`zeus-ari.yml`) and the offline/live-USB ISO are the remaining
> bare-metal parity gaps — both optional per §6 Q4.

**Phase 2 — Capstone consumes Zeus (the add-on)**

> Status: unchanged in code, but the Zeus-side prerequisites have landed
> (portal API spec above, entitlements G7, transfer-resolve G3, converge
> tool G1/G2/G4) — the remaining bullets are Capstone's own `ZEUS_*`
> config, ARI re-pointing, and agent-side SMS/fax/voicemail actions
> against the new spec.

- [ ] Capstone `ZEUS_*` config: `ZEUS_API_URL` / `ZEUS_API_TOKEN` / ARI creds
- [ ] ARI wiring targets Zeus's Asterisk; Capstone's bundled PBX becomes
      optional (`CAPSTONE_PBX=standalone|zeus`)
- [ ] Agent-side SMS/fax/voicemail actions via Zeus portal API
- [ ] One-click "register agent on Zeus" flow in Workflow Studio

**Phase 3 — ops convergence**
- [ ] Shared deploy conventions documented once (in this repo), implemented
      identically in both
- [ ] Cross-platform smoke: place a call through Zeus → answered by a
      Capstone agent → SMS summary sent back through Zeus

---

## 5. What explicitly does NOT move

- **Identity** stays Authentik (both platforms consume; neither owns).
- **Secrets** stay Infisical (both consume).
- **Billing** stays Magnate (both consume; Zeus's legacy `STRIPE_*`
  self-billing mode is deprecated and empty by default — revisit only when
  Magnate's billing API is production-ready).
- **Storage** stays ONYX (Capstone transcripts/recordings may archive there).
- **Trust/certs** stay Cerulean (both consume).

## 6. Open questions for the owner

1. Should Capstone's standalone mode eventually be *removed* (PBX always
   from Zeus), or kept forever for air-gapped installs? (Recommended: keep.)
2. Does the personal-assistant surface live in the **Zeus portal** (agent
   tab) or in a **Capstone UI** embedded via iframe/SSO? (Recommended:
   Capstone UI, SSO'd through Authentik, linked from Zeus.)
3. Is the Control Center a Zeus surface (portal route) or a shared Capstone
   dashboard pointed at both stacks? (Recommended: Capstone dashboard,
   since it already owns the ops UI.)
4. Zeal for the ISO/offline path in Zeus — same offline-first commitment as
   Capstone, or compose-only for now?