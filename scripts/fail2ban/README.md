# fail2ban — Bulwark admin logins

Bulwark writes `admin.login_failed` to its audit log and answers `401` (or `429`
once throttled) on `POST /api/admin/auth`. A **rate-limited browser** that still
holds an old admin password looks exactly like a slow, steady attack: one attempt
every N minutes from one address, forever.

This pair stops that at the edge without guessing:

| File | Goes to |
| --- | --- |
| `bulwark-admin.conf` | `/etc/fail2ban/filter.d/bulwark-admin.conf` |
| `bulwark-admin.jail` | `/etc/fail2ban/jail.d/bulwark-admin.conf` |

```sh
install -m 0644 bulwark-admin.conf /etc/fail2ban/filter.d/bulwark-admin.conf
install -m 0644 bulwark-admin.jail /etc/fail2ban/jail.d/bulwark-admin.conf
fail2ban-client reload
fail2ban-client status bulwark-admin
```

## Before you enable it

- **Point `logpath` at the host that actually serves Bulwark.** The proxy in
  front of the estate records a different log format and may not record the
  status code at all; a jail reading a log with no status can never match.
- **Never remove the LAN from `ignoreip`.** The estate's checks and the admin
  workstation live on `192.168.1.0/24`; banning them locks you out of the thing
  you are protecting. The offending rows in Bulwark's audit log naming a LAN
  address are usually those same checks — confirm with the client address in the
  access log before treating one as hostile.
- **Five failures in ten minutes** is generous for a person and tight for a
  script. Lower `maxretry` only after watching a week of real traffic.

## Prove a match

```sh
printf '%s\n' \
  '192.0.2.10 - - [26/Nov/2026:09:30:11 +0000] "POST /api/admin/auth HTTP/1.1" 401 27 "-" "curl/8.0"' \
  '192.0.2.10 - - [26/Nov/2026:09:30:12 +0000] "POST /api/admin/auth HTTP/1.1" 401 27 "-" "curl/8.0"' \
  '192.0.2.10 - - [26/Nov/2026:09:31:00 +0000] "GET /admin HTTP/1.1" 200 900 "-" "Mozilla/5.0"' \
  | fail2ban-regex - /etc/fail2ban/filter.d/bulwark-admin.conf
```

Expect two matches: the `GET` is a page view and is deliberately ignored.
