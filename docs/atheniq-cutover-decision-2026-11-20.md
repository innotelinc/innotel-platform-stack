# AthenIQ: what the cutover needs, 2026-11-20

**Decision: the public cutover has not happened and should not be scheduled until
the three open items below are closed** (item 2 was closed on 2026-11-20).** `atheniq-next` (`.120`) is a healthy parallel
stack, now reachable at `next.atheniq.courses` over TLS, and it is the right place
to rehearse on. It is not yet a replacement for the stack answering
`atheniq.courses`, `catalog.atheniq.courses`, `learn.atheniq.courses` and
`apps.atheniq.courses`.

## What serves the public names today

| Name | Edge vhost | Upstream |
|---|---|---|
| `atheniq.courses` | `atheniq-courses` | `192.168.1.59:18080` |
| `catalog.atheniq.courses` | `atheniq-courses-catalog` | `192.168.1.59:18080` |
| `learn.atheniq.courses` | `atheniq-courses-learn` | `192.168.1.59:18080` |
| `apps.atheniq.courses` | `atheniq-courses-apps` | `192.168.1.59:18080` |
| `next.atheniq.courses` | `next-atheniq-courses` | `192.168.1.120:18080` (via TLS at the edge) |

The apex row on `.59` has been up since the migration began; `atheniq-next` has
been up in parallel. Both are full tutor stacks, and both answer the same paths.

## What still blocks a cutover

1. **One writer per database.** The two stacks do not share a database. Cutting
   over without a final dump/restore, or a freeze on `.59`, loses everything
   written between the rehearsal and the switch. The order has to be: stop
   writers on `.59`, dump Mongo + MySQL + the media volume, restore into `.120`,
   then move names. `docs/atheniq-cutover-and-migration-2026-11-18.md` records the
   addresses and the restore shape used for the first rehearsal.
2. ~~**Search is empty on the rehearsal stack.**~~ **Closed 2026-11-20, later the
   same day.** The index caught up on its own: Meilisearch reports 120 documents in
   `tutor_course_info` (last updated 17:10), and both halves of the search path now
   answer from it — `POST /indexes/tutor_course_info/search` returns hits for
   `ECON101 — Microeconomics`, and the LMS endpoint the catalog calls,
   `/search/unstable/v0/course_list_search/`, returns `{"total": 120, "results": […]}`.
   The statement above was true when written and stale within hours, which is why it is
   recorded as closed rather than deleted: the blocker was a backfill completing, not a
   defect, and nothing had to be reindexed by hand. One thing it exposes for the
   cutover: `?search=` on `/api/courses/v1/courses/` returns the whole catalogue (120 for
   `Financial` and for `Nursing` alike), so the search box is the Meilisearch path only.
   Verify a cutover against `course_list_search`, not against the courses API's `search`
   parameter, which looks like it works and does not filter.
3. **The certificate and the redirect are per name.** `next.atheniq.courses` now
   has both (`npm-next-atheniq-courses`, valid to 2027-02-18, HTTP forced to
   HTTPS). The four public names already have theirs. A cutover should reuse those
   certificate rows and only change the upstream, not re-issue.
4. **Identity and mail.** `learn.` and `apps.` sign in through the LMS/OAuth
   client that is registered against the public names. A cutover that changes the
   stack must keep `LMS_ROOT_URL`, `CMS_ROOT_URL`, the OAuth client redirects and
   the mail relay the same, or sign-in breaks in a way that looks like a password
   problem. `check-sso-redirects.py` covers the registered-URI half.

## The order to do it in

1. Finish indexing on `.120` and click through `next.atheniq.courses` as an owner
   and as a learner.
2. Freeze `.59` writers, dump, restore into `.120`, run migrations, verify the
   course count and the course about pages.
3. Point the four vhost upstreams at `192.168.1.120:18080` — one at a time, apex
   last — leaving DNS on the edge throughout. No A record changes are needed, and
   `check-edge-front-door.py` fails if one is introduced.
4. Keep `.59` stopped, not deleted, until the first full day of classes has passed.

## Why the failure on 2026-11-20 looked like a stack failure

`next.atheniq.courses` had been pointed at `192.168.1.120` directly. The stack was
healthy; the browser reached it over plain HTTP, so `CSRF_COOKIE_SECURE` meant the
CSRF cookie was never stored and the catalog's first `POST` was refused with
`CSRF cookie not set`. The fix was to move the name behind the edge and give it the
same TLS front door as the public names — not to relax the cookie setting.
