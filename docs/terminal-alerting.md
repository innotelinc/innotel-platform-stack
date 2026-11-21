# Terminal host alerting — the termix timers and the public probe

Measured **2026-11-21**.

| | |
|---|---|
| host | `terminal` (incus container, `192.168.1.22`, runs on `i4`) |
| service | Termix **26.11.3**, compose `/usr/src/docker-compose.yaml`, gate script `/usr/src/termix/termix-entry` |
| channel | ntfy in **`proxy`**, `http://192.168.1.71:2586`, topic **`estate-alerts`** |
| credential | `/etc/innotel/ntfy.env` **on `terminal`**, mode 600, user **`terminal-alerts`**, read-write on `estate-alerts` only |
| publisher | `/usr/local/sbin/estate-notify.sh` + `estate-notify@.service` — the same pair as every other host |

## Why this exists

On **2026-11-21** Termix was upgraded `26.11.1 → 26.11.3`. The new image ships
**neither `wget` nor `curl`**. Two probes this host had written in terms of `wget`
therefore could not run:

1. the compose `healthcheck`, which asked `wget` for `30001/health` and the default
   layout's bundle — so the container could never report healthy;
2. `termix-entry`, which lifts the nginx **cold-start gate** the same way — so the
   gate never lifted and **every visitor got 503** while `docker compose up -d` had
   reported success and the container was `Up`.

Nothing paged. The only signal was `termix-health-check.service` failing **every
five minutes** into `/var/log/termix-health.log`, which nobody reads. Both probes are
now `node -e fetch` (node is the one HTTP client the image is guaranteed to have), and
the three units below exist so the next failure of this shape reaches a person.

## The units

| unit | cadence | a failure means | wired |
|---|---|---|---|
| `termix-update.service` | daily 00/6:20 (+30 min jitter) | the release was not deployed, or the rollback did not take | **yes** — `OnFailure=estate-notify@%n` |
| `termix-health-check.service` | every 5 min | Termix is up but a plugin bundle is not servable (the desktop shell then quits with "Failed to import module") | **yes** — `OnFailure=estate-notify@%n` |
| `termix-public-watch.service` | every 5 min | `https://terminal.innotel.us/` stopped answering 200 | **partly** — the script pages once itself; `OnFailure` only covers the script crashing |

## Why the public probe is not a plain `OnFailure`

The estate rule (see `estate-alerts-ntfy.md`) is that a job which retries on its own
must not buzz every run for as long as the cause lasts. A five-minute HTTP probe
wired straight to `OnFailure` would do exactly that: 288 messages a day, and the
first thing anyone does with that topic is ignore it.

So `termix-public-watch` is a state machine:

* it pages **once**, on the **second consecutive non-200** — one 503 is a normal
  restart (the gate answers 503 while plugins activate), two in a row is not;
* it sends **one** recovery notice when 200 comes back;
* both are deduped by `/var/lib/termix-public-watch/{failing,alerted}`, so a systemd
  restart or a re-run does not re-page;
* it exits 0 on both up and down (the state file and the journal carry the verdict),
  which is why `OnFailure` on it means "the probe itself crashed", not "the site died".

The page body names the four things to look at — `docker ps` for `termix`,
`docker logs termix` for `gate lifted`, the health unit, and a direct probe of
`30001/health` — because the 2026-11-21 incident was only diagnosable from
the container's own log line `[termix-entry] gate: ... left answering 503`.

## The credential

`estate-notify.sh` reads `NTFY_URL`, `NTFY_TOPIC`, `NTFY_USER` and `NTFY_PASSWORD`
from `/etc/innotel/ntfy.env` (mode 600, never in this repository). On this host that
user is **`terminal-alerts`**, created 2026-11-21 with read-write on `estate-alerts`
and nothing else, because the estate's shared `estate` password lives on `i1` and this
host cannot read it. To use the shared account instead, replace the two lines in the
env file and remove the extra user:

```
docker run --rm --entrypoint sh -v ntfy-cache:/var/cache/ntfy \
  -v /tmp/ntfy-cli.yml:/etc/ntfy/server.yml binwiederhier/ntfy:latest \
  -c 'ntfy user remove terminal-alerts'
# /tmp/ntfy-cli.yml: auth-file: /var/cache/ntfy/auth.db
```

The password is handed to `curl` through a config file on **stdin** (`-K -`), so it
never appears in `ps` — same as `estate-notify.sh`.

## Measured

* `termix-public-watch test` → `notified: termix: watch test (terminal.innotel.us)`.
* simulated unreachable target with `THRESHOLD=1` and a temp `STATE_DIR` →
  `notified: ... is DOWN (HTTP 000)` once; the immediate second run was silent.
* throwaway unit with `OnFailure=estate-notify@%n` → `notified: estate-alerts <- termix-alert-selftest.service`.
* real probe: `OK https://terminal.innotel.us/ -> 200`.

## What is not wired

* A **single** failed probe does not page, by design; 503 during a restart is expected.
* `termix-health-check` treats 503 from the gate as `STARTING` and exits 0, so a slow
  cold start (measured up to 186 s on this host under load) is not a page.
* The probe only asserts HTTP 200 on `/`. It does **not** log in or exercise the
  desktop shell's plugin imports; `termix-health-check` does check the layout bundle
  (`/plugin-assets/classic-layout/frontend.js` > 100 kB).
