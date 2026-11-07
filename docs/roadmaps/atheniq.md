# atheniq — roadmap

- **Job:** LearningOps
- **Classification:** business
- **Runs on:** server 1
- **Consumes:** Authentik, Cerulean Vault, Cerulean, ONYX, Magnate, Signara, NPM Edge
- **Target version:** 2.0.0

## What it owns

Course delivery, assessments, learner records and AI classrooms.

## 2.0 baseline — in force

- [x] **One job, written down.** This page names the single responsibility and the boundary beside it.
- [x] **Consumption, not embedding.** Authentik, Cerulean Vault, Cerulean, ONYX, Magnate, Signara, NPM Edge are consumed as platform services; no second copy is shipped here.
- [x] **Recorded placement.** server 1 is the recorded group placement, so a deployment is a lookup in this repository.
- [x] **One release path.** The product repo cuts releases with `scripts/release-repo.sh`, which tags, builds and attaches artifacts.
- [x] **Version agreement.** `VERSION`, `docs/versions.md` and this roadmap all say 2.0.0.

## Next on this line

- [ ] Emits completion evidence; Signara remains the only signer.

## Wiring

- Identity: Authentik (`auth.<platform>.innotel.us` is an edge alias, never a second IdP).
- Secrets: Cerulean Vault; `.env` carries `vault://` references and is derived.
- Edge: NPM Edge terminates TLS with the zone wildcard, issued by Cerulean over DNS-01.
- Billing: Magnate owns plans and entitlements; this product never prices a seat locally.
