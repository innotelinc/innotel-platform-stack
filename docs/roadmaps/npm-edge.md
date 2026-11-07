# npm-edge — roadmap

- **Job:** EdgeOps
- **Classification:** platform service
- **Runs on:** 192.168.1.71
- **Consumes:** Cerulean, Cerulean Vault
- **Target version:** 2.0.0

## What it owns

Public HTTP/S routing, TLS termination, proxy hosts and access lists.

## 2.0 baseline — in force

- [x] **One job, written down.** This page names the single responsibility and the boundary beside it.
- [x] **Consumption, not embedding.** Cerulean, Cerulean Vault are consumed as platform services; no second copy is shipped here.
- [x] **Recorded placement.** 192.168.1.71 is the recorded group placement, so a deployment is a lookup in this repository.
- [x] **One release path.** The product repo cuts releases with `scripts/release-repo.sh`, which tags, builds and attaches artifacts.
- [x] **Version agreement.** `VERSION`, `docs/versions.md` and this roadmap all say 2.0.0.

## Next on this line

- [ ] Keep the generated Nginx state recoverable and the wildcard-per-zone rule enforced.

## Wiring

- Identity: Authentik (`auth.<platform>.innotel.us` is an edge alias, never a second IdP).
- Secrets: Cerulean Vault; `.env` carries `vault://` references and is derived.
- Edge: NPM Edge terminates TLS with the zone wildcard, issued by Cerulean over DNS-01.
- Billing: Magnate owns plans and entitlements; this product never prices a seat locally.
