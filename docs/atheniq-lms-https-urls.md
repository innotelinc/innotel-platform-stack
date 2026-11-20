# The platform's own URLs are https, behind an edge that terminates TLS

*(2026-11-20: the stack this was written on, `next.atheniq.courses`, is stopped and its
vhost disabled — `atheniq-stays-on-59-2026-11-20.md`. The method below is kept: it is
what any Tutor stack behind the edge needs, and it is what `scripts/atheniq-lms-https.py`
checks.)*

`next.atheniq.courses` is served by the estate edge, which terminates TLS and forwards
plain HTTP to the Open edX container. Django therefore has to be told two things it cannot
work out for itself: that its public URLs are `https://`, and that a request arriving over
HTTP from the proxy is secure.

## Why the catalog page failed with "An unexpected error occurred"

The catalog MFE asks the LMS for its configuration and builds every API call from the
answer. On 2026-11-20 that answer was

```
LMS_BASE_URL  http://next.atheniq.courses
LOGIN_URL     http://next.atheniq.courses/login
```

while the page itself was served over `https://`. The browser blocked each `http://` call as
mixed content, the app's error boundary showed a generic message, and nothing in the estate
looked broken: the containers were up, the edge answered 200, and the catalog page's own
status was 200. Only the scheme was wrong.

## Where the value comes from

Tutor renders it from a Jinja conditional that follows `ENABLE_HTTPS`:

```jinja
{% if ENABLE_HTTPS %}https{% else %}http{% endif %}://{{ LMS_HOST }}
```

`ENABLE_HTTPS` is `false` here on purpose — tutor's Caddy would then try to obtain its own
certificates on 443, which the edge owns. So the conditional cannot be switched at the
config level without taking the front door away from the edge.

## What was changed, and why it survives `tutor config save`

Two surfaces were edited in the installed tutor packages (tutor 22.0.2 at `/opt/tutor`,
site-packages, `TUTOR_ROOT=/home/tutor/tutor-next`):

1. **Every `ENABLE_HTTPS` scheme conditional** in `tutor/templates/**` and
   `tutormfe/patches/**` now renders `https` unconditionally (8 files: config defaults, the
   LMS/CMS production settings, the shared settings partial, the MFE patches, the init job).
2. **The LMS settings patch** (`tutormfe/patches/openedx-lms-production-settings`) gained

   ```python
   SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
   CSRF_TRUSTED_ORIGINS = list(CSRF_TRUSTED_ORIGINS) + ["https://{{ LMS_HOST }}", "https://{{ CMS_HOST }}"]
   ```

Both are template sources, so a regeneration reproduces them; that is the point, and it was
verified by running `tutor config save` and re-reading the rendered
`env/apps/openedx/settings/lms/production.py`.

Both edits are carried by `scripts/atheniq-lms-https.py`, which is idempotent and takes a
`--dry-run`. It is the whole fix in one command, and the thing to run after an upgrade:

```sh
sudo python3 /usr/local/bin/atheniq-lms-https.py --dry-run   # what would change
sudo python3 /usr/local/bin/atheniq-lms-https.py            # templates
TUTOR_ROOT=/home/tutor/tutor-next /opt/tutor/bin/tutor config save
sudo python3 /usr/local/bin/atheniq-lms-https.py --check \
    /home/tutor/tutor-next/env/apps/openedx/settings/lms/production.py
```

A tutor **upgrade** replaces the package and takes both edits with it, which is why the
edit is a script rather than a memory. `--check` reads the rendered file and fails on any
`http://` platform URL, so the upgrade is visible before a learner finds it; the live
answer is still in `/api/mfe_config/v1?app=catalog`, so that endpoint is the other one to
read after an upgrade:

```sh
curl -s https://next.atheniq.courses/api/mfe_config/v1?app=catalog | grep -o 'http[^"]*atheniq.courses' | sort -u
```

Every line has to start `https://`. If one does not, re-apply the two edits above and run
`tutor config save && tutor local restart lms cms`.

## The durable alternative, not taken

`ENABLE_HTTPS: true` is the supported switch and would set all of this by itself. It also
makes tutor's Caddy demand 443 and its own certificates, and 443 here belongs to the edge —
so the URLs were made right without moving the front door.
