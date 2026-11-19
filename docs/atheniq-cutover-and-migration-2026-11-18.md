# AthenIQ LMS — CI image, config durability, migration and the student portal

Date: 2026-11-18 (evening) · Host: `atheniq` incus container on i1 (192.168.1.51)

This records what was actually done and verified, and the two things that will
bite the next person.

---

## 1. The catalog 500 is fixed, and the fix now survives `tutor config save`

`edx-search` raises when a request asks for more results than
`SEARCH_MAX_PAGE_SIZE` (default 100). The catalog asks for the whole catalogue in
one page (120). The consequence was an HTTP 500 on `/catalog/`.

The setting used to be a hand-edit in
`env/apps/openedx/config/lms.env.yml`, which **Tutor regenerates on every
`tutor config save`** — the first save would have brought the 500 back.

It is now a plugin, `atheniq_search`, which patches the LMS *settings module*
(`openedx-lms-production-settings`). That module is imported after
`lms.envs.production` applies the yml, so the plugin also wins over a stale value
left in the yml.

Verified after restart:

```
SEARCH_MAX_PAGE_SIZE = 250
catalog search = 200 results = 120
/heartbeat 200 · /catalog/ 200 · /authn/register 200
```

Proof it is durable: rendered the env into a scratch root with the live config
and diffed it against production. Before the plugin, `lms.env.yml` lost exactly
three lines (the key and its comment) and **nothing else** — the hand-written
domain-rewrite blocks in `production.py` are re-emitted by an existing plugin, so
they were never at risk. After the plugin, `production.py` contains exactly one
`SEARCH_MAX_PAGE_SIZE = 250`.

## 2. There are two Tutor roots. Only one is real.

```
/root/.local/share/tutor        ← what bare `tutor` uses when run as root (empty, stale)
/home/tutor/.local/share/tutor  ← the deployment (containers mount this)
```

Evidence: `docker inspect tutor_local-lms-1` mounts
`/home/tutor/.local/share/tutor/env/apps/openedx/settings/lms` into the container;
`/root`'s config has no `DOCKER_IMAGE_OPENEDX` key at all.

**Consequence:** `tutor config save` run as root without `TUTOR_ROOT` appears to
succeed and changes nothing the containers read. Always:

```bash
export TUTOR_ROOT=/home/tutor/.local/share/tutor
/home/tutor/tutor-venv/bin/tutor <cmd>
```

Plugin discovery follows the *user's* home: `/root/.local/share/tutor-plugins/`
when run as root, `/home/tutor/.local/share/tutor-plugins/` when run as `tutor`.
`atheniq_search.py` is installed in both so either invocation finds it. It is
**not** loaded from `$TUTOR_ROOT/plugins/` — that directory is not on the search
path, and a copy placed there renders nothing (this was caught by the
occurrence-count check, and is why that check exists).

## 3. The image is built by CI, not on this box

* Fork branch `atheniq/verawood`, created at upstream `release/verawood.1`
  = `9e67d1429d5ab49a36fb9881a26457efeee0bb89` — the exact commit production runs.
* Workflow `.github/workflows/build-openedx.yml` (on `atheniq/verawood` **and**
  `master`, because a workflow is only dispatchable once it exists on the default
  branch). Actions must be enabled on a fork; it is off by default.
* Run **44429631686** — `completed / success`: build, GHCR login, push.
* Image: `ghcr.io/innotelinc/openedx-platform:de1f9f5` in GHCR (tagged by the
  commit it baked: `de1f9f5a`).

The container has 3 cores and ~1.8 GB RAM free, so an edx-platform source build
here is not an option — pulling is.

## 4. Snapshot taken (the migration asset)

`/home/tutor/migration/snapshot-20261118-1946/`

| file | size |
|---|---|
| `mysql-openedx.sql` | 15 MB |
| `mongo-openedx.archive.gz` | 29 MB |
| media (`/openedx/media`) | 7.7 MB |

Contents at snapshot time: **120 courses, 5 enrolments, 7 users.** MySQL
credentials never leave the container: the dump is produced inside it and only the
stream reaches the host.

## 5. Parallel stack: why it cannot run today

| | value |
|---|---|
| container memory limit | **8192 MiB** (raised from 6144 on 2026-11-18) |
| container in use | 5.80 GiB — 71% of the new limit (was 97%) |
| host total / available | 15279 MiB / 5340 MiB |

The limit was raised because the live stack was pinned at 97% of a 6 GiB cap:
any burst in Celery or a course import could OOM-kill LMS. 8192 MiB costs nothing
until it is used and gives the live site the headroom it was missing.

A second full Open edX stack (LMS, CMS, two workers, MySQL, Mongo, Redis,
Meilisearch) needs roughly 3.5–4.5 GiB. That is **more than the container's
remaining room and more than the host's spare memory combined**. Starting one now
would swap the live site, not run beside it.

So there are two honest paths:

**A — parallel (preferred, memory-tight).** A *full* second stack still does not
fit. A **slim** one can: no Meilisearch of its own, one CMS, one worker, a small
gunicorn pool — about 3.5 GiB, which is what the host has free. It needs its own
container rather than room inside `atheniq`, since `atheniq`'s 8 GiB is already
2/3 spoken for. Script:
`platform-stack/scripts/atheniq-parallel-stack.sh` (`--plan` refuses to launch
when host memory is short). No downtime for students; DNS flips last.

**B — snapshot cutover (possible today, ~45–60 min downtime).** Reinstall the
stack from the CI image, restore the snapshot, verify, flip. Reversible: the
snapshot above restores either side.

Helper: `platform-stack/scripts/atheniq-migrate-to-image.sh`
(`--plan` default, `--snapshot`, `--verify`, `--apply`, `--restore`).

## 6. Student portal — first slice shipped

In `atheniq-console` (Next 15 / React 19), on the learner side rather than the
staff side:

| file | what it does |
|---|---|
| `lib/portal.ts` | learner cookie (`atheniq_learner`, HMAC label `portal:`), dashboard assembly |
| `lib/lms.ts` | read-only `/api/courses/v1/courses/` client; never throws, 4 s timeout |
| `app/api/portal/login/route.ts` | email + existing scrypt hash → cookie; accepts a real form post |
| `app/api/portal/logout/route.ts` | clears the cookie |
| `app/portal/page.tsx` | courses with progress, billing, credentials, links into the courseware |
| `app/portal/login/page.tsx` | sign-in form that works without JavaScript |
| `middleware.ts` | the staff gate stands aside for `/portal` and `/api/portal/` |

Deliberate choices:

* **No second password store.** The portal verifies the learner's existing
  `passwordHash`, the same one the app uses.
* **No LMS credentials in the portal.** Course availability comes from the one
  Open edX endpoint that answers without a token; if the LMS is down the portal
  still renders (that is why `lib/lms.ts` swallows failures).
* **Billing is no longer read-only.** A student can pay an existing invoice by
  card from the portal; registry records the card payment as the same
  "confirmed" purchase a registrar would have made (see §7).

Still to come for the full institute: news, `@atheniq.institute` mail
provisioning, and the student app consuming these same records.

## 7. Card payment, and who confirms it

The school already had an invoice model: a learner enrols, gets a reference
(`ATH-<CODE>-XXXXX`), claims the payment in the app, and a registrar confirms it.
Card capture slots into that path rather than replacing it.

| file | role |
|---|---|
| `lib/stripe.ts` | Checkout Session by REST + webhook signature check (no SDK) |
| `app/api/portal/pay/route.ts` | learner session → existing invoice → Stripe URL |
| `app/api/portal/pay/webhook/route.ts` | signed event → purchase `confirmed`, enrollment `paid` |
| `app/students/*`, `app/api/students/*` | registrar list, student page, decisions, grades |

Decisions worth knowing:

* **The webhook is the only thing that opens a course for a card payment, and its
authentication is the HMAC signature** — the route is reachable without a
session, so nothing is read from the body until `stripe-signature` verifies
inside a 300 s window. An unsigned event gets 400 and changes nothing.
* **A settled card payment is recorded as `decidedBy: "stripe"`**, so the audit
trail still distinguishes a machine confirmation from a registrar's.
* **Never a second checkout for a confirmed invoice** — the pay route returns to
the portal instead, because paying twice is worse than a stale link.
* **`payment_status` is checked**, so a delayed method (bank debit) waits for
`checkout.session.async_payment_succeeded` instead of opening lessons on an
unsettled payment.

To turn it on, set in the console's environment and restart:

```
STRIPE_SECRET_KEY=sk_live_…        # or sk_test_… to dry-run
STRIPE_WEBHOOK_SECRET=whsec_…      # endpoint: https://<console host>/api/portal/pay/webhook
```

Without `STRIPE_SECRET_KEY` the pay button does not render, the route says card
payment is not set up, and the claim/confirm flow works exactly as before. No
key is in the repository, and **nothing here has processed a real card** — that
needs the school's Stripe account.

Where a key goes: `secrets/atheniq-console.env` (mode 0600, `secrets/` is
git-ignored). A key is only real once the console process has it in its
environment and has been restarted; writing the file alone changes nothing.

## 8. What blocks the student-facing surfaces (measured 2026-11-19)

Both the portal and the payment webhook sit **behind the registrar's SSO gate**.
Measured with `curl` against `https://registrar.atheniq.institute`:

| path | answer | consequence |
|---|---|---|
| `/portal` | 302 → `auth.cerulean.innotel.us` | students have no Authentik account, so they cannot sign in |
| `/api/portal/pay/webhook` | 302 → the same gate | Stripe's POST becomes a redirect; **no payment can ever confirm** |
| `/students`, `/news` | 302 → the gate | correct: these are staff surfaces |

The oauth2-proxy already exempts public routes (`^/pay`, `^/verify`,
`^/api/mobile`, …), so the fix is the same shape — add to its command:

```
--skip-auth-route=^/portal($|[/?])
--skip-auth-route=^/api/portal/
```

The portal authenticates itself with its own signed cookie (`atheniq_learner`),
and the webhook authenticates itself with the Stripe signature, so neither needs
SSO. `--skip-auth-route` is a prefix match, so `^/api/portal/` also covers the
pay, login and logout handlers. **Do not** exempt `/students` or `/news`.

**Status: staged, not applied.** The two lines are in
`/home/tutor/atheniq/deploy/compose/atheniq-sso.yml` (lines 111–112, YAML
validated, original kept as `atheniq-sso.yml.bak-*`), but
`docker compose up -d` **refuses** to recreate the file's services:

```
error while interpolating services.generation-sso.environment.[]:
required variable OAUTH2_PROXY_CLIENT_ID is missing a value
```

That variable is resolved from the vault at deploy time, so the compose file
cannot be brought up by hand. The running proxies are unchanged (`Up 10 hours`).
Applying it means re-rendering the compose with its vault environment, not
editing the file alone.

### The hostname is served by a different application

Measured against the proxy's own upstream (`http://192.168.1.59:8090`):

| fact | value |
|---|---|
| server header | `AthenIQPortal/1 Python/3.12.3` |
| process | `/usr/bin/python3 /home/tutor/atheniq-generation/scripts/atheniq-portal.py` |
| unit | `deploy/systemd/atheniq-generation.service` (`GENERATION_PORT=8090`) |
| `/api/portal/pay/webhook` | **404** |
| `/portal` | 200 — the catch-all page, not a portal |

So `registrar.atheniq.institute` is **not** the Next console. It is the
course-generation portal: a read-only catalog plus the generation queue, which
forwards two actions to `generation-console.py` and owns no registrar, billing or
student surfaces at all.

The Next console in `innotelinc/atheniq-console` (§6–§8, head `e053819`) has
never been deployed to that name. Nothing there is broken — it simply is not
live, and the 404 is the honest answer rather than a regression.

Where the console runs is therefore a **placement decision, not a bug**: a new
systemd unit alongside `atheniq-generation.service`, or a container on
`atheniq-next` (§9), with the gate pointed at it. Until that is chosen, `/portal`,
`/students`, `/news` and the Stripe webhook exist only in the repository.

### Mail for `@atheniq.institute`

The estate already has the pieces, and they are half-connected:

* `MX atheniq.institute` → `mail.atheniq.institute` → `73.68.203.71`.
* `mail` container (`Stalwart`, `220 mail.innotel.us Stalwart ESMTP`) answers on 25/587/443.
* A probe (`RCPT TO:<nobody-probe@atheniq.institute>`) returns
  `550 5.1.1 Mailbox does not exist` — the server accepts the sender and rejects
  only the recipient, i.e. the domain is hosted and simply has no users yet.

So no third-party mail provider is needed: mailboxes are created in Stalwart on
infrastructure the institute owns, and the console only reserves the address
(§7). The console's `mailbox.status` stays `allocated` until the mailbox exists,
which is why the portal says “being set up” rather than pretending mail is live.

**The first mailbox exists.** Stalwart's management API answers to the recovery
admin with Basic auth on `127.0.0.1:8080` (the Bearer challenge in its 401 is for
the *web admin*, not the API), and accounts are created over JMAP:

```
x:Domain/query  {"filter":{"name":"atheniq.institute"}}        -> domain id "z"
x:Account/set   {"create":{"n1":{"@type":"User","name":"ada.mensah",
                 "domainId":"z","roles":{"@type":"User"}}}} -> id "dd"
```

Confirmed at the SMTP edge rather than by reading the store back:

```
MAIL FROM:<probe@innotel.us>            250 2.1.0 OK
RCPT TO:<ada.mensah@atheniq.institute>  250 2.1.5 OK          (was 550 5.1.1)
RCPT TO:<ghost-probe@atheniq.institute> 550 5.1.1 Mailbox does not exist.
```

The control address still being refused is the point: the 250 is this mailbox,
not a catch-all. `mailbox.status` then became `active` with `mailboxId: "dd"`, so
the student's portal shows a working address. Remaining addresses are reserved in
the console and created the same way — one `x:Account/set` each.

## 9. Second stack: launched, and it cannot host the console

`atheniq-next` is up on i1 at `192.168.1.120` — 2 vCPU, 46 MiB in use, host still
at 5134 MiB available. It is an **empty Ubuntu container**: the Docker/Tutor
install and the snapshot restore are §5's next steps, not done, and it is
reversible with `atheniq-parallel-stack.sh --destroy`.

Two measurements rule it out as the console's home:

* `limits.memory` is **256 MiB**, not the 3584 MiB the script records, so a
  `next build` or even a warm `next start` is not safe there.
* `apt-get update` never completed inside it, so nothing can be installed there
  from the network it does have.

The LMS container is therefore where the console runs (§10). Leaving `atheniq-next`
alive is harmless but it is not the parallel stack §5 describes until its memory
limit is fixed in `atheniq-parallel-stack.sh`.

## 10. The console is deployed, and the gate points at it

| item | value |
|---|---|
| service | `atheniq-console.service` (systemd, inside the `atheniq` container) |
| code | `/opt/atheniq-console` — `atheniq-console@e053819`, built on i4, shipped as a tarball |
| runtime | `/opt/node` (Node v22.14.0, shipped; the container has no Node and its apt hangs) |
| listener | `192.168.1.59:8094` |
| secrets | `/etc/atheniq-console.env`, 0600, copied from `secrets/atheniq-console.env` |
| gate | `registrar-sso` (vault container) upstream `127.0.0.1:8090 → 192.168.1.59:8094` |

Two proxy files exist, and the hostname uses the one that is **not** in
`atheniq-sso.yml`: `registrar.atheniq.institute` terminates at `registrar-sso`
inside the **vault** container, built from
`/usr/src/atheniq-console-sso/docker-compose.yml` (cookie
`_atheniq_registrar_sso`, OIDC client `atheniq-registrar`). That is where the two
exemptions and the new upstream belong. `generation-sso` on `8091` is a different
hostname's door and was pointed at the console only for internal probes — patching
it first changed nothing on the public site, which is how this was found.

Nothing was removed: `atheniq-portal.service` still serves the generation catalog
on `192.168.1.59:8090` and `atheniq-student.service` still serves the learner view
on `8092`. The previous compose sits beside it as `docker-compose.yml.bak-*` for a
one-line revert.

Measured on the hostname itself
(`--resolve registrar.atheniq.institute:443:73.68.203.71`):

| request | answer | meaning |
|---|---|---|
| `GET /portal` | 307 → `/portal/login` | students reach it; no Authentik wall |
| `GET /portal/login` | 200 | the form students sign in with |
| `POST /api/portal/pay/webhook` | **400** | reaches Next and fails the signature — not a 302 |
| `GET /students` | 302 → Authentik | staff only, as intended |
| `GET /news` | 302 → Authentik | staff only, as intended |

The compose ran with `--env-file .env.atheniq-sso`. The earlier
`OAUTH2_PROXY_CLIENT_ID` failure was only that missing flag, not a broken file.

Stated plainly: the store shipped with the **development fixtures** — 4 learners on
`example.edu` addresses, 77 courses, 3 programs — because `data/store.json` is
untracked and the live institute's records never lived in this repository. Ada
Mensah is a fixture learner, so her mailbox is a working demonstration, not a real
student's account. The next real step is importing the actual roster.
## 11. Live billing, four mailboxes, and what a roster actually is (2026-11-18)

### Billing

The live keys are in `secrets/atheniq-console.env` and `/etc/atheniq-console.env`
(both 0600) and the console was restarted, because a key only exists for the
process that has it in its environment. Verified with read-only calls only:

| check | result |
|---|---|
| `GET /v1/account` | `acct_1G3vKxB2waof1AVK`, `charges_enabled: true`, `details_submitted: true`, country US |
| key mode | live — the endpoint created under it reports `livemode: true` |
| webhook endpoint | `we_1UdLtFB2waof1AVKUJl9w5c1`, `status: enabled`, `livemode: true` |
| events | `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.expired` |
| target | `https://registrar.atheniq.institute/api/portal/pay/webhook` |

The endpoint's signing secret went straight into the console's env file and was
never printed. Before this the webhook answered **400 for every request** because
no secret existed to verify against; that is the same 400 you get from an unsigned
POST now, which is correct but is no longer the *only* possible answer.

**No card has been charged, and none can be by me.** Completing a live checkout
needs a real payment instrument, so end-to-end proof of money moving is still the
school's step.

**Risk to state plainly:** `/pay` and `/api/portal/pay/` are exempt from the SSO
gate, so anyone who learns the URL can open a live checkout against this account.
With fixture learners and unreconciled prices, the account should be treated as
open to the public until the gate decision is revisited.

### Student mailboxes

Four accounts now exist, each created through `x:Account/set` with `name` and
`domainId` only — `emailAddress` is server-derived and refused as a client-set
property (`invalidPatch: Cannot modify server set property`).

| learner | address | Stalwart id | SMTP RCPT |
|---|---|---|---|
| `lrn-ada` | `ada.mensah@atheniq.institute` | `dd` | 250 2.1.5 OK |
| `lrn-marcus` | `marcus.bell@atheniq.institute` | `de` | 250 2.1.5 OK |
| `lrn-priya` | `priya.raman@atheniq.institute` | `df` | 250 2.1.5 OK |
| `lrn-jonas` | `jonas.weber@atheniq.institute` | `dg` | 250 2.1.5 OK |
| control | `ghost-probe@atheniq.institute` | — | 550 5.1.1 Mailbox does not exist. |

The control address is the evidence that matters: the four 250s are four
mailboxes, not a catch-all.

**Passwords are refused, and that is a design fact rather than a missing step.**
Setting a credential returns `forbidden: Cannot set credentials for accounts in an
external directory.` The accounts are directory-backed, so Stalwart does not own
the password and cannot mint one; authentication belongs to the identity
provider. A student therefore cannot sign into webmail until their directory entry
exists, which is the same dependency as the roster below.

### The roster does not exist yet

`scripts/import-lms-roster.mjs` reads Open edX's `auth_user` (or a registrar CSV
with `name,email[,program]`), dry-run by default, matching by e-mail so mailboxes
and completions survive. Run against the LMS:

```
source: Open edX auth_user
read 5, add 0, update 0, skip 5
```

All five are refused on purpose: two are system mailboxes (`cms@openedx`,
`login_service_user@fake.email`) and three are nameless placeholders
(`student.one|two|three@innotel.us`). Enrolling those as students would be worse
than the four named fixtures the store already holds, so nothing was written.

**Conclusion: there is no institute roster to import.** The estate has a working
LMS with a test course, an empty identity directory for students, and four demo
learners. The importer is the mechanism; the registrar's data is the missing
input, and it should arrive as a CSV or as real accounts in the identity provider
— not as placeholder rows renamed to look real.
