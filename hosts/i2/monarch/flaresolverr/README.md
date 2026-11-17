# `monarch`'s FlareSolverr patch

`monarch` (`.56`, on `i2`) holds the media estate's 36 containers, `flaresolverr` among
them. This directory is the versioned copy of the two things FlareSolverr needs that no
image or compose file can give it: the patch that fixes how it serves, and the reaper that
stops it leaking browsers. Both were written on the host during the 2026-11-17 incident
and are mirrored here so a rebuilt host does not have to rediscover them.

## Files

| File | Goes to | Why |
|---|---|---|
| `sitecustomize.py` | `/usr/src/projects/complete/3-media/monarch/flaresolverr/sitecustomize.py` on `monarch`, mounted into the container as `/patch/sitecustomize.py` | Pins `waitress.serve(threads=1)`: FlareSolverr's solver queue is the driver's, and parallel solves fight over one browser profile |
| `flaresolverr-reap-chromium` | `/usr/local/bin/flaresolverr-reap-chromium` on `monarch` | Kills browser trees a dead or hung session left in `flaresolverr`'s cgroup: any `chromium`/`chrome`/`chromedriver` whose own age **or an ancestor's** is past 300 s |
| `flaresolverr-reap-chromium.service` | `/etc/systemd/system/` on `monarch` | Runs the reaper once per timer tick |
| `flaresolverr-reap-chromium.timer` | `/etc/systemd/system/` on `monarch` | Every five minutes, `OnBootSec=5min` |

## Why the reaper is on the host and not in the container

Measured 2026-11-17: `flaresolverr` held **78** `chromium` processes under one two-hour-old
browser with **zero** requests in the hour before, no `chromedriver` alive, and 2.95 GiB of
anon against a 3 GiB cap — so the next solve was the one the kernel killed. The tree is
what a solve leaves when its driver dies, and serialising solves cannot reach it.

A reaper written *inside* the container looked right and killed nothing: every
`os.kill` raised `PermissionError`, because the browser trees are not owned by the user
the app runs as. Removing that version and putting the same rule on the host as root is
the fix; the in-container thread would have sat in the module looking healthy forever.

The age comes from `ps -o etimes=` and deliberately not from `/proc/uptime`: Incus masks
uptime per container, so `uptime - starttime` is *negative* and nothing is ever old enough
to kill. That cost a round each time it was tried — the script says so where it matters.

The rule reads **ancestry, not only the process**. When a browser crashes inside a live
session, ChromeDriver relaunches it, so every browser process is seconds old while the
session owning them is not; an age-only rule fires never. Measured 2026-11-17 while this
file was being written: one 90-minute-old `chromedriver`, 65 fresh `chromium` children, the
cgroup at its 3 GiB cap, 1033 OOM kills and no solve in the log. The ceiling is 300 s
against prowlarr's 180 s `requestTimeout`, so nothing a person is waiting for is that old.

## Install / re-apply

```sh
# from the repository root, against i2
incus file push hosts/i2/monarch/flaresolverr/sitecustomize.py \
  monarch/usr/src/projects/complete/3-media/monarch/flaresolverr/sitecustomize.py
for f in flaresolverr-reap-chromium flaresolverr-reap-chromium.service flaresolverr-reap-chromium.timer; do
  incus file push "hosts/i2/monarch/flaresolverr/$f" "monarch/tmp/$f"
done

incus exec monarch -- bash -c '
  install -m 0755 /tmp/flaresolverr-reap-chromium /usr/local/bin/
  install -m 0644 /tmp/flaresolverr-reap-chromium.service /tmp/flaresolverr-reap-chromium.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now flaresolverr-reap-chromium.timer
  docker restart flaresolverr'
```

`incus file push … monarch/…` reaches `monarch`'s filesystem from `i2`; the `incus` calls
need `</dev/null` when they run over a non-interactive ssh.

## Verify

```sh
# the timer is the thing that has to be running
systemctl is-enabled flaresolverr-reap-chromium.timer    # enabled
systemctl list-timers flaresolverr-reap-chromium.timer

# the kill path, against a browser started a moment ago
docker exec -d -u root flaresolverr /tmp/chromium 300
FLARESOLVERR_CHROMIUM_MAX_AGE_S=0 /usr/local/bin/flaresolverr-reap-chromium
# → killed N Chromium process(es) older than 0s

# and that a solve still works afterwards
curl -s -X POST http://127.0.0.1:8191/v1 -H 'Content-Type: application/json' \
  -d '{"cmd":"request.get","url":"https://example.com","maxTimeout":60000}' | head -c 60
```

`check-media-apps.py` publishes what the solver is doing — `memory_current` against the
cap and the cumulative `oom_kill` — so the estate's own alerts see a solver pinned at its
ceiling with nothing to do, which is the state this directory exists to prevent.
