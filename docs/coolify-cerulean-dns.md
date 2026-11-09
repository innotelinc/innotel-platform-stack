# Coolify → Cerulean DNS

Coolify's managed-DNS feature publishes only to Cloudflare (4.6.0 ships
`CloudflareDnsProvider` and nothing else), and the estate's zones live in Cerulean
(Technitium). So the Coolify host publishes the same addresses itself: a small
publisher reads Coolify's Postgres, works out which hostname should resolve to
which server's public IPv4, and writes A/CNAME records into the owning Cerulean
zone through the Cerulean service API. Nothing in Coolify is patched, so a Coolify
upgrade neither loses the behaviour nor has to be re-done.

## What runs where

| File (`scripts/`) | Job |
| --- | --- |
| `coolify-cerulean-dns.py` | The publisher. Dry run by default, `--apply` writes. Reads `instance_settings`, `applications`, `application_previews`, `service_applications`, `servers` through `docker exec coolify-db psql`. |
| `coolify-cerulean-dns-watch.py` | Listens on the Docker event stream and runs the publisher 10 s after the last deployment-ish event (`coolify*`/`traefik` ignored). |
| `coolify-cerulean-dns-health.py` | Drift check: compares Coolify's configuration with the Cerulean zone *and* asks Cerulean's resolver whether each name answers; mails `CERULEAN_DNS_ALERT_TO`. |
| `coolify-cerulean-dns-status.py` | One table: hostname, resource, server, zone, expected vs Cerulean, state. `--detail` adds the resource name and when its Coolify row last changed; `--json` for scripts. |

Units (`systemd/`): `coolify-cerulean-dns.service` (oneshot publisher),
`.timer` (daily 03:17 drift net for domain edits that never redeploy),
`coolify-cerulean-dns-watch.service` (event listener, always on),
`coolify-cerulean-dns-health.service` + `.timer` (15 min).

## Install

```sh
install -m 0755 scripts/coolify-cerulean-dns*.py /root/
install -m 0644 systemd/coolify-cerulean-dns*.service systemd/coolify-cerulean-dns*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now coolify-cerulean-dns-watch.service coolify-cerulean-dns.timer coolify-cerulean-dns-health.timer
```

Credentials in `/root/.cerulean-coolify` (0600), read by the publisher:

```
CERULEAN_URL=http://192.168.1.71:3003
CERULEAN_TOKEN=<Cerulean service token: dns:read dns:write domains domains:read>
```

Alert channel in `/etc/innotel/coolify-cerulean-dns.env` (`CERULEAN_DNS_ALERT_TO`,
`CERULEAN_DNS_SMTP_HOST=192.168.1.15`, `CERULEAN_DNS_SMTP_PORT=25`). Unset address
means the failure only lands in the journal and the failed unit.

## What it writes, and what it will not touch

One record per hostname Coolify has configured (instance FQDN, applications,
previews, service applications), in the most specific Cerulean zone that owns the
name, pointing at the public IPv4 of the server the resource runs on (server 0 =
the localhost server = the instance's public IPv4). `*.sslip.io`, `*.nip.io`,
`.local`, `.internal.test` names are skipped — Coolify's magic wildcards are not
real DNS.

Every record it creates is remembered in `/var/lib/coolify-cerulean-dns/state.json`
and that file is the only thing it will delete. A same-name A/CNAME it did not
create is reported as `hold` and left alone; a name configured for two servers
with different addresses is reported and skipped, not flipped per deployment.

## CNAMEs

A Coolify domain that should follow another name rather than duplicate its address
goes in `/etc/innotel/coolify-cerulean-dns.aliases` (override with
`COOLIFY_CERULEAN_ALIASES`), one per line:

```
www.example.com -> example.com
```

Switching a name between A and CNAME is handled: the owned record of the other
type is removed and the new one written; a prune removes the record when the name
leaves Coolify.

## Checking it

```sh
python3 /root/coolify-cerulean-dns.py            # dry run: what would change
python3 /root/coolify-cerulean-dns-status.py     # what is where
python3 /root/coolify-cerulean-dns-status.py --detail
python3 /root/coolify-cerulean-dns-health.py     # exit 1 = drift, 2 = check could not run
```

The health check mails one message per distinct drift and repeats it after 12 h
(`CERULEAN_DNS_ALERT_REPEAT_HOURS`), so one broken domain cannot mail every 15
minutes. Resolution checks are on by default against the `CERULEAN_URL` host; set
`CERULEAN_DNS_SERVER` to name another resolver or `COOLIFY_CERULEAN_RESOLVE=0` to
skip them.

## Troubleshooting

**`Technitium login failed (HTTP 200): Invalid username or password for user: admin`**
Cerulean's `TECHNITIUM_PASSWORD` on the Cerulean host no longer matches Technitium's
admin password. Fix `/usr/src/projects/complete/1-primary/cerulean/.env` and
`docker compose up -d --force-recreate cerulean`. Five bad logins block logins for
300 s, so test the password once before recreating.

**`hold` lines** — the name exists in Cerulean but not as a record this publisher
created. Decide by hand; nothing is overwritten.

**A deployment fails with `Railpack deployments require the Docker buildx CLI plugin`**
The Ubuntu `docker.io`/`docker-ce` install here does not ship the plugin. It belongs
at `/usr/libexec/docker/cli-plugins/docker-buildx` (plus `~/.docker/cli-plugins` for
`DOCKER_CONFIG=/root/.docker`); the Coolify helper image already carries its own.

**Status says `missing` and the record exists** — the zone queried is the longest
Cerulean zone that matches the name; check `curl /api/service/domains` for the zone
list the service token can see.
