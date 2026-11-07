# Platform versions

Every product in the stack declares **2.0.0**. The number is the stack's compatibility
line, not a claim that the third-party images underneath share it: Authentik, Vault, NPM
Edge, PostgreSQL and the rest keep their own upstream versions and are pinned in
`groups/*.yml`.

The check is `scripts/check-versions.py`. It fails when a product in the table has no
roadmap, a roadmap targets a different version, or the table and `VERSION` disagree.

| Product | Classification | Group / host | Version | Roadmap |
|---|---|---|---|---|
| authentik | platform service | server 1 (Cerulean) | 2.0.0 | [roadmap](roadmaps/authentik.md) |
| cerulean-vault | platform service | server 1 (Cerulean) | 2.0.0 | [roadmap](roadmaps/cerulean-vault.md) |
| cerulean | platform service | 192.168.1.71 | 2.0.0 | [roadmap](roadmaps/cerulean.md) |
| onyx | platform service | server 4 | 2.0.0 | [roadmap](roadmaps/onyx.md) |
| magnate | platform service | server 1 | 2.0.0 | [roadmap](roadmaps/magnate.md) |
| npm-edge | platform service | 192.168.1.71 | 2.0.0 | [roadmap](roadmaps/npm-edge.md) |
| monarch | business | server 3 (192.168.1.56) | 2.0.0 | [roadmap](roadmaps/monarch.md) |
| zeus | business | server 2 (192.168.1.30) | 2.0.0 | [roadmap](roadmaps/zeus.md) |
| oasis | business | server 5 | 2.0.0 | [roadmap](roadmaps/oasis.md) |
| signara | business | server 1 | 2.0.0 | [roadmap](roadmaps/signara.md) |
| capstone | business | server 2 (192.168.1.30) | 2.0.0 | [roadmap](roadmaps/capstone.md) |
| rizzaura | business | server 4 | 2.0.0 | [roadmap](roadmaps/rizzaura.md) |
| zapit | business | on demand | 2.0.0 | [roadmap](roadmaps/zapit.md) |
| ontrak | business | 192.168.1.21 (incus ontrak) | 2.0.0 | [roadmap](roadmaps/ontrak.md) |
| atheniq | business | server 1 | 2.0.0 | [roadmap](roadmaps/atheniq.md) |
| atlas | business | server 5 | 2.0.0 | [roadmap](roadmaps/atlas.md) |
| distro | business | 192.168.1.61:20140 | 2.0.0 | [roadmap](roadmaps/distro.md) |
| plutus | business | server 3 | 2.0.0 | [roadmap](roadmaps/plutus.md) |
