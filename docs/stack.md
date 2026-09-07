# Innotel Platform Stack — Stack Role

**Role: Platform stack repository** — the canonical single-responsibility architecture definition for the whole Innotel ecosystem.

This page is the *hub* document: every product repository's `docs/stack.md` points back
here, and this repo is the one place where the owns/consumes map is defined.

## Owns

- The canonical single-responsibility architecture definition (this document + the README's
  platform service table + the integration flows + security boundaries).
- `stack.sh` — the multi-group orchestrator that bootstraps all platform groups, the
  WireGuard mesh, Consul service discovery, and extension enable/disable.
- The five-group deployment topology (Primary / Voice / Media / Social / Dev) used across
  the ecosystem.
- The shared dev/cosmetic toolkit under `scripts/` (branding, healthchecks, bootstrap,
  backup, sync) used by multiple repos.
- The conformity standard (`docs/standard.md`) and the `scripts/conform-project.sh`
  scaffolding/audit tool that keeps every repo consistent.

## Provides

- The canonical definition of who owns what: Authentik = Identity, Infisical = Secrets,
  Cerulean = Trust, ONYX = Storage, Magnate = Revenue, NPM Edge = Edge; everything else is
  a business function that consumes those.
- The cross-platform integration flows (identity, secrets, trust, revenue, edge).
- The security boundary model (identity / secret / trust / tenant / network / commit).
- The canonical repo list with links to each platform's `docs/stack.md`.

## Consumes

- Nothing platform-level. This repo is the definition layer, not a runtime service. It
  assembles upstream platforms (Authentik, Infisical, Gitea, Chef, Convex, NPM, Jellyfin,
  *arr, Asterisk/FreePBX, Zimbra, etc.) and documents their roles.

## Explicitly does NOT own

- Runtime operation of any platform service. Each platform owns its own deployment, compose
  stack, and runtime config. This repo only orchestrates and documents.
- Application data, user accounts, secrets at runtime, TLS material at runtime, or billing
  transactions — those belong to the owning platform.

## Service map (stack-owned)

| Component | Technology | Job |
|---|---|---|
| `stack.sh` | bash orchestrator | Boot groups, mesh, Consul, extensions; discover/register services |
| WireGuard mesh | wg overlay | Encrypt + connect the 5 groups across servers |
| Consul | service registry | Service discovery across groups |
| Extension system | compose fragments | Attach optional capabilities to any group |
| Conformity standard | `docs/standard.md` + `scripts/conform-project.sh` | Keep every repo conformant |

## In the ecosystem

| Flow | Path |
|---|---|
| Definition source | This repo is the canonical definition; every platform links back here |
| Orchestration | `stack.sh` boots the platform services the other platforms consume |
| Conformity | `scripts/conform-project.sh` audits/scaffolds every product repo |
| Releases | This repo cuts releases via `.github/workflows/release.yml` |

Back to the canonical definition: the
[Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack).
