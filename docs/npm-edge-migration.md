# NPM Edge — moved to Group 1, running on the primary host

**Status: cutover complete (2026-09-15).** The edge serves live traffic from the
primary host on its real ports, the full certificate set has been re-issued as
wildcards by Cerulean, and `.71` is out of the wiring — nothing resolves,
routes or forwards to it. Two follow-ups remain, both owner actions: the
`rizzaura.net` delegation at the registrar (§5) and physically powering `.71`
off (§6).

## 1. What moved

| | Before | Now |
|---|---|---|
| Repo | `3-media/npm` (Group 3, MediaOps) | `1-primary/npm` (Group 1) |
| Runs on | `192.168.1.71` — a separate edge box | this host (`192.168.1.46`), with Cerulean |
| Cerulean mode | `NPM_MODE=remote` → `http://192.168.1.71:81` | `NPM_MODE=local` → `cerulean-npm:81` in-network, `127.0.0.1:81` from the host |
| Platform NPM credential | the `.71` admin account | the local instance's admin (`NPM_EMAIL`/`NPM_PASSWORD`) |
| Edge ports | `:80/:443` on `.71` | `:80/:443` on `.46` — **real ports since the cutover**, staged `2280` retired |

The group move is not cosmetic: Group 1 is the trust layer, and the edge
terminates the certificates Cerulean issues, so the two sit beside each other
and Cerulean's `include` resolves `../npm/compose.cerulean.yml`. The mesh
roster, the hostname heuristic in `scripts/mesh.sh`, and the subscriber-page
sync's group map all follow (`ips` commit *feat(mesh): NPM Edge joins group 1*);
`scripts/sync-mesh.sh --check` reports no drift.

## 2. Replicated configuration

The new edge holds the live edge's configuration:

- **175 / 175 proxy hosts**, with routing, TLS flags, websocket/HTTP-2 settings,
  `advanced_config` (including every forward-auth `auth_request`) and locations.
- **20 / 20 `provider: other` certificates**, material included: their PEMs were
  written to `1-primary/npm/data/custom_ssl/npm-<id>/` and attached to the 23
  hosts that used them.

Two NPM behaviours worth knowing, both hit during the migration:

1. `POST /api/nginx/certificates` **stores the row but does not write the PEM
   files** — only the upload path (`/nginx/certificates/:id/upload`) calls
   `writeCustomCert`. The files have to be materialised (or uploaded) separately;
   Cerulean's `importCertificate()` already does both calls.
2. A partial `PUT /api/nginx/proxy-hosts/<id>` **updates the database but does
   not regenerate the nginx config** — full bodies are required for any edit
   that should take effect (the cutover's port repoints used full PUTs and
   verified the stored result).

## 3. Certificates — solved by re-issuing as wildcards

The 76 Let's Encrypt certificates on `.71` could not be copied: NPM's API never
returns the private key for a `letsencrypt` certificate. On 2026-09-15 the whole
public name set was re-issued as **wildcards via Cerulean** (Technitium DNS-01),
which also matches the convergence direction — NPM should not be issuing
certificates at all; Cerulean issues, imports and attaches.

Issued (rows in Cerulean's `certificates` table, names `cerulean-<domain>-wildcard`):

- `*.innotel.us` and `innotel.us`, `*.rizzaura.net` and `rizzaura.net` (apexes failed first pass — see §5)
- 17 sub-zone wildcards covering every multi-level host family:
  `*.cerulean` `*.zeus` `*.capstone` `*.monarch` `*.onyx` `*.rizz` `*.atheniq`
  `*.atlas` `*.distro` `*.learn` `*.magnate` `*.oasis` `*.olympus` `*.plutus`
  `*.rizzaura.innotel.us` `*.signara` `*.zapit` `*.internal` (all `.innotel.us`)

Each issuance ran through `jobs.runIssueJob` — the production path — so
material is saved in Cerulean, imported into NPM as a custom certificate, and
attached to every matching host (`NPM_WILDCARD_ATTACH=1`, never overriding a
host's existing certificate). Renewal is Cerulean's 12-hour renewal sweep; the
one-time rate-limit concern disappears because the whole zone rides ~20 certs,
not 76.

**Not covered** (verified outside our DNS authority — Cerulean cannot answer
their DNS-01 challenges): `cattape.us`, `fomocoin.one`, `denovocredit.com`
(incl. `pi.` and `www.`) and `backend.api.capstone.innotel.us`. These stay
HTTP-only until their DNS moves to Technitium, or get an HTTP-01 cert per host
(`:80` now lands on this edge, so that path works). This is the doc's old
"option 3" residue, now down to a handful of hosts.

## 4. The cutover — done

1. ~~**Router:** forward `:80` and `:443` to `192.168.1.46`.~~ — verified
   already in place (public `:443` terminated by the new edge, `:80` answering
   on `.46`).
2. ~~**Free port 80.**~~ — done: `pbx.innotel.us`, `pbx.zeus.innotel.us` and
   `fax.zeus.innotel.us` (edge host ids 108/109/77) repointed to `:8083` with
   full PUTs, then `80:80` dropped from `2-voice/zeus/docker-compose.full.yml`.
   `zeus-freepbx` now publishes **`8083:80` only**, and the zeus-portal's
   `FREEPBX_URL`/`AVANTFAX_URL` were repointed to `:8083` in the same change
   (the portal dials the PBX GUI over LAN HTTP; leaving them on the implicit
   `:80` would have broken it at step 3).
3. ~~**Real ports.**~~ — done: `NPM_HTTP_PORT=80` in `1-primary/cerulean/.env`,
   `cerulean-npm` recreated; binds `:80`, `:443`, `:81` (+ backup UI `:82`).
4. ~~**Verify.**~~ — verified over WAN and loopback: SNI handshakes serve the
   expected certificates, HTTP-01 challenge paths answer through the real
   `:80`, and the zeus stack is healthy after the port change.

Rollback is no longer trivial (the old edge's ports no longer matter — it has
been out of the routing path since step 1), but nothing about the DNS depends
on `.71`: no internal `A` records pointed at it, and no checkout env references
it any more.

## 5. Remaining: rizzaura.net delegation (owner action)

The `rizzaura.net` wildcard and apex orders failed **at Let's Encrypt** ("No
TXT record found at `_acme-challenge.rizzaura.net`") while every innotel.us
order succeeded. Cause, confirmed with `dig +trace`:

- The **`.net` registry still delegates `rizzaura.net` to
  `ns1/ns2.hosting.businessidentity.llc`** (the old registrar DNS).
- Our `ns1/ns2.innotel.us` NS records exist only *inside* the zone — visible to
  anyone who already queries our servers, invisible to the delegation walk.
- So LE's challenge TXT lands on Technitium (authoritative per the zone's own
  NS set) but the public never asks us: the world resolves rizzaura.net names
  from the old provider's stale zone.

**Fix:** at the rizzaura.net registrar, set the domain's nameservers to
`ns1.innotel.us` / `ns2.innotel.us` (the same delegation pattern `.us` already
serves for innotel.us). Then re-run the idempotent bulk script — only the two
`error` rizzaura rows will issue, and their 9 hosts get attached automatically:

    docker cp 1-primary/cerulean/scripts/npm-bulk-wildcard-reissue.js cerulean:/tmp/
    docker exec cerulean node /tmp/npm-bulk-wildcard-reissue.js

(The rows already exist in Cerulean with status `error`; equivalently,
Certificates → Renew in the portal re-issues just those two.) Until then the 9
rizzaura.net hosts stay HTTP-only; everything else on the edge has a
certificate attached.

## 6. Retiring .71 — wiring is gone, power-off is owner action

Nothing in the repo, the env files, the edge's 175 proxy hosts, or the mesh
roster references `192.168.1.71` any more. The only stale reference found was a
Next.js build artifact (`2-voice/zeus/.next/standalone/.env`) — build output,
not configuration; regenerated on the next build. The old edge's backup-ui
credentials (`BACKUP_UI_USER`/`BACKUP_UI_PASSWORD`) were never in any checkout
or in git history, so the option-1 full-copy was never possible — and is no
longer needed, since §3 re-created everything the platform owns.

Owner actions on `.71`:

1. Verify no console-of-one service still points there (audit on 2026-09-15
   found none).
2. Power the box off. Keep it (or its disks) for a cooling-off period; it holds
   the only copy of the original 76 LE keys, which are now irrelevant — but
   `/data/nginx/proxy_hosts` on it is the pre-migration audit trail.

(DNS is unaffected: `ns1/ns2.innotel.us` resolve to this site's public WAN IP
— `73.68.203.71`, the router — which forwards `:53` to Technitium on this host.
The `.71` lookalike in that address is the WAN octet, not the retired box.)

## 7. Gotcha hit on the way: stale container env silently detaches the edge

The bulk issuance initially ran with `docker exec cerulean node ...`, whose
process env came from **when the container was created** — before this
checkout's `.env` was rewritten to `NPM_MODE=local`. The app therefore synced
every import/attach to `http://192.168.1.71:81` — the OLD edge, which was still
up and happily accepted it (its admin answered 200 all along). Nothing on the
new edge moved, and no error surfaced: Cerulean logs npm-sync failures only to
its activity log, and these were not even failures.

Diagnosis that cracked it: `docker exec cerulean node -e 'console.log(require("/app/server/dist/config.js").config.npm)'`
showed `{mode:"remote", apiUrl:"http://192.168.1.71:81"}` while `.env` said
`NPM_MODE=local`. Fix: `cd 1-primary/cerulean && docker compose up -d cerulean`
(recreate with current env), then re-run the attach leg of
`scripts/npm-bulk-wildcard-reissue.js` — idempotent, refreshed the 10
certs already imported on `.71` in place on the new edge and attached **114
hosts** in one pass.

Lesson, now policy: **any `.env` change requires recreating the containers that
read it** (`docker compose up -d <service>`), and after any cert/attach bulk
operation, verify from the target's API (NPM `/api/nginx/certificates`), not
from the issuer's logs alone.

Final state after the re-run: **162 / 175 proxy hosts carry a certificate**
(55 before), 43 custom certificates in NPM (33 originals + 20 new, 10 of them
refreshed in place by the re-attach), residue of 13 HTTP-only hosts (9
rizzaura.net — §5 — plus the 4 outside our DNS authority from §3).

## 8. Also found on the way (historical)

`https://distro.innotel.us` was **live-broken (502)** at migration time:
retiring the bolt.diy front door removed the `:5173` upstream, but the edge
route still pointed at it. Both edges now send it to the Distro control plane
(`:20140`), matching `cp.distro.innotel.us`. The audit also listed 19 hosts
whose upstreams on this host are simply not running (the ONYX, Signara, AthenIQ
and Atlas stacks), and 25 more that belong to other machines — faithful copies
of the live edge's state, not migration defects.
