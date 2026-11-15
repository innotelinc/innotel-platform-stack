# The classroom is `class.atheniq.courses`

Decided 2026-11-14. The AthenIQ classroom answers on two names, and they are not the same
kind of name:

| name | what it is | what a learner gets |
| --- | --- | --- |
| `class.atheniq.courses` | **the classroom** — every link in the courseware names it | 302 to authentik, `client_id=classroom`, back to its own `/oauth2/callback` |
| `classroom.atheniq.courses` | a superseded name, still served by the same app | the same 302, with *its* own callback |
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

## The ordered change (edge work — not done in this repo)

1. **Redirect it.** In NPM, replace the `classroom.atheniq.courses` proxy host with a
   redirection host to `https://class.atheniq.courses`, path preserved — the estate's
   convention for a superseded name (`learn.innotel.us → atheniq.courses`,
   `studio.innotel.us → studio.atheniq.courses`). Verify a deep link:
   `curl -sI https://classroom.atheniq.courses/classroom/<id>` → `301` with the path intact.
2. **Then the provider.** Remove `https://classroom.atheniq.courses/oauth2/callback` from
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
people learn to close.

For the alias it accepts exactly two states and no third: **gated the same way**, or
**redirecting to the classroom** (published as a `note`, so the retirement is visible on the
dashboard when it happens). A 200 on either name is the failure the check exists for: the
classroom is not "up" when it answers anonymously, it is open.
