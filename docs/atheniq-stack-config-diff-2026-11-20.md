# AthenIQ: what the two tutor roots disagree about (2026-11-20)

`atheniq` (`.59`, the public names) and `atheniq-next` (`.120`, `next.atheniq.courses`)
are both tutor 22.0.2 stacks, and they do not carry the same configuration. This is the
list of differences that a cutover has to close, produced by reading each host's
`config.yml` for its key names and for a fixed set of non-secret values — no secret value
was read for this document, and none is recorded here.

| | `atheniq` (`.59`) | `atheniq-next` (`.120`) |
|---|---|---|
| tutor root | `/home/tutor/.local/share/tutor` | `/home/tutor/tutor-next` |
| config keys | 35 | 19 |
| `LMS_HOST` | `atheniq.courses` | `next.atheniq.courses` |
| `CMS_HOST` | `studio.atheniq.courses` | `studio.next.atheniq.courses` |
| `DOCKER_IMAGE_OPENEDX` | `docker.io/overhangio/openedx:22.0.2-indigo` (stock) | `ghcr.io/innotelinc/openedx-platform:de1f9f5-unity` (fork) |
| `ENABLE_HTTPS` | `true` | `false` |
| LMS/CMS ports | `18080` / `18081`, `CADDY_HTTP_PORT: 18080` | not set (tutor defaults), published behind NPM on `:80` |

## Keys that exist only on the public stack

These are the gaps. Each one is a behaviour the public names have and the rehearsal stack
does not.

**Mail — `.120` has no outbound SMTP at all.** `.59` carries `EMAIL_HOST: mail.innotel.us`,
`EMAIL_PORT: 587`, `EMAIL_USE_TLS: true`, `EMAIL_HOST_USER: atheniq@innotel.us` and its app
password. `.120` carries none of them, so it falls back to tutor's bundled relay
(`devture/exim-relay`): registration, password reset and approval mail either never leave
the host or leave from an address the mail server has no reason to trust. This is the gap
that fails most quietly, because the platform looks healthy while nobody receives anything.

**Identity — the OIDC client and the old hostnames.** `.59` carries
`CERULEAN_OIDC_CLIENT_ID`/`_SECRET`, plus `ATHENIQ_SESSION_COOKIE_DOMAIN: .atheniq.courses`,
`ATHENIQ_LEGACY_LMS_HOSTS`, `ATHENIQ_LEGACY_MFE_HOSTS`, `ATHENIQ_STUDIO_ALIAS_HOSTS` and
`ATHENIQ_REDIRECT_HOSTS`. `.120` carries a *different* client (`ATHENIQ_NEXT_OIDC_CLIENT_ID`
/ `_SECRET`). Consequences on `.120`: no shared session cookie across the subdomains, no
redirects from `lms.innotel.us`, `learn.innotel.us`, `apps.learn.innotel.us`,
`studio.innotel.us` or `www.atheniq.courses`, and — the one that looks like a password bug —
`apps.` and `learn.` sign in through a client whose registered redirect URIs belong to the
public names. `check-sso-redirects.py` covers half of this; the cookie domain and the alias
hosts are not covered by any check.

**The front door's ports.** `.59` publishes `18080`/`18081` and tells its own Caddy to listen
on `18080` (`CADDY_HTTP_PORT`, `ENABLE_HTTPS: true` with `ENABLE_WEB_PROXY: false`). `.120`
has none of those keys: it answers on tutor's defaults behind NPM on `:80` with
`ENABLE_HTTPS: false`. The edge vhosts still point at `192.168.1.59:18080`, so a cutover that
only moves names would arrive at the wrong port, and the https scheme on `.120` is currently
supplied by the template fix in `atheniq-lms-https-urls.md` rather than by `ENABLE_HTTPS`.

**Branding and the MFE pin.** `.59` carries the `MFE_CONFIG` dict (account, profile,
discussions, authoring and admin URLs under `apps.atheniq.courses`), `MFE_DOCKER_IMAGE:
…openedx-mfe:22.0.0-indigo`, `PARAGON_THEME_URLS` and `PLATFORM_NAME`. `.120` carries none of
them: the MFE image is resolved from tutor's default and the theme/branding values are
absent, so a cutover would either lose the look or silently re-theme the estate.

## Keys that exist only on the rehearsal stack

`OPENEDX_COMMON_VERSION: release/verawood.1` and `RUN_MEILISEARCH: true` — the first pins the
platform line the fork image was built from, the second is tutor's default made explicit.

## What this means for the cutover order

The names can move in an afternoon; the configuration is the part that takes care. In
dependency order: copy the mail values and prove a real message lands; register the
`.120` client's redirect URIs (or reuse the public client) and set the session cookie
domain; decide whether `.120` keeps `:80` behind NPM or takes over `:18080`; then carry
`MFE_CONFIG` and the theme values; only then move `catalog.`, `learn.`, `apps.`, apex last.

`check-edge-front-door.py` guards the DNS half of that move (no name may bypass the edge).
Nothing yet guards the configuration half, which is why this file exists.
