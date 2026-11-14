# Estate alerts — ntfy, and the units that use it

Measured **2026-11-14**.

| | |
|---|---|
| service | `ntfy` container in **`proxy`, on `i2`** |
| software | `binwiederhier/ntfy` **2.32.0**, compose at `/usr/src/proxy/ntfy/docker-compose.yml`, volume `ntfy-cache` |
| address | **`http://192.168.1.71:2586`** — not `:8080` |
| access | `--auth-default-access deny-all`; user **`estate`**, read-write on topic **`estate-alerts`** |
| credential | `/etc/innotel/ntfy.env` on `i1`, mode **600**; never in this repository |

## Why two documents said the estate had no channel

Both statements were mine, and both came from a probe that answered a different
question than the one asked:

1. It probed **`http://192.168.1.71:8080`**, where the estate's **nginx** answers. ntfy
   listens on **2586**. "000" there says nothing about ntfy.
2. It ran `docker ps` on **i1**. `proxy` runs on **i2** — `incus list` on i1 has no
   instance called `proxy`, which is what `Error: Instance not found` meant — and
   `docs/host-map.md` still placed the container on i1.

ntfy had been up the whole time: `docker logs ntfy` shows its per-minute stats line,
`users=2`, and `Up 31 hours` when this was measured. It is started by compose with
`restart: unless-stopped`, so a reboot brings it back without anyone remembering it.

## How a unit opts in

```ini
[Unit]
OnFailure=estate-notify@%n
```

`%n` is the failing unit's full name (`media-orphans.service`), so the instance is
`estate-notify@media-orphans.service` and the template gets `media-orphans` as `%i`.
`%i` drops the type suffix, so the template's `ExecStart` appends `.service` back.

**Do not write `OnFailure=estate-notify@%n.service`.** That produces
`estate-notify@media-orphans.service.service`: systemd starts it, the notification is
sent, and it names a unit that does not exist — so the message reads `estate:
media-orphans failed` and its "journal" is empty. This was written, run, and fixed on
2026-11-14; the wrong form is easy to type because `%n` looks like it needs a suffix.

| piece | where |
|---|---|
| publisher | `scripts/estate-notify.sh` → `/usr/local/sbin/estate-notify.sh` |
| template unit | `systemd/estate-notify@.service` |
| credentials | `/etc/innotel/ntfy.env` on the host that runs the timer |

The publisher reads the unit's own verdict (`systemctl show -p Result -p
ExecMainStatus`), takes the last 25 journal lines as the message, and POSTs them with a
high priority. The password goes to `curl` through a config file on stdin, so it never
appears in `ps`. A host with no `ntfy.env` fails the unit with `CANNOT RUN` instead of
pretending to have notified anyone.

## Every scheduled unit, and why it is or is not wired

The rule, which is the reason half of the table says no: **wire a job whose failure needs a
person and whose next attempt is far away or never.** A job that retries on its own needs a
metric or a log — a topic that buzzes every run for as long as the cause lasts is the
wallpaper `incus-ceiling-check.md` is about.

| unit | cadence | a failure means | wired |
|---|---|---|---|
| `media-orphans` | daily 04:20 | unclaimed media, or an *arr API that could not be read | **yes** |
| `regcache-credential` | daily 06:10 | the Docker Hub token is dead | **yes** |
| `vault-replica-sync` | nightly 03:20 | the mirror did not run; the next attempt is 24 h away | **yes** |
| `proxy-backup` (i2) | weekly, Sun 03:30 | no restore point was written; the next attempt is a week away | **yes** |
| `monarch-torrent-perms` | 5 min | the ownership repair did not stick | **yes** |
| `swap-ceiling-check` | 10 min | a usage threshold was crossed and is still crossed | no — the log line |
| `agent-reaper` | 10 min | a session was skipped, or the scan could not run | no — every run writes `agent-reaper.prom`, so the run is a series, not a one-off |
| `edge-liveness` | 5 min | the edge does not answer | no — it already carries its own mail/webhook channel, and a dead edge would buzz the topic every five minutes |
| `estate-checks` | 30 min | cannot fail: every `ExecStart` is `-` prefixed so each check publishes even when it is unhappy; the finding is its `.prom` | no — there is nothing to wire |
| `sync-mail-identities` | 15 min | a mailbox did not mirror | no — it retries in fifteen minutes, so a wired failure would buzz 96 times a day; **it has no metric yet, which is the open gap** |
| `stalwart-watchdog` | 5 min | the directory recovery did not take | no — `estate-checks`' `check-stalwart-oidc-cache` publishes that outcome |

Serials (`memguard`, `stalwart-oidc-proxy`, `stalwart-oidc-redirect`, `wifi-powersave-off`) are
not scheduled checks: `memguard` is restart-managed, and the other three are enable-time or
long-running units whose state is their own running state.

## What was proven, and how

| claim | evidence |
|---|---|
| the channel works | authenticated POST → **200**; message read back from the topic |
| it is not open | POST with no credentials → **403** |
| a failing unit reaches it | `media-orphans.service` exited 1 on two orphans; `OnFailure` started `estate-notify@media-orphans.service`, which exited **0** and left `estate: media-orphans.service failed (exit-code, rc 1)` on the topic |

```bash
# what the topic holds (as root on i1; the credential never leaves the file)
. /etc/innotel/ntfy.env
printf 'user = "%s:%s"\n' "$NTFY_USER" "$NTFY_PASSWORD" |
  curl -s -K - "http://192.168.1.71:2586/$NTFY_TOPIC/json?poll=1"
```

## The honest gaps

- **Nobody is subscribed.** `subscribers=0`: a message is stored and visible in ntfy's
  web UI and in a phone app *once someone subscribes* to
  `http://192.168.1.71:2586/estate-alerts` as `estate` (or an admin). Until then the
  channel is a place failures are written, not a place they are read.
- **It shares a host with the edge.** `proxy` is the estate edge and ntfy now runs on
  i2 beside it, so a failure of i2 or `proxy` cannot be announced over this channel.
  `systemctl --failed` on each host is still the dependency-free fallback.
- **Plain HTTP, LAN only.** No vhost, no TLS: the topic is reachable to anything on the
  LAN that has the credential, and to nothing outside it.
- **It is a nudge, not a log.** ntfy expires cached messages after its default 12 h.
  The journal is the record; the topic is the tap on the shoulder.
