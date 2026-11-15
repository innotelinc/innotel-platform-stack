# The classroom is `class.atheniq.courses`

Decided 2026-11-14. The AthenIQ classroom answers on two names, and they are not the same
kind of name:

| name | what it is | what a learner gets |
| --- | --- | --- |
| `class.atheniq.courses` | **the classroom** — every link in the courseware names it | 302 to authentik, `client_id=classroom`, back to its own `/oauth2/callback` |
| `classroom.atheniq.courses` | a superseded name, now a 301 to the classroom | `301` to `https://class.atheniq.courses`, path and query preserved |
| `ai.atheniq.courses` | the authoring/generation app | its own gate; not part of this decision |

Measured 2026-11-14 with a plain `curl` from outside, no cookies:

```
class.atheniq.courses/      302 → https://auth.cerulean.innotel.us/application/o/authorize/
                                  ?client_id=classroom&redirect_uri=https%3A%2F%2Fclass.atheniq.courses%2Foauth2%2Fcallback
classroom.atheniq.courses/  302 → …client_id=classroom&redirect_uri=…%2Fclassroom.atheniq.courses%2Foauth2%2Fcallback
class.atheniq.courses/classroom/stage-MZMqptgj1o64
                            302 → the same gate (the deep links are gated too)
```

Both callbacks are registered on the one `classroom` provider — `classroom.`'s was added by
hand on 2026-11-06 (`mail-sso-directory-2026-11-06.md`), because a door asking for a
callback the provider does not hold fails *after* a correct sign-in with the same generic
error as a wrong issuer.

## Why the decision is not a rename

`classroom.atheniq.courses` does not have a certificate of its own. It is one of the four
SANs on the **learn** certificate (`ai`, `catalog`, `classroom`, `learn.atheniq.courses`),
while `class`, `studio` and `apps` each have a single-name certificate. So removing the
name is not one delete: the shared certificate has to be reissued with a shorter list, and
until it is, any DNS change leaves a name whose certificate claims it should exist.

That is why the retirement runs in this order, and why the repo's check accepts either
state while it does.

## Where it stands on 2026-11-14

Steps 1 and 2 are done and measured; step 3 is in this repository and reaches the edge
with `scripts/install-estate-checks.sh`; step 4 has not been started.

```
classroom.atheniq.courses/                  301 → https://class.atheniq.courses/
classroom.atheniq.courses/classroom/abc123 301 → https://class.atheniq.courses/classroom/abc123
classroom.atheniq.courses/classroom/abc123?x=1
                                            301 → https://class.atheniq.courses/classroom/abc123?x=1
```

The path *and* the query survive, which is why step 1 is a redirection host rather than a
302 to a landing page: the courseware's links are deep, and a learner who followed one from
a saved copy still lands on the lesson.

The `classroom` provider in authentik holds four callbacks after step 2
(`classroom.innotel.us`, `ai.lms.innotel.us`, `class.atheniq.courses`, `ai.atheniq.courses`);
the alias's own is gone, so the provider's record no longer names a door nobody uses. The
classroom's callback is untouched — removing it would have been the failure the check exists
for.

One thing worth knowing before the next retirement: this NPM (2026.8.5's bundled build)
rejects the body its own documentation suggests, and in two different ways. `enabled` is not
a writable field on a redirection host, and `advanced_config` has no default, so leaving it
out is a 500 rather than a validation error. Both came from NPM's own log
(`Field 'advanced_config' doesn't have a default value`) after the request had already passed
schema validation — not from guessing, and not from the API's error text alone.

## The ordered change (steps 1–2 done, 3–4 as written)

1. **Redirect it.** *(done 2026-11-14 — redirection host 45, certificate 119, `preserve_path`.)* In NPM, replace the `classroom.atheniq.courses` proxy host with a
   redirection host to `https://class.atheniq.courses`, path preserved — the estate's
   convention for a superseded name (`learn.innotel.us → atheniq.courses`,
   `studio.innotel.us → studio.atheniq.courses`). Verify a deep link:
   `curl -sI https://classroom.atheniq.courses/classroom/<id>` → `301` with the path intact.
2. **Then the provider.** *(done 2026-11-14 — the alias is a 301, so nothing authenticates on it.)* Remove `https://classroom.atheniq.courses/oauth2/callback` from
   the `classroom` provider in authentik. Nobody authenticates on the alias after step 1;
   leaving the callback registered is a second door left unlocked in the provider's own
   record.
3. **Then the record.** Add the row to `scripts/edge-redirect-targets.tsv` — to that file,
   not before it, because its header is explicit that it records what was measured, not
   what is planned. `check-edge-vhost-pins.py --redirect-pins` then holds the row, the
   generated file and the proxy-host list to it, and forbids the name quietly gaining a
   proxy host again.
4. **Then the certificate.** Reissue the learn certificate without `classroom.`, verify
   `catalog` and `learn` still serve on the new one, and only then remove the DNS record if
   the name is to disappear entirely.

Steps 1–2 are the ones with a user-visible deadline; 3–4 have none.

## How it is watched

`scripts/check-atheniq-doors.py` (`atheniq_doors`), run by `estate-checks.service` on the
edge, drives both names every half hour. It asserts the gate is on the *name* rather than
the landing page (it probes `/classroom/`, the path the courseware's links use), that the
client and callback match the provider's registration, and that the issuer is
`auth.innotel.us` — reporting the cerulean name as a **note**, not a failure and not a
warning. Both classroom names are consistently on one identity host, which is the
requirement [identity-hosts.md](identity-hosts.md) states; the brand is the estate's
direction. It is a note because `EstateCheckNotPinned` pages on a warning older than an
hour, and this one cannot clear until the provider changes — a page that cannot end is one
people learn to close. (On 2026-11-14 the gate was moved to `auth.innotel.us`, but the note did not clear
until the edge stopped serving a **second** host that also claimed `auth.innotel.us`: nginx
logged `conflicting server name "auth.innotel.us"` and answered with the cerulean record.
With that name removed from the duplicate, the public door answers
`https://auth.innotel.us/application/o/authorize/…` and the check reports
`0 failure(s), 0 warning(s), 1 note(s)` — the note being `alias_moved`, the end state.
The measurements above stay as taken; see [identity-hosts.md](identity-hosts.md) for the
duplicate-host failure, which also kept the mail mirror failing.)

For the alias it accepts exactly two states and no third: **gated the same way**, or
**redirecting to the classroom** (published as a `note`, so the retirement is visible on the
dashboard when it happens). It has been in the second state since 2026-11-14, which is what
`alias_moved` on the dashboard means. A 200 on either name is the failure the check exists for: the
classroom is not "up" when it answers anonymously, it is open.
