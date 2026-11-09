# mail.innotel.us SSO — the cached directory failure, 2026-11-06

**Report:** "mail.innotel.us SSO login isn't working."
**What it was:** not the sign-in. Authentik authenticated the person and the webmail
exchanged the authorization code for a token — then every request after that answered
**500**, so the page said *Authentication Failed* after a *correct* login. Stalwart's
authentication directory had failed to initialise once and cached that failure for the
life of the process.
**Fix applied:** restart `stalwart` in the `mail` container, then a watchdog that
performs that restart automatically (`stalwart-watchdog.timer`).
**Now watched:** `scripts/check-mail-sso.py`, run by `estate-checks.service`.

## What was measured, leg by leg

The whole browser round-trip was driven against production (a throwaway Authentik
identity in the `Mail` group, created and deleted for the run):

| Leg | Observation |
| --- | --- |
| `POST /api/auth/sso/start` | **200**, authorize URL naming `client_id=stalwart-webui` and `https://mail.innotel.us/en/auth/callback` |
| Authentik authorize | **302** into `default-authentication-flow` — the callback is on the provider's list |
| Authentik flow (identification + password) | completed, redirect to the app's callback |
| `GET /en/auth/callback?code=…&state=…` | code returned, `state` matched |
| `POST /api/auth/sso/complete` | **200 with an access token** |
| `GET /jmap/session` with that token | **500** — the failure, one step after a successful sign-in |

The discriminator was the bearer probe:

| Request | Result |
| --- | --- |
| `GET /jmap/session` anonymous | **200** (the server serves) |
| `GET /jmap/session` with `Authorization: Bearer not-a-token` | **500** (every bearer) |
| `GET /jmap/session` with a structurally bogus JWT | **500** |
| same, on `oasis.innotel.us` and on `192.168.1.15:8080` | **500** |

A token the server never issued must be refused with **401**. Answering 500 means the
request never reached token validation: the authentication backend is unavailable.

## Root cause: the directory initialises once and caches a failure

Stalwart's log names it, on the `http` listener as the webmail uses it:

```
ERROR Store not configured (store.not-configured) listenerId = "http", localPort = 8080
  details = "Directory is unavailable because it failed to initialize"
  reason  = "Network error: Discovery fetch failed: error sending request for url
             (https://auth.cerulean.innotel.us/application/o/stalwart-webui/.well-known/openid-configuration)"
  causedBy = "crates/directory/src/core/dispatch.rs:19"
```

The fetch that failed was never a network problem at the time of the failure: the same
discovery URL answers **200 in 0.12 s from inside that container**, and the webmail's own
discovery proxy reads it successfully. The initialisation happened once, failed (a blip,
an Authentik restart, or the container's own start ordering), and the failed state was
cached — Stalwart 0.16.27 never retries it. The condition was already present in the logs
on 2026-11-04, 2026-11-05 and 2026-11-06, so it had survived a restart of the container
and would have survived the next one.

This is the same fault recorded on 2026-11-02 in
[sso-cleanup-2026-11-02.md](sso-cleanup-2026-11-02.md), where a restart cleared it and
the one-time cause looked like a transient. It is not transient: it comes back, and in
this build nothing in the configuration prevents it.

### Why app passwords hid it

The directory is consulted for **bearer** tokens (SSO) and for password lookups. Accounts
hold `AppPassword` credentials, which the log shows succeeding throughout
(`Authentication successful … details = "Authenticated with app password"` for
`dhunter`, `postmaster`, `alertmanager`, `pbx`). Mail kept flowing over IMAP/SMTP and the
alert path stayed up while the interactive sign-in was the only thing broken — which is
why it was reported as an SSO problem rather than a mail-server problem.

## The fix

Two parts, because the restart is a recovery and not a cure:

1. **Recovery now.** `systemctl restart stalwart` in the `mail` container. All eight
   listeners return (25, 465, 587, 993, 995, 4190, 443, 8080) and app-password
   authentication continues uninterrupted. Verified after the restart: an impossible
   bearer answers **401** (was 500), anonymous **200**, and a full SSO round-trip ends
   with `/jmap/session` **200** (`username` resolved, accounts listed).
2. **Recovery automatically.** `stalwart-watchdog.timer` on the mail container runs
   `stalwart-directory-watchdog.sh` every five minutes. It restarts Stalwart only when
   the impossible-bearer probe answers **500** *and* the IdP discovery URL answers 200
   from the same container — so an Authentik outage is never turned into a restart loop —
   and no more than once per 15 minutes if a restart did not clear it. Recovery is logged
   to the journal with `logger -t stalwart-watchdog`.

Files (this repo is the source of truth for what is deployed):

| File | Installed as |
| --- | --- |
| `scripts/stalwart-directory-watchdog.sh` | `/usr/local/bin/stalwart-directory-watchdog.sh` (0755) in `mail` |
| `systemd/stalwart-watchdog.service` | `/etc/systemd/system/stalwart-watchdog.service` |
| `systemd/stalwart-watchdog.timer` | `/etc/systemd/system/stalwart-watchdog.timer`, enabled |

## What watches it now

`scripts/check-mail-sso.py`, added to `systemd/estate-checks.service` (half-hourly), drives
the three legs from outside and publishes the shared `innotel_estate_check_*` contract:

| Leg | Failure code | Meaning |
| --- | --- | --- |
| `sso/start` answers 200 | `sso_start_failed` | nobody can begin a sign-in (also `SESSION_SECRET` missing) |
| the app sends its own callback | `callback_mismatch` | a renamed host left the app asking for an unregistered URI |
| Authentik accepts that callback | `authorize_rejected` | provider list and app request disagree (Redirect URI Error) |
| discovery document answers 200 | `idp_discovery_failed` | the document both sides validate against is unreadable |
| anonymous JMAP answers 200 | `stalwart_unhealthy` | the server is not serving |
| impossible bearer answers **401** | `directory_unavailable` | the cached directory failure — names the watchdog |

An unreachable webmail or mail server is **exit 2**, never a pass, following the estate
rule that a check which could not run must not look clean.

## Adjacent audit, same day

Because the fault was a *directory* fault and not an SSO fault, the obvious question is
whether any other Authentik-backed service is quietly doing the same thing. The whole
estate was swept with `scripts/check-sso-redirects.py` from the edge (`i2move:proxy`,
where `docker exec cerulean` and the NPM database live): **184 registered redirect URIs,
274 hostnames probed, 138 pass, 46 fail, 0 warn.**

None of the 46 is this symptom. Categorically they are registration drift, not runtime
failure:

| Count | What it is |
| --- | --- |
| 44 | a redirect URI registered for a name that has **no enabled NPM proxy host** — retired or LAN-only (`*.capstone`, `*.zeus`, `192.168.1.51/52/54:8443`, `fax.pbx.services`) |
| 1 | a registered URI whose name does not resolve |
| 1 | a URI Authentik rejects although it is on the provider's own list |

Every mail/oasis row passes — all 19 of Stalwart's registered callbacks authorize with
302, across `mail.innotel.us`, `webmail.innotel.us`, `admin.mail.innotel.us`,
`oasis.innotel.us`, `app.oasis.innotel.us`, `files.oasis.innotel.us` and
`mail.oasis.innotel.us`. The bearer probe agrees: an impossible token is refused with
**401** on every one of those names, i.e. the directory is initialised everywhere the
same Stalwart answers. `webmail.innotel.us` answers 307 on `/jmap/session` because it is
the webmail front end, which redirects rather than serving JMAP.

Why no other service shows it: Stalwart is the only thing in the estate whose
*authentication* directory is fetched over the network at start-up and cached. The other
apps are OAuth2 forward-auth through the NPM/outpost chain, which re-reads the provider
metadata per request, and a failed read there surfaces as a 502 on one sign-in, not as a
cached state that outlives a restart.

The 46 registration findings are worth their own cleanup pass but are **not** part of
this incident and were left alone.

## Residual risk

The underlying behaviour is Stalwart 0.16.27's, not a configuration of this estate. The
watchdog bounds the broken window to about five minutes instead of "until somebody
notices"; it does not remove it. Two changes would:

- a Stalwart build whose directory retries initialisation (or whose directory type is
  `Internal`/`Sql`, which this build does not carry — see 2026-11-02), or
- fronting the OIDC directory with a **local** discovery document, so the fetch that
  fails at start-up does not depend on the network at all.

If bare IMAP password login is wanted again, that is the same 2026-11-02 conclusion: this
build has no internal directory, so clients stay on app passwords or OAuth.
