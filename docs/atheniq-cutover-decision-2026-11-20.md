# AthenIQ: what the cutover needs, 2026-11-20

**Decision: the public cutover has not happened and should not be scheduled until
the four items below are closed.** `atheniq-next` (`.120`) is a healthy parallel
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
2. **Search is empty on the rehearsal stack.** `tutor_course_info` in
   `atheniq-next` reports 0 documents, so the catalog page lists courses from the
   courses API but its search box returns nothing. The index is populated by CMS
   (`reindex_studio` → Celery `rebuild_index_incremental`); the first run has to
   complete before the stack is a fair rehearsal. This is a data step, not a code
   defect, and it does not affect the page-level fix made on 2026-11-20.
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
