# Product roadmaps

One roadmap per product in the stack, each ending on the same compatibility line:
**2.0.0**. The common version is a statement about this stack's contract — one identity
source, one secrets store, one billing plane, one edge — not about the upstream versions
pinned inside `groups/*.yml`.

## The 2.0 line

A product is on 2.0 when all five hold:

1. **One job, written down.** The platform's single responsibility is stated, and what it
   must not own is stated beside it.
2. **Consumption, not embedding.** What it consumes from the platform services is listed;
   it does not ship a second identity, secrets, billing, storage or edge.
3. **A recorded placement.** The group and host that run it are in this repository, so a
   deployment is a lookup rather than a memory.
4. **One release path.** The product repo cuts releases through
   `scripts/release-repo.sh`; images are traceable to a commit.
5. **The version agrees.** `VERSION`, this index, and the product's roadmap all say
   2.0.0.

## Products

| Product | Job | Roadmap |
|---|---|---|
| authentik | IdentityOps | [authentik](authentik.md) |
| cerulean-vault | SecretOps | [cerulean-vault](cerulean-vault.md) |
| cerulean | TrustOps | [cerulean](cerulean.md) |
| onyx | StorageOps | [onyx](onyx.md) |
| magnate | RevenueOps | [magnate](magnate.md) |
| npm-edge | EdgeOps | [npm-edge](npm-edge.md) |
| monarch | MediaOps | [monarch](monarch.md) |
| zeus | VoiceOps | [zeus](zeus.md) |
| oasis | MailOps | [oasis](oasis.md) |
| signara | DocumentOps | [signara](signara.md) |
| capstone | AgentOps | [capstone](capstone.md) |
| rizzaura | CommunityOps | [rizzaura](rizzaura.md) |
| zapit | TransferOps | [zapit](zapit.md) |
| ontrak | TrainingOps | [ontrak](ontrak.md) |
| atheniq | LearningOps | [atheniq](atheniq.md) |
| atlas | CodeOps | [atlas](atlas.md) |
| distro | BuilderOps | [distro](distro.md) |
| plutus | VideoOps | [plutus](plutus.md) |
