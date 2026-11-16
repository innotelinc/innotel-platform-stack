# Why the monarch copy to i2 died, and how migrations are run from now on

## The failure

On 2026-11-16, `incus copy monarch i2:monarch --instance-only` transferred 13.6 GB of
about 25 GB and then aborted:

```
level=error msg="Failed migration on source" err="Error reading migration control target:
websocket: close 1006 (abnormal closure): unexpected EOF" instance=monarch live=false push=false
```

Two things make this look like a network fault when it is not.

**The destination daemon restarted.** On i2:

| fact | value |
| --- | --- |
| `systemctl show incus -p ExecMainStartTimestamp` | `Mon 2026-11-16 15:42:34 UTC` (= 10:42:34 EST) |
| `NRestarts` | `0` — so it was restarted by hand or by a package trigger, not by systemd |
| source abort | `10:42:35` — **one second later** |

A migration is one control websocket plus one data stream between the two daemons. When
incusd on i2 went away, the source's control read returned EOF and the whole operation
failed. The `1006` is the *symptom*: the destination closed without a close frame.

i2's own log at that exact second shows the daemon on its way down, not a network fault:

```
time="2026-11-16T15:42:34Z" level=warning msg="Failed shutting down instance, forcefully stopping"
    err="Failed shutting down instance, status is \"Running\": context deadline exceeded"
```

So the destination was being torn down (and force-stopping its own instances) while the copy
was streaming into it. Nothing on the source could have prevented that; only a preflight that
refuses a destination daemon younger than ten minutes, plus a resumable transport, makes the
next attempt survive it.

**The client was disposable.** The copy ran inside `timeout 560 ssh ... bash -s` issued from
another session. Anything that kills that ssh channel — the `timeout`, a session ending, an
agent's own cleanup — aborts the transfer the same way and is indistinguishable in the log.

## What incus does not do

- No retry, no resume: 13.6 GB is thrown away and the run starts from zero.
- No pacing: it reads the source volume at full speed, which on i1 (a single spindle shared
  with mail/ontrak/atheniq) drove `/proc/pressure/io` to ~95%.
- No preflight on the destination: it will happily start a 25 GB transfer two seconds after
  the destination daemon booted.
- No cleanup on failure: i2 was left holding a 22 GB instance with **0** docker state dirs and
  **0** overlay2 dirs — an instance that looks real and is unusable.

## The rules adopted

1. **Never migrate onto a daemon that just started.** Require `ExecMainStartTimestamp` to be at
   least 10 minutes old. A restart is the proven cause; this check makes it impossible.
2. **Never run the transfer inside a disposable shell.** Use `setsid nohup` (or tmux) so client
   death cannot abort it, and log to `/var/log/incus-migrate/`.
3. **Pace and resume.** `rsync --bwlimit --partial` re-runs only send the difference, so a failed
   attempt costs minutes, not the whole volume.
4. **Build the destination config first.** `incus init --empty` plus explicit config/devices,
   verified before any data lands (`volatile.idmap.next` must match the source idmap, otherwise
   every file lands with the wrong owner).
5. **Verify by counting, not by "it finished".** File count on both sides must match; check
   `var/lib/docker/containers` and `overlay2` counts, because those are what a truncated copy
   loses first.
6. **One session at a time.** `incus-guard claim <instance>` before touching it; exit 75 means
   someone else is mid-work.

## The tools

- `scripts/incus-migrate-safe` — preflight (stopped source, healthy destination, free space,
  matching entry counts), read-only source mount, paced rsync with retries, detached mode.
- `scripts/incus-guard` — per-instance lease (`claim`/`release`/`status`/`run`) so two agent
  sessions cannot both act on one instance.

Verified on the 2026-11-16 monarch move (i1 -> i2): 25.4 GB, entry counts matched, docker
state intact.
