# Innotel Platform Stack — Repository Conformity Standard

**Source of truth for the shape of every Innotel product repository.** A repo that
passes this checklist is *conformant*: same headers, same guard, same landing, same
stack doc, same license posture, same env template, same CI posture. Use
[`scripts/conform-project.sh`](../scripts/conform-project.sh) to bootstrap or audit a repo.

> This standard is descriptive-first: it is written from what the portfolio already does.
> Where this document is silent, match the most complete existing repo (currently Atlas,
> Cerulean, Signara). Where it conflicts with a platform's unique needs (monolith vs
> Compose, AGPL vs MIT), the platform's own docs win — but the *shared layers* still
> conform.

**Golden rules (short version):**
1. One job per platform; consume the platform services, never re-implement them.
2. Cerulean = auth/trust (Authentik SSO + DNS + TLS + PKI). Magnate = billing. Zeus = telephony.
   ONYX = storage. Infisical = secrets. NPM Edge = edge.
3. Shared layers are identical across repos: README shape, landing outline, attribution guard
   (CI + hooks + guard-lib), `docs/stack.md`, `.env.example` posture, license posture.
4. No secrets, no real IPs/hostnames, no AI attribution in any repo file or commit.

---

## 1. The stack (one job per platform)

Every platform repo declares its role in the
[Innotel Platform Stack](../README.md) — owns / provides / consumes / does not own —
in `docs/stack.md`, and points back to the canonical stack repo. The six platform
services are owned **once** and consumed by everyone else:

| Capability | Owner | Classification | Consumed by |
|---|---|---|---|
| Identity / SSO / MFA | Authentik (hosted inside Cerulean) | IdentityOps | every platform |
| Secrets / keys / tokens | Infisical (hosted inside Cerulean) | SecretOps | every platform |
| Certificates / PKI / DNS | Cerulean | TrustOps | every platform |
| Storage / backups / snapshots | ONYX | StorageOps | platforms that need storage |
| Billing / subscriptions / entitlements | Magnate | RevenueOps | platforms with paid seats |
| Public routing / TLS / proxy recovery | NPM Edge | EdgeOps | every platform with public hosts |

**Rule:** a business platform (Atlas, Distro, Zeus, Monarch, Capstone, Rizz Aura, AthenIQ,
Signara, Oasis, zapit) never re-implements identity, secrets, billing, DNS/TLS, or storage
for itself. It integrates with the platform service and documents the contract.

---

## 2. The canonical file layout

Every conformant repo contains at least these paths (a repo missing one is non-conformant
until it adds it):

```
<repo>/
├── README.md                 # front door — see §3
├── LICENSE                   # — see §7
├── .env.example              # env template — see §8
├── .gitignore                # at minimum ignores .env, data + build artifacts
├── Makefile                  # operator workflow — see §6  (optional for non-compute repos)
├── docker-compose.yml        # if the repo runs a compose stack
├── package.json              # if the repo ships Node/TS/JS
├── web/landing/index.html    # GitHub Pages landing — see §4
├── .github/
│   ├── workflows/
│   │   ├── attribution-guard.yml   # attribution guard — see §5
│   │   └── ci.yml                  # optional but strongly preferred
│   └── ...
└── docs/
    └── stack.md              # role in the Innotel Platform Stack — see §9
```

Optional but preferred where relevant: `docs/Architecture.md`,
`docs/Integrations.md`, `docs/Deployment.md`, `docs/ops.md`, `.github/workflows/pages.yml`,
`.github/workflows/release.yml`, `.githooks/`.

---

## 3. README — the front door

Every conformant README follows this shape (sections may be empty/omitted if genuinely N/A,
but the structure stays):

1. **Prologue (centered, with the GitHub badge + license badge):**
   - One-line title + a one-line tagline that names the *classification* (CodeOps, BuilderOps,
     VoiceOps, etc.) and the self-hosted / single-responsibility framing.
   - `![CI](.../actions/workflows/ci.yml/badge.svg)` and
     `![License: ...](...)` badges. If the repo publishes releases, add the release badge.
2. **About/blockquote:** 2–4 sentences. Names the upstream(s) if the repo is a fork or
   assembly, names the platform services it consumes, and ends with the landing-page link.
3. **`## Why <name>`** — a **Problem / Answer** table. This is the portfolio's signature
   README element. 3–7 rows.
4. **`## What it is`** — bullet list of the things the platform *owns*.
5. **`## Quick start`** — the minimal first-successful-boot recipe (copy-pasteable).
6. **`## Documentation`** — table of `docs/*` entries with what each covers.
7. **Repo layout** (optional; present in the larger platforms).
8. **`## Status` / `## Notes`** (optional; for convergence targets, non-verified paths,
   sizing notes — whatever is honest and useful).
9. **`## License`** — one paragraph, states the repo's license and how upstream licenses are
   retained. For assembled/forked repos this also references `THIRD_PARTY_NOTICES.md` if present.
10. **Footer:** `© <year> <Platform> — <tagline>. © <year>` (or equivalent), plus the GitHub
    link and the Innotel Platform Stack link.

**Rules:**
- The `Why` table and the badges are mandatory for a *conformant product repo*.
- README prose is never the place to track detailed config — that's `.env.example` + docs.
- README must not contain secrets, keys, IPs, or hostnames that vary per deployment.

---

## 4. GitHub Pages landing — `web/landing/index.html`

Every conformant product repo publishes a static landing page to GitHub Pages:
`https://innotelinc.github.io/<repo>/` (configured once in repo Settings → Pages → Source:
GitHub Actions; the `pages.yml` workflow then republishes on every push to `main` that
touches `web/landing/**`).

**Conformity rules for the landing page:**
- The landing must actually be **published**: Pages enabled on the repo and the site reachable
  at `https://innotelinc.github.io/<repo>/` (the conformity audit fails a repo whose Pages are
  disabled — the `pages.yml` workflow cannot self-enable with an Actions token).
- File: `web/landing/index.html` — a single self-contained static HTML file (no build step).
- `<meta charset>`, `<meta name="viewport">`, a meaningful `<title>`, and `og:title` /
  `og:description` / `og:type=website`.
- Inline SVG favicon via `<link rel="icon" href="data:image/svg+xml,…">` — never an external
  image for the primary icon.
- **Single shared visual language.** Dark, calm, product-forward. The portfolio converged on
  one outline (ONYX's landing is the reference shape): a sticky frosted topbar with the brand
  mark + GitHub link, a hero with a kicker, `h1` (with one accent word in the platform's accent
  color), a lede, a primary + secondary CTA row, a quickstart shell block with a copy button,
  stats/cards, and a footer. New landings should match this outline rather than invent a new one.
- Palette is CSS custom properties under a single `:root` block (`--p-bg`, `--p-text`,
  `--p-accent`, …). The accent color is the *platform's* accent (Cyan/Indigo for Distro,
  Cyan for Atlas, Purple for Capstone/AthenIQ, Blue for Cerulean, Emerald for Magnate, Amber
  for Oasis, etc.) — not a per-repo random choice.
- Typography: system-ui stack, `ui-sans-serif, system-ui, "Segoe UI", Roboto, …`; mono for
  code: `"JetBrains Mono", ui-monospace, …`.
- No network requests to third parties for the core page (Google Fonts are used by Zeus only
  and are the documented exception; otherwise the page is fully offline-loadable).
- The page must not embed attribution to an AI tool or co-author. (See §5.)

**Landing vs README:** the landing is the public marketing face (GitHub Pages); the README is
the in-repo front door. They share the tagline voice but are not copies of each other.

---

## 5. Attribution guard — the one rule every repo enforces

**Policy:** no credit is given to anyone but the project owner
(`Darnel Hunter <dhunter@innotel.us>`) — in commit messages, PR titles/bodies, and in lines
added to any file. AI-assistant attribution ("Generated with/by X", co-author trailers, credit
lines) is rejected everywhere.

**Enforcement points (all three, every repo):**
1. **Local hooks** — `.githooks/commit-msg` + `.githooks/pre-commit`, installed via
   `git config core.hooksPath .githooks` (done by `setup.sh` / `bootstrap.sh`).
2. **CI** — `.github/workflows/attribution-guard.yml`, on `push` + `pull_request`, sourcing
   the shared `.githooks/guard-lib`.
3. **`guard-lib`** — `.githooks/guard-lib` is the single source of the policy patterns and is
   identical across repos (copy it verbatim; do not edit the patterns per repo).

**A conformant repo must have at least the CI workflow.** Having local hooks too is preferred
(better UX — fail before push).

**Rules:**
- The guard scans *content*, never author/committer identity.
- The guard must never reject itself — `guard-lib` must be free of literal attribution strings
  naming a tool or person other than the allowed ones.
- Plain product prose that names a technology ("OpenAI-compatible endpoint", "generated by the
  release pipeline") is **not** attribution and is allowed.

---

## 6. Operator workflow — `Makefile`

Every compose / compute repo has a `Makefile` with a `help` target that is the operator's
first stop. The portfolio signature:

- `help` — prints targets from `##` comments (the `awk` one-liner is the shared helper).
- The phony targets are grouped with comment banners: `## ---- Bootstrap ----`,
  `## ---- Core platform ----`, `## ---- Profiles ----`, etc.
- Common targets across repos: `setup` (preflight + hooks + .env secrets), `up`, `down`,
  `logs`, `ps`.
- Output is human-readable; commands use `##` doc comments so `make help` is always useful.

A repo without a `Makefile` is fine if it is not an operator-facing compute stack (e.g. a
pure landing/marketing repo). But any repo that you `docker compose up` for must have one.

---

## 7. License — declare it, retain upstreams

- Every conformant repo has a top-level `LICENSE`.
- The repo's own new material license is stated in the README's License section and is one of
  the portfolio's approved licenses: **MIT** or **AGPL-3.0-or-later** (AGPL for the platforms
  that build on AGPL upstreams: Atlas, AthenIQ, Signara, Onyx).
- Assembled/forked repos (Distro, Capstone, Monarch, etc.) keep upstream licenses in-tree and
  document attribution in `THIRD_PARTY_NOTICES.md` (or the README License section) — never
  re-license upstream material.
- The license file must be the **full canonical text** of the chosen license (verbatim AGPL-3.0
  or MIT body) — GitHub license detection returns NOASSERTION for short notices, which breaks
  the repo-page license badge. The conformity audit enforces this.

---

## 8. Environment template — `.env.example`

Every conformant repo that reads runtime env has a `.env.example` that is:

- A copyable template (never the real `.env`).
- Comment-heavy: every variable has a one-line purpose; secrets note how to generate them
  (`openssl rand -base64 …` / `openssl rand -hex …`).
- Never contains real secrets. Placeholder values like `change-me`, empty, or obviously fake.
- Documents the *shared platform services* the repo consumes, with the canonical example values
  where they exist (e.g. `OIDC_ISSUER_URL=https://auth.cerulean.innotel.us/application/o/<app>/`,
  `MAGNATE_URL=https://magnate.innotel.us`, `CERULEAN_DNS_API_URL=http://127.0.0.1:3003`).
- States the Infisical posture: production secrets come from Infisical (SecretOps); `.env` is
  derived / local-only; `.env` is gitignored.
- For repos that consume Magnate billing, documents `MAGNATE_URL` / `ENTITLEMENTS_API_TOKEN`
  (and that the token must equal Magnate's `ENTITLEMENTS_API_TOKEN`).
- For repos that consume Cerulean Authentik SSO, documents the OIDC vars and the redirect URI
  as the browser sees it.

---

## 9. Stack role — `docs/stack.md`

Every conformant product repo has `docs/stack.md` declaring its role in the Innotel Platform
Stack. Shape:

- Title: `<Name> in the Innotel Platform Stack`.
- One-line role: `**Role: <Classification>** — <one sentence>`.
- **Boundaries** — Owns / Consumes / Does not own. This is the heart of the doc.
- Service map (the things the platform *owns*), as a table: Component / Technology / Job.
- In the ecosystem — how identity, secrets, trust, revenue, edge, and source-of-truth flow for
  this platform.
- Where applicable: which other platform it integrates with (e.g. Distro ↔ Atlas).

Every `docs/stack.md` links back to the canonical
[Innotel Platform Stack](../README.md) repo so the ecosystem definition lives in one place.

---

## 10. CI posture

Conformant repos have at least:
- **Attribution guard** (§5) — mandatory.
- **A `ci.yml`** (optional but strongly preferred) that validates the repo's own concerns:
  compose config (`docker compose config --quiet` where applicable), commit-message / structure
  policy, syntax checks on the repo's authored code, landing sanity. CI must be green on `main`.

---

## 11. Secrets, hooks, and local development

- `.env` is gitignored everywhere.
- Guard hooks are installed by the repo's setup/bootstrap script (`git config core.hooksPath
  .githooks`).
- `setup.sh` / `bootstrap.sh` is the one-shot bootstrap where the repo has one: preflight,
  guard hooks, generated `.env` secrets, and (for Compose repos) first boot guidance.

---

## 12. Applying this standard

To bring a repo into conformity or start a new one, run:

```bash
./scripts/conform-project.sh <repo-dir>   # audit an existing repo
./scripts/conform-project.sh --new <name> <classification>   # scaffold a new conformant repo
```

For a *new app or project* you want in the stack going forward: scaffold with `--new`, then
fill in the platform-specific parts (compose services, code, docs/stack.md role). The shared
layers — README shape, landing, attribution guard, ci.yml, `.env.example` posture, license,
Makefile shape, `docs/stack.md` — come from this standard.

**Golden rules (short version):**
1. One job per platform; consume the platform services, never re-implement them.
2. Cerulean = auth/trust (Authentik SSO + DNS + TLS + PKI). Magnate = billing. Zeus = telephony.
   ONYX = storage. Infisical = secrets. NPM Edge = edge.
3. Shared layers are identical across repos: README shape, landing outline, attribution guard
   (CI + hooks + guard-lib), `docs/stack.md`, `.env.example` posture, license posture.
4. No secrets, no real IPs/hostnames, no AI attribution in any repo file or commit.
