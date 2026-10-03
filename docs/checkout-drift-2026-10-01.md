# Stack checkout drift — 2026-10-01

Every service container keeps **its own copy** of the monorepo (or its own
standalone `/opt/<product>` checkout); they are not a shared mount. Those live
copies drift from the dev box (`dev`, 192.168.1.74), which is where changes are
authored. This records where they have drifted, so a reconciliation can be done
deliberately rather than by guessing.

## Why this is a *merge*, not a copy

Both sides carry commits the other does not. The dev box and the live checkouts
are on **independent lines** off the same ancestry, and both usually have
uncommitted work:

| Repo | Live HEAD | Dev HEAD | Read |
| --- | --- | --- | --- |
| zeus | `b440628` *Say when an adopted extension carries no secret…* | `2a5cb37` *Let a machine client in, scoped…* | divergent lines |
| capstone | `d5a87c1` *Clean up the row the collision pass creates…* | `150982c` *Adopt the Unity theme on the landing page…* | divergent lines |
| monarch | `f5fc7f1` *Update Seerr to 3.5.0…* | `b3db762` *Drop the tiles of the products that are gone…* | divergent lines |
| plutus | `75658e1` | `f02553f` | divergent |
| onyx | `4bdf74f` (detached `HEAD`) | `ead51ef` | divergent **and** detached |
| distro | `61182c6` (`/opt/distro`) | `155ef50` (`5-dev/distro`) | divergent |
| genesis | `ef781de` (`/opt/genesis`) | `bea4049` (`1-primary/genesis`) | divergent |
| ips | `40e308c` *groups: make the manifests say what each host actually runs* | `3e0bdb3` *Move genesis to the app host…* | divergent lines |
| atlas | not a git checkout (`/opt/atlas`) | `b3ef08f` (`5-dev/atlas`) | live is a plain copy |
| rizzaura | not a git checkout (`/opt/rizzaura`) | `36d6f54` | live is a plain copy |

Copying one tree over the other would silently drop the other side's commits and
uncommitted work — some of it live-only (e.g. the zeus PBX extension-adoption
work lives only on the live checkout; the Unity-theme work only on dev). So the
reconciliation belongs on a **shared remote**: push both sides, merge, then have
each host pull.

## Locations (so a merge can find them)

| Service | Host | Container | Path |
| --- | --- | --- | --- |
| zeus | i2 | `capstone` | `/usr/src/projects/complete/2-voice/zeus` |
| capstone | i2 | `capstone` | `/usr/src/projects/complete/2-voice/capstone` |
| monarch | i1 | `monarch` | `/usr/src/projects/complete/3-media/monarch` |
| plutus | i1 | `monarch` | `/usr/src/projects/complete/3-media/plutus` |
| onyx | i3 | `onyx` | `/usr/src/projects/complete/4-social/onyx` |
| genesis | i2 | `genesis` | `/opt/genesis` |
| distro | i3 | `distro` | `/opt/distro` |
| ips | i1/i2 | `monarch` / `capstone` | `/usr/src/projects/complete/ips` |
| atlas | i2 | `atlas` | `/opt/atlas` (no `.git`) |
| rizzaura | i2 | `rizzaura` | `/opt/rizzaura` (no `.git`) |

The monorepo root `/usr/src/projects/complete` is **not** a git repo; each
service subdirectory under it is its own repo. `/opt/atlas`, `/opt/rizzaura`
and `/opt/genesis` are standalone copies.

## Dirty working trees

Live checkouts carry uncommitted changes that a copy-over would destroy. The
largest, at the time of this report:

- **zeus (live)**: 20 entries — `pbx/asterisk_converge.py`,
  `pbx/outbound_route.py`, `pbx/bootstrap-zeus-pbx.sh`, their tests,
  `scripts/zeus-pbx-sync.sh`, `docker-entrypoint.sh`, `docker-compose*.yml`;
  untracked `pbx/ari_conf_guard.py`, `pbx/rtp_settings_guard.py` and tests.
- **capstone (live)**: 11 — `scripts/npm-proxy-hosts.py`,
  `pbx/asterisk_converge.py`, `docs/zeus-integration.md`, and an untracked
  `docker-compose.yml.bak-kokoro-threads-…`.
- **monarch (live)**: 10 — `docker-compose.yml`, `docs/operations.md`,
  `scripts/drift-check.sh`; untracked clipbucket sync scripts/timers.
- **onyx (live)**: `docker-compose.yml` on a detached HEAD.

The dev side is similarly dirty (the session's own edits: `ips/docs/`,
`ips/hosts/`, zeus/atlas docs and scripts, monarch compose/docs).

## What was reconciled directly (safe, additive)

These are new or clearly-newer tracked files pushed to the live checkout during
the session, leaving live-only work untouched:

- zeus: `scripts/compose-vault.sh`, `scripts/vault-env-file.mjs` (new),
  `docker-entrypoint.sh` `VAULT_KEYS`, `compose.gateway-vault.yml`, `.env`
  (references), `.env.example`.
- atlas: `.env.example` (declared the six Vault-owned keys as references; the
  live copy was an older `.46`-era revision).
- monarch: `docker-compose.yml` (qbittorrent pin + healthcheck).

## Recommendation

1. Stand up (or point at) one reachable remote per repo and push **both** the
   dev and each live checkout's branch, so the divergence is visible.
2. Merge deliberately — the live-only PBX work and the dev-only Unity work are
   both wanted; neither is stale.
3. For `atlas` and `rizzaura`, which are plain copies with no `.git`, convert
   them to clones (or accept them as deploy artifacts and keep them derived).
4. Add a periodic drift check: compare `git rev-parse HEAD` and `git status
   --porcelain` per service against an expected revision, and alert on change.
   `scripts/drift-check.sh` (monarch) already does this for live-stack
   invariants; a checkout-HEAD variant would close this gap.
