# Cutting the AthenIQ LMS off the VPS — scope, not done yet

**Status: executed 2026-11-18 at 18:19 EST.** Nine edge hosts were repointed to
`192.168.1.59`, the VPS LMS was frozen and the VPS powered off once the sweep in §7 passed.
The plan below is kept exactly as it was written — including the order — because those
measurements are why it ran in one pass. Every number was read live between 17:00 and
18:30 EST.

It follows the `atheniq.institute` outage earlier the same day: the landing page was
restored on the estate (it had been stopped on the VPS for 24 h, and the edge had been
pointing at a container that never served 8085), and `atheniq-lander-probe.timer` now
checks the public URL and the lander origin every minute. That fix left the edge
deliberately on the estate copy, so the landing page is **not** part of this cutover.

---

## 1. What the VPS is actually carrying

Nine edge (`cerulean-npm`) proxy hosts still forward to `104.238.162.5`. Six of them are
one service — the Tutor LMS behind Caddy on `:18080`:

| Edge host | Forward | Domains |
|---|---|---|
| 229 | `:18080` | `meilisearch.lms.innotel.us` |
| 240 | `:18080` | `atheniq.courses` |
| 242 | `:18080` | `apps.atheniq.courses` |
| 243 | `:18080` | `studio.atheniq.courses` |
| 279 | `:18080` | `www.atheniq.courses` |
| 289 | `:18080` | `catalog.atheniq.courses`, `learn.atheniq.courses` |
| 241 | `:8088` | `zeus.innotel.us` + 8 subdomains (`zeus-landing`, static Caddy) |
| 245 | `:8087` | `workforce.atheniq.online` (`atheniq-workforce`, static Caddy) |
| 276 | `:8086` | `innotel.tech`, `www.innotel.tech` (`innotel-landing`, static Caddy) |

The box itself: Vultr `VC2`, **15 986 MiB** RAM (8 135 available), `/` 300 G at **62 %**
(176 G used), load ~0.95. Also on it, and unrelated to the cutover: `metrics-grafana`,
`metrics-prometheus`, `metrics-node-exporter`.

**There is no `vultr-cli` and no Vultr API key on the host.** Once it is powered off, only
the Vultr web panel can bring it back — which is the reason this is a plan and not a
command.

---

## 2. The good news: the estate already runs the same stack, with the same data

The `atheniq` container on i1 (`192.168.1.59`) is not an empty shell. It runs the identical
Tutor stack — `overhangio/openedx:22.0.2-indigo`, `openedx-mfe:22.0.0-indigo`,
`mysql:8.4.11`, `mongo:7.0.39`, `redis:7.4.10`, `meilisearch:v1.36.0`, `caddy:2.11.4` —
and its data matches the VPS row for row:

| Measure | VPS | estate `.59` |
|---|---|---|
| `openedx.auth_user` | 7 | **7** |
| `student_courseenrollment` | 5 | **5** |
| `course_overviews_courseoverview` | 120 | **120** |
| Mongo `modulestore.active_versions` (courses) | 120 | **120** |
| `certificates_generatedcertificate` | 4 | **4** |
| MySQL tables | 943 | **943** |
| Media files (`openedx-media` + `-private`) | 1 035 | **1 035** |
| Media on disk | 5.4 M | 6.9 M |
| Tutor stack resident RAM | ≈ 3.4 GiB | ≈ 2.8 GiB |
| Host RAM | 15 986 MiB, 8 135 free | 6 144 MiB, **2 642 free** |
| Host disk | 300 G, 176 G used (62 %) | 477 G, **26 G used (6 %)** |

The estate stack is already rendered for the right hostnames — `LMS_HOST=atheniq.courses`,
`CMS_HOST=studio.atheniq.courses`, `ENABLE_HTTPS=true` — and, asked with each `Host`
header, answers exactly as the VPS does:

| Host header | VPS `:18080` | estate `:18080` |
|---|---|---|
| `atheniq.courses` | 301 → `https://atheniq.courses/catalog/` | 301 → `https://atheniq.courses/catalog/` |
| `www.atheniq.courses` | 301 | 301 |
| `apps.atheniq.courses` | 302 | 302 |
| `studio.atheniq.courses` | 302 | 302 |
| `learn.atheniq.courses` | 200 | 200 |
| `catalog.atheniq.courses` | 200 | 200 |
| `/login` | 302 | 302 |
| `/catalog/courses` | 200 | 200 |

**So this is a cutover, not a data migration.** The expensive steps a migration would need
— dumping MySQL/Mongo, copying media, re-rendering Tutor for a new `LMS_HOST`, rebuilding
images — are not required. What remains is the edge, the freeze, and the leftovers.

### The one behavioural difference found

`GET /dashboard` with `Host: atheniq.courses`:

- VPS: `308` → `/dashboard/`
- estate: `302` → `/learner-dashboard`

Both are redirects to a dashboard, but they are not the same route, which means the two
MFE builds are not at the same revision. Confirm which destination is correct before
cutting over, so the difference is a decision rather than a surprise.

---

## 3. The four real risks

1. **Two live LMS instances on one dataset.** The estate copy is a full clone, so any write
   accepted by the VPS after the cutover is lost, and vice versa. The VPS stack must be
   **stopped** at cutover, not merely unreachable through the edge — its containers are
   `restart: unless-stopped` and come back on every reboot.
2. **Media is equal in count but not in bytes** (1 035 files each; 5.4 M vs 6.9 M). Neither
   side is provably a superset. A one-way `rsync` VPS → estate is cheap at this size and
   removes the question.
3. **Three non-LMS sites have no estate home yet.** `workforce.atheniq.online` is easy — the
   estate container already runs `atheniq-workforce` on `:8087`. `innotel.tech` has a compose
   service (`innotel`) defined but not started. **`zeus.*` (9 names) has no estate
   counterpart at all.** All three were placed on the estate before the cutover — §7.
4. **The estate host is smaller.** 6 144 MiB total with 2 642 available while the LMS stack,
   OpenMAIC, `classroom-sso`, `generation-sso` and `atheniq-sso-sessions` already run. The
   LMS itself is ~2.8 GiB, so it fits today — but this is the container that has to absorb
   it, and nothing adds headroom.

---

## 4. Cutover, in order

Every step has a verification; none is "run it and hope".

1. **Back up the VPS side** (both databases and media, to `/root` on the VPS):
   `mysqldump openedx`, `mongodump --db openedx`, `tar` of `openedx-media{,-private}`.
   Verify the dump header/table count, not just that the command exited 0.
2. **Sync media VPS → estate** with `rsync -a` over SSH, so the byte difference in §2 is
   resolved in one direction and counted afterwards (1 035 files expected).
3. **Freeze writes**: `tutor local stop` on the VPS. This is the actual downtime window —
   minutes, not hours.
4. **Re-verify parity** on the stopped VPS against the live estate: the five counts from
   §2 must match. A difference here means a dump restore is needed before continuing.
5. **Repoint the six LMS edge hosts** (229, 240, 242, 243, 279, 289) from
   `104.238.162.5:18080` to `192.168.1.59:18080`, in **both** places — the `proxy_host`
   row in the edge's MySQL and the rendered `/data/nginx/proxy_host/<id>.conf` — then
   `nginx -t` and `nginx -s reload`. This is exactly the pattern used for host 206 earlier
   today; a database-only change is reverted the next time NPM re-renders.
6. **Verify from outside**: `https://atheniq.courses/` 301 → `/catalog/`,
   `/catalog/courses` 200, `/login` 302 → `/authn/login` 200, plus `studio.`, `apps.`,
   `learn.`, `catalog.` and `www.` — and confirm which `/dashboard` behaviour you decided
   in §2.
7. **Repoint `workforce.atheniq.online`** (245) to `192.168.1.59:8087`; start the estate's
   `innotel` service and repoint 276; decide and place `zeus.*` (241).
8. **Leave the VPS powered on and stopped** for a rollback window (suggest a week), then
   power it off in the Vultr panel. Stopped containers do not restart on their own; a
   *power-off* is the only state where they cannot come back at all.

### Rollback

Repoint the six hosts back to `104.238.162.5:18080` (DB + rendered conf + reload) and
`tutor local start` on the VPS. Because the datasets are identical and the freeze in step 3
happens before the edge moves, rollback loses only the writes made on the estate during the
window — which is why step 3 precedes step 5 and not the other way round.

---

## 5. What must be true before the VPS is powered off

- **Zero edge hosts** forward to `104.238.162.5` (today: 9).
- ~~**No DNS record** resolves a live name straight at `104.238.162.5`~~ — verified: all 20
  public names resolve to the edge (`73.68.203.71`), none to the VPS. This was the check
  most likely to be forgotten, and it turned out to be clean.
- The VPS Tutor stack is stopped, not merely unrouted.
- A backup of MySQL, Mongo and media exists somewhere other than the VPS.
- Someone can reach the **Vultr panel**, since nothing on the box can turn it back on.

---

## 6. Not verified

- **Whether any client reaches the LMS by address** rather than by name (a bookmark, a
  CronJob, an SSO callback pinned to the IP). The edge inventory proves what the *edge*
  does; it cannot prove what the rest of the estate does.
- ~~**`zeus.*`'s home.**~~ Resolved in §7: the page was copied with a matching digest and
  given a compose service on `:8088`.
- **The `/dashboard` difference** — measured, not explained.
- ~~**The media byte difference**~~ Resolved in §7: equal file counts hid files each side
  held and the other did not, plus five files that differed in content.
- **Long-term fit on `.59`.** The LMS fits in 2 642 MiB available today; no measurement was
  taken of the estate stack under real learner traffic, because there has not been any.
- ~~**Nothing here was executed.**~~ This page is now a record, not a proposal — see §7.

---

## 7. What was actually done (2026-11-18, 18:19 EST)

Measurements first, backups second, freeze third, edge last — the order §4 asked for, and
the reasons it held:

1. **Backups left the box before anything was touched.** `mysqldump --databases openedx`
   (13.9 MB, 611 base tables, ends in `-- Dump completed`), `mongodump --db openedx
   --archive --gzip` (32 MB, confirmed readable with `mongorestore --dryRun`), and a media
   tar — all pulled to the edge host at `/root/atheniq-vps-backup-20261118/` and checked
   against a `SHA256SUMS` written on the VPS. Two transfers truncated a tarball; the third
   was digest-matched end to end, which is the whole reason the check exists.
2. **Media was reconciled, not assumed.** Equal file counts (1 035) hid real divergence:
   each side held files the other did not, and five files differed in content. The VPS set
   — what visitors were actually being served — was merged in. The estate now holds every
   VPS file with zero mismatches, plus 70 estate-only files kept rather than deleted.
3. **`zeus.*` and `innotel.tech` were given estate homes.** Both are single-page static
   sites. `web/zeus/` and `web/innotel/` were copied with digests matching the VPS exactly;
   `innotel` started from the compose service that already existed (`:8086`); a `zeus`
   service was added (`:8088`, catch-all `file-server`, `mem_limit: 128m`,
   `restart: unless-stopped`) with `ZEUS_PORT` in `.env.example`, and
   `docker compose config -q` validated before it was started. `web/workforce/` was
   refreshed from the VPS copy so the workforce site did not quietly change content.
4. **Freeze before repoint.** LMS, CMS, both workers and the Tutor Caddy were stopped; the
   databases were left running so counts could still be read. Parity on the frozen VPS:
   users 7, enrollments 5, overviews 120, certificates 4, mongo `active_versions` 120,
   611 base tables — identical to the estate, as §2 predicted.
5. **Nine hosts repointed in both places.** `proxy_host.forward_host` for 229, 240, 241,
   242, 243, 245, 276, 279 and 289 (9 rows), the rendered `*.conf` files patched with
   timestamped `.bak` copies, `nginx -t` clean, reload done. The edge now reports **0 hosts
   forwarding to `104.238.162.5`**.
6. **Verified from outside**, through the public names: `atheniq.courses` 301 →
   `/catalog/`, `/catalog/courses` 200, `/login` 302 → `/authn/login`,
   `www.`/`apps.`/`studio.`/`learn.`/`catalog.` unchanged in behaviour, `zeus.innotel.us`
   200, `workforce.atheniq.online` 200, `innotel.tech` 200,
   `meilisearch.lms.innotel.us` 200.
7. **The probe now covers four checks** — `lander_public`, `lander_origin`,
   `courses_public`, `courses_origin`, the last asking the container directly with
   `--resolve` and the real host header so an edge misroute and a dead LMS cannot be
   mistaken for each other. New alert `AtheniqCoursesDown`, same Alertmanager receiver.
   Tested against a dead port first: fires once, stays silent on a repeat, resolves.
8. **The VPS was powered off** after the sweep confirmed zero edge hosts, no DNS name
   pointing at it, and the backups present off-box. Its disk is intact; only the Vultr
   panel can turn it back on.

Still true from §6: the `/dashboard` route difference was measured but never explained, and
nothing has measured the estate stack under real learner traffic.
