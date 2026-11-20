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
| `i3` `signara`, `signara-alertmanager-1` + `signara-api-1` | `192.168.1.15:587`, `alertmanager@innotel.us`, password set | delivers, but `SMTP_TLS_INSECURE=true` with `SMTP_TLS_SERVERNAME` empty | Works, with **certificate verification off**. The app password is also readable through `docker inspect` on both containers. Delivery itself is re-proven every run by `scripts/check-alert-delivery.py`. |
| `i4` `proxy`, `cerulean-authentik-worker` | no `AUTHENTIK_EMAIL__*` at all | Authentik falls back to `localhost:25`; that container has no MTA | **Silently fails.** Account-recovery and notification mail cannot leave the container. |
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
- The `signara` TLS posture and the Authentik gap were **recorded, not changed**: each is
  a decision about the estate's security posture or product mail, not a credential
  rotation.

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
