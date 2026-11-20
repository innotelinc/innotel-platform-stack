# AthenIQ Institute — integrated platform plan (2026-11-19)

Status: **plan only**. Nothing in this document has been built.

## What exists today

| Surface | What it is | State |
|---|---|---|
| `atheniq.institute` | static landing page (200) | live |
| `registrar.atheniq.institute` | application intake (302 on `/`) | live, source not yet identified |
| `atheniq.courses` | Tutor 22 (Verawood) LMS, 120 courses, Meilisearch index | live; catalog 500 fixed |
| `atheniq-console` | Next.js 15 / React 19 / TS, private, port 3100 | staff console: courses, credentials, programs, pay, verify, integrations |
| `atheniq-console/mobile` | Expo app (`app.json`, `store/`) | staff/general app |
| OmniRoute | OpenAI-compatible gateway `192.168.1.71:20128` | running, used by console `lib/ai.ts` |
| Mail | Stalwart at `mail.innotel.us`, SMTP 587 (console: 465) | working for console mail |

Two integration assets already in place, both worth building on:

- **OIDC**: config carries `CERULEAN_OIDC_CLIENT_ID: atheniq-lms` — the LMS already federates
  with the estate identity provider (Cerulean). The student portal and app should join the same
  provider, not invent a second login.
- **Console libraries**: `lib/mailer.ts`, `lib/mobile-api.ts`, `lib/mobile-auth.ts`,
  `lib/mobile-commerce.ts`, `lib/mobile-push.ts`, `lib/passkey-client.ts`. The student app is
  mostly a re-skin of these, not a new codebase.

## The decision that was blocking everything (settled 2026-11-20)

"Recreate all courses and everything from scratch" would mean deleting 120 courses, their
enrolments, student accounts and progress — irreversible, and it takes the site down while it
happens.

The answer, decided 2026-11-20: **the LMS stays where it is, on `.59`.** The courses, users and
progress are not recreated, and the parallel stack built on `next.atheniq.courses` is a lab, not
a replacement (`atheniq-stays-on-59-2026-11-20.md`). The phases below therefore add surfaces
around the live LMS rather than behind a new one, and nothing in them needs a cutover.

## Phases

**P1 — identity spine.** Register the student portal + student app as OIDC clients on Cerulean,
alongside `atheniq-lms`. Every other phase assumes this. Deliverable: one login works across
`atheniq.institute`, `portal.atheniq.institute`, `atheniq.courses`.

**P2 — student portal.** New `student` surface in `atheniq-console`: enrolments, grades,
transcript (console already issues credentials via Signara), billing, profile. Reads the LMS via
its REST APIs (`/api/enrollment/v1`, `/api/grades/v1`, `/api/user/v1`) rather than duplicating data.

**P3 — billing.** Stripe (needs an account + keys; not yet chosen or provisioned). Tuition per
program term, invoices visible in the portal; the LMS `pay` surface in console is the precedent.

**P4 — student mail `@atheniq.institute`.** Add the domain to Stalwart, publish SPF/DKIM/DMARC,
and provision mailboxes on enrolment. Verify current `atheniq.institute` MX first — it was not
probed.

**P5 — news/announcements.** Single source (console `lib/alerts.ts` precedent), syndicated to the
landing page, portal and app. Avoid a second CMS: the console is already the operations backend.

**P6 — student app.** Fork `atheniq-console/mobile` (Expo), swap the API base to the student
surface, reuse `lib/mobile-auth`/`mobile-commerce`/`mobile-push`. Course playback in-app via the
LMS mobile API + video pipeline.

**P7 — AI study tools.** Route through the existing OmniRoute gateway: syllabus Q&A grounded in
course content, practice generation, feedback on drafts. No new model provider needed.

## Sequencing note

P1 then P2 is the smallest slice that makes "full institute" real: one identity, one portal showing
a student's real enrolments and grades from the live LMS. P3–P7 are additive on top of it and each
is independently shippable. Building P6 before P2 is the common failure mode — the app would have
no API to talk to.
