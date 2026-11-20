# Estate mail senders — audit (2026-11-20)

**Why this exists.** The AthenIQ LMS was handing every message to a local relay that had
no smarthost, so its mail queued forever while the stack looked healthy — the same
"configured but silent" shape this repo already carries for Alertmanager
(`container-placement.md`, §*The estate's one Alertmanager delivers*). That was found by
accident while rotating a credential, so this pass looked for the shape everywhere
rather than waiting for the next accident.

Two shapes count as broken, and they look identical from the outside:

1. **A relay with no destination.** `SMARTHOST` (and usually the credentials) empty, so
   the relay accepts mail and queues it.
2. **A sender pointed at nothing.** A mail host that is empty, `localhost`, or an MTA
   the container does not have, so the send either fails silently or is dropped.

## How each service was measured

1. **Container environment sweep.** Every running container on `i1`–`i4`
   (incus → docker) had its `Config.Env` filtered for
   `MAIL|SMTP|EMAIL|EXIM|RELAY|SENDGRID|RESEND|POSTMARK|MAILGUN|AWS_SES|MSMTP|SENDMAIL|POSTFIX`,
   with any value whose key names a password/secret/key/token reduced to
   `<set:length>` or `<EMPTY>`. Roughly 120 containers.
2. **Rendered-config probe** for the apps that keep mail settings in files, where an env
   sweep cannot see them: Gitea `app.ini [mailer]`, Grafana `[smtp]` (both instances),
   Alertmanager's `email_configs` and its `SMTP_*` env, Coolify's `instance_settings`
   row, Authentik's `AUTHENTIK_EMAIL__*`.
3. **Host MTA probe.** `sendmail` presence on each host, and `MAILTO` in
   `/etc/crontab` and `/etc/cron.d`.

## Findings

| Where | Transport as configured | State | Verdict |
|---|---|---|---|
| `i1` `atheniq`, `tutor_local-smtp-1` | `devture/exim-relay`, `SMARTHOST` + `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_USERDOMAIN` all empty | LMS rendered `EMAIL_HOST=smtp`, `EMAIL_PORT=8025`, no auth | **Broken — fixed 2026-11-20.** Relay retired; Tutor's `SMTP_*` keys submit to `mail.innotel.us:587` with STARTTLS and the `atheniq@innotel.us` app password. Proven delivered (`docs/atheniq-stays-on-59-2026-11-20.md`). |
| `i3` `signara`, `signara-alertmanager-1` + `signara-api-1` | `mail.innotel.us:587`, `alertmanager@innotel.us`, password from a mounted file | fixed 2026-11-20: `SMTP_TLS_SERVERNAME=mail.innotel.us`, `SMTP_TLS_INSECURE=false`, `SMTP_PASS` no longer in either container's environment | **Fixed** (was: verification off and the password visible to `docker inspect`). Delivery re-proven every run by `scripts/check-alert-delivery.py`; see *The two gaps, closed*. |
| `i4` `proxy`, `cerulean-authentik` + `cerulean-authentik-worker` | `AUTHENTIK_EMAIL__*` → `mail.innotel.us:587`, STARTTLS, `admin@innotel.us` | fixed 2026-11-20: was falling back to `localhost:25`, which has no MTA in that container | **Fixed.** Recovery and notification mail now leaves the container; the one password stays in the stack's `.env` because Authentik has no file-backed secret option. |
| `i2` `coolify` | `instance_settings` has `smtp_host`, `smtp_port`, `smtp_encryption`, `smtp_from_address` all `NULL`, no username/password | not configured | No mail; Coolify notifications are off. |
| `i2` `atlas`, `atlas-gitea` | `[mailer] ENABLED = false` | deliberate | No mail (password reset / activation mail off). |
| `i2` `monarch` and `i4` `proxy`, `metrics-grafana` | `[smtp]` block entirely commented out | deliberate | No mail; Grafana alerting cannot email. |
| `i2` `monarch`, `jellyfin`, `sonarr`, `radarr`, `lidarr`, `bazarr`, `prowlarr` | no SMTP strings under `/config`, no notification env | not configured | No mail. |
| `i2` `monarch`, `watchtower` | no `WATCHTOWER_NOTIFICATION_*` env | not configured | No mail. |
| `i1` `mail`, `bulwark` | JMAP client (`JMAP_SERVER_URL=https://mail.innotel.us`) | presents mail, submits through Stalwart's own JMAP | Fine — it is not an SMTP sender. |
| `i4` `proxy`, `cerulean` | `MAIL_PROVISION_URL=http://192.168.1.15:8091` | mailbox provisioning over Stalwart's API, not SMTP | Fine; outside this audit's scope. |
| all hosts `i1`–`i4` | no `sendmail`, no `MAILTO` in `/etc/crontab` or `/etc/cron.d` | — | Nothing to deliver; cron output is not mailed anywhere by design. |

## What this audit does not cover

- **App-internal notification settings stored in each app's own database** (*arr
  notification agents, Jellyfin plugins, ntfy, Coolify's per-project notification
  targets). Reading those means querying each app, not reading a file; the env sweep
  would only show a transport these apps do not use.
- **Outbound deliverability** (SPF/DKIM/DMARC, reputation, port 25). That is a separate
  check — `scripts/check-mail-auth.py` — and it is not re-derived here.
-The `signara` TLS posture and the Authentik gap were **recorded first and closed later the
same day** — see *The two gaps, closed* below for what changed and how each was proven.

## The two gaps, closed (2026-11-20)

### Signara: verification on, password out of the environment

The stack dialled the relay by its LAN address with `SMTP_TLS_INSECURE=true` and an empty
`SMTP_TLS_SERVERNAME`, so STARTTLS happened but the certificate was never checked, and the
app password sat in `.env` where `docker inspect` reads it. Both are gone:

- `.env` now carries `SMTP_HOST=mail.innotel.us`, `SMTP_TLS_SERVERNAME=mail.innotel.us`
  and `SMTP_TLS_INSECURE=false`. The blog post in `alertmanager.yml` already called
  `server_name` "the fix that keeps verification ON"; it is now the setting in use.
- the password lives in
  `/usr/src/projects/complete/1-primary/signara/infra/secrets/smtp_password`, mounted
  read-only at `/run/secrets/smtp_password` into both consumers. Alertmanager reads it
  with `smtp_auth_password_file`; the API reads it through `SMTP_PASS_FILE` in
  `apps/api/src/config/configuration.ts` (`readSecret()` falls back to the plain variable
  where no file is named). Neither container has `SMTP_PASS` in its environment.
- the file is mode **0444**, not 0600, because that was a measured failure: at 0600 both
  consumers were refused (`passlen=0` from the API and `could not read
  /run/secrets/smtp_password: permission denied` in Alertmanager's notify log) —
  Alertmanager runs as `nobody` and the API as `signara`. The file lives inside the
  single-tenant `signara` container, which is what makes world-readable acceptable there.
- the app password was rotated in the process (Stalwart ids `c` → `d`; `c` was printed
  into a session transcript by an inspection command before the change, so it is deleted,
  and `d` is now the account's only app password).

Proven: `amtool check-config` says SUCCESS on the rendered file, which shows
`smtp_smarthost: 'mail.innotel.us:587'`, `smtp_auth_password_file`,
`server_name: 'mail.innotel.us'` and `insecure_skip_verify: false`; the API's own config
module resolves a 40-character password from the file and `nodemailer.verify()` returns
`smtp-auth-ok` over STARTTLS; and a synthetic `critical` alert posted to Alertmanager's
API arrived in `admin@innotel.us` at `2026-11-20T20:34:12Z` with verification on.

These edits are in the **deployment checkout** (`/usr/src/projects/complete/1-primary/signara`)
and are not committed to the Signara repo — that checkout already carried local
modifications before this pass. `groups/1-primary.yml` here is deliberately untouched: it
is generated from the member repos by `scripts/gen-group-compose.py` and CI checks it, so
hand-editing it would only make the drift invisible.

### Authentik: a sender that exists

Both `authentik-server` and `authentik-worker` in cerulean's compose now carry
`AUTHENTIK_EMAIL__HOST=mail.innotel.us`, `__PORT=587`, `__USE_TLS=true`, `__USE_SSL=false`,
`__TIMEOUT=10`, `__FROM="AthenIQ Identity <admin@innotel.us>"`,
`__USERNAME=admin@innotel.us` and `__PASSWORD=${AUTHENTIK_SMTP_PASSWORD}` from the stack's
`.env`. The credential is the Stalwart app password *Authentik system mail to Stalwart
(2026-11-20)* on `admin@innotel.us`; a first attempt on the same account produced an
orphaned password whose secret was lost between steps, and it was destroyed rather than
left behind.

Authentik 2026.8.6 has **no email settings model** (mail is configured from
`authentik/lib/default.yml` overridden by env) and no file-backed secret option, so this
password necessarily sits in the stack's `.env` and is therefore visible to `docker inspect` — the asymmetry with Signara is a limitation of the product, not
an oversight.

Proven: the worker's `CONFIG` reports host/port/TLS/username/from and a 40-character
password, and `send_mail` from that worker arrived in `admin@innotel.us` at
`2026-11-20T20:28:54Z`.

### Re-verifying both

    docker exec signara-alertmanager-1 /bin/amtool check-config /tmp/alertmanager.rendered.yml
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' signara-api-1 | grep -c '^SMTP_PASS='   # 0
    docker exec cerulean-authentik-worker ak shell -c \
      'from authentik.lib.config import CONFIG; print(CONFIG.get("email.host"), CONFIG.get("email.port"))'

## Re-running the sweep

The env half is one command per host:

```sh
for h in i1 i2 i3 i4; do echo "== $h"; ssh "$h" bash -s <<'EOF'
for c in $(incus list --format csv -c n,s | grep 'RUNNING$' | cut -d, -f1); do
  incus exec "$c" -- bash -c '
    for ct in $(docker ps --format "{{.Names}}"); do
      docker inspect --format "{{json .Config.Env}}" "$ct" 2>/dev/null |
        tr "," "\n" | grep -Ei "^(MAIL|SMTP|EMAIL|EXIM|RELAY|SENDGRID|RESEND|POSTMARK|MAILGUN|AWS_SES)" |
        sed "s/^/'"$c"' '"$ct"' /"
    done' 2>/dev/null
done
EOF
done
```

Treat a hit with `=<EMPTY>` in a relay's `SMARTHOST`, or an `EMAIL_HOST` of `localhost`,
as the failure this document was written for. Values are deliberately printed in full
only for non-secret keys — never paste a `*PASS*`/`*SECRET*` hit into a ticket or a doc.

## The check that keeps them fixed

A sweep is a moment, and the fix it proves is a compose edit nothing watched: the mail
checks all pass on a stack that mails perfectly and hands its password to `docker
inspect`. So `scripts/check-smtp-credentials.py` is now in the `estate-checks` unit and
reads the one thing none of the others look at — the environment of every running
container on all four hosts. Measured on its first run: **165 containers in 21 Docker
instances**. A key naming a mail transport (`SMTP`, `MAIL`, `EMAIL`) and a credential
(`PASS`, `PASSWORD`, `SECRET`) with a non-empty value is a failure; a name ending
`_FILE`/`_PATH`/`_DIR` is the fix and is skipped; a host that cannot be read is exit 2,
never a pass.

**It found two senders the table above did not have.** Both are inside `vault` on `i1`,
both carry a 40-character Stalwart app password as plain environment:

| Container | Variable | State |
|---|---|---|
| `vaultwarden` | `SMTP_PASSWORD` | **fails** the check — not exempted |
| `atheniq-console` | `MAIL_SMTP_PASS` | **fails** the check — not exempted |

They are deliberately left failing rather than added to the exemption list: an exemption
is for a case the product makes impossible (Authentik), and these two have options —
mount the value and read a `*_FILE` name where the app supports one, or, where it does
not, a per-service app password rotated on a schedule instead of the shared one. Until
then the new textfile says so and `EstateCheckFailing` names the containment, which is a
page with an owner rather than a quiet `docker inspect`.

Authentik is the one named exemption, keyed by host, instance **and** variable
(`i4`, `proxy`, `AUTHENTIK_EMAIL__PASSWORD`) so it cannot cover a different container
that starts carrying the same name; it publishes as a note, so the deliberate case still
reaches the dashboard instead of reading as a missing row.

Re-verify by hand — it is the same read the check does, and it prints no values:

    python3 scripts/check-smtp-credentials.py            # 0 = clean, 1 = a leak, 2 = unreadable host
    cat /var/lib/node_exporter/textfile/estate-check-smtp-credentials.prom

Installed on the edge the same day: the unit names it, `systemctl start
estate-checks.service` came back `Result=success` / `ExecMainStatus=0` (a failing check is
a textfile, not a failed unit), and the series published `last_status 0`, `failures 2`,
`notes 2` — the two `vault` containers above, plus Authentik's two deliberate notes.
