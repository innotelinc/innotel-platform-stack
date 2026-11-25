# Forward-auth gates: the domain decides the auth host (2026-11-25)

**Symptom.** `deploy.innotel.us` authenticated and then came back to the login
page, forever. The same was true of every gated name outside
`*.cerulean.innotel.us`, while the handful of gates *inside* that zone worked
and made the whole thing look half-configured.

> **Correction to `docs/sign-in-posture.md`.** That file states (2026-09-15) that
> forward-auth is retired and no NPM proxy host carries an `auth_request` gate.
> That is no longer true: **27 proxy hosts carry the forward-auth snippet today**.
> This document is the record of what they are and what they must point at. The
> posture file's contract (OIDC-native or `oauth2-proxy` gateway) still describes
> the majority of the estate; the forward-auth gates are an additional, separate
> front door and are not covered by it.

## The mechanism, measured

An Authentik embedded outpost serving forward-auth selects the **provider from
the host of the auth URL** — the `Host` header on
`/outpost.goauthentik.io/*` — not from the `rd=` parameter naming the site
being gated. The provider in turn decides the **cookie domain**. So the auth
host baked into each NPM `advanced_config` snippet silently decides where the
session cookie may be read:

| auth URL in the snippet | provider | cookie domain |
| --- | --- | --- |
| `https://auth.innotel.us` | 35 | `innotel.us` |
| `https://auth.cerulean.innotel.us` | 34 | `cerulean.innotel.us` |
| `https://capstone.innotel.site` | 68 | `capstone.innotel.site` |
| `https://auth.innotel.site` | 69 | `innotel.site` |
| `https://auth.telno.services` | 70 | `telno.services` |
| `https://auth.pbx.it.com` | 71 | `pbx.it.com` |
| `https://auth.rizzaura.net` | 72 | `rizzaura.net` |
| `https://auth.zeus.innotel.us` | 32 | `zeus.innotel.us` |
| `https://christ.innotel.us` | 56 | `innotel.us` |

**The root cause:** all 27 snippets named `auth.cerulean.innotel.us`, so every
gate outside `cerulean.innotel.us` was issued a cookie the site could never
present. The login succeeded and the gate re-challenged — the classic loop.
Two snippets (`voice.innotel.us`, `grist.innotel.site`) were worse: their
`return 302` pointed at `$scheme://$http_host`, i.e. **the gated site itself**,
so the challenge looped without Authentik ever being asked.

## The gates, and what each now points at

`deploy.innotel.us` is one row of 27. Verified by following the redirect from
each name and by resolving `/outpost.goauthentik.io/start` on each auth host to
its provider and cookie domain.

| NPM host | Gated name | auth host | provider |
| --- | --- | --- | --- |
| 6, 11, 84, 85, 89, 183, 221, 223, 342, 375, 5, 74, 177 | `admin.magnate`, `admin.signara`, `i1`, `i2`, `internal`, `admin.distro`, `admin.mail`, `admin.oasis`, **`deploy`**, `terminal`, `admin.cerulean`, `dns.cerulean`, `npm.cerulean` | `auth.innotel.us` | 35 |
| 382, 383, 385, 386, 387 | `capstone`, `app.capstone`, `dashboard.capstone`, `admin.capstone`, `voice.capstone` | `capstone.innotel.site` | 68 |
| 388, 393, 408 | `voice`, `grist`, `f2b` | `auth.innotel.site` | 69 |
| 399, 401, 406 | `pbx.telno`, `admin.telno`, `grist.telno` | `auth.telno.services` | 70 |
| 405 | `pbx.it.com`, `www.pbx.it.com` | `auth.pbx.it.com` | 71 |
| 10 | `admin.rizzaura.net` | `auth.rizzaura.net` | 72 |

## What had to be built

Four of those domains had **no provider at all**, and `pbx.it.com` /
`rizzaura.net` had auth hosts that fell through to the Capstone provider with an
empty cookie domain — reachable, and useless. Created:

| Domain | DNS | NPM host | Cert | Provider | App |
| --- | --- | --- | --- | --- | --- |
| `innotel.site` | `auth` A → `73.68.203.71` | 413 | 209 | 69 | `innotel-site-npm-forward-auth` |
| `telno.services` | `auth` A → `73.68.203.71` | 414 | 210 | 70 | `telno-services-npm-forward-auth` |
| `pbx.it.com` | existing | 34 (existing) | 95 | 71 | `pbx-it-com-npm-forward-auth` |
| `rizzaura.net` | existing | 45 (existing) | 69 | 72 | `rizzaura-net-npm-forward-auth` |

All four are in the embedded outpost (`99914d2a-5235-4955-9ebe-9f8f468def6f`),
each with the flows the zone providers already use: implicit-consent
authorization, `default-invalidation-flow`, `forward_domain`, `intercept_header_auth`.

## Leftovers closed in the same pass

- **`auth.zeus.innotel.us` was swallowed by the Zeus app.** NPM host 241 listed
  `auth.zeus.innotel.us` among nine Zeus names and forwarded it to the app on
  `192.168.1.59:8088`, so the outpost returned the app's **404** and provider 32
  was unreachable. The name is split out (host 415, cert 212) and now answers
  `/outpost.goauthentik.io/start` with provider 32; host 241 keeps the other
  eight Zeus names unchanged.
- **`christ.innotel.us` had DNS and a provider but no proxy host** — TLS failed
  with `TLSV1_UNRECOGNIZED_NAME` before Authentik was ever reached. Host 416
  (cert 142, already issued) makes provider 56 usable.
- **`voice.innotel.us` referenced certificate 1, which no longer exists.**
  It was the only host in NPM with a dangling `certificate_id`; NPM cannot
  complete a handshake for it. Cert 211 issued and attached.
- **Provider 68 carried no cookie domain**, so the Capstone subdomain gates
  (`app.`, `dashboard.`, `admin.`, `voice.`) could not receive a cookie. Set to
  `capstone.innotel.site`. (A `PATCH` is rejected — the provider requires a full
  update body with `internal_host`; use `PUT`.)
- **Provider 64 (`p-clearpath`) was on no outpost** and **provider 66 (`grist`,
  `forward_single`) is superseded** by provider 69 for that name. 64 added,
  66 removed from the outpost. Provider 34 stays on the outpost: the three
  cerulean gates were consolidated onto `auth.innotel.us`, but
  `auth.cerulean.innotel.us` remains a valid door for anything else naming it.
- **Duplicate `speaches.innotel.site` certificates** 201 and 203 removed
  (198, attached to host 397, kept).
- **`openco-mcp.innotel.site` had two A records** — the public address and a
  stale `192.168.1.53`. The internal one is gone; the zone answers one address.

## Verification

- All 27 gates return `302` to the auth host that owns their domain (the table
  above), and each auth host's `/start` resolves to the expected provider and
  cookie domain.
- Following the redirect chain, `deploy.innotel.us`, `pbx.it.com`,
  `grist.telno.services`, `admin.rizzaura.net`, `f2b.innotel.site`,
  `voice.capstone.innotel.site`, `admin.telno.services`, `terminal.innotel.us`
  and `grist.innotel.site` each land on a `200` Authentik login page.
- `voice.innotel.us` completes TLS again and challenges with provider 35.

**Not verified:** the post-login hop back to the gated app. That needs a real
credential submission through the flow, which this audit did not perform. The
fix rests on the cookie domain matching the site's domain — the exact thing
that was broken — and that is asserted per host above.

## Note for the next change

Two rules made this class of bug invisible, and both are worth keeping in mind:

1. **A gate's `advanced_config` is the only place its auth host is written.**
   Nothing validates that the provider behind that host covers the gated domain,
   so a copy-pasted snippet is indistinguishable from a correct one until someone
   signs in. Check `cookie_domain` against the gated name, not the redirect's
   shape.
2. **A provider with an empty `cookie_domain` works for exactly one hostname.**
   It is a valid config that silently fails for every subdomain.

Backup of all 27 original snippets: `/root/npm-auth-backup-20261125.json` on `.54`.
