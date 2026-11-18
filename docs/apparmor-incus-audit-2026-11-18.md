# The AppArmor signal denial, and which hosts are exposed to it

Measured 2026-11-18, after the `signara` outage of 2026-11-15 → 2026-11-18.

## The class

Docker generates an AppArmor profile named `docker-default` for every container. Under
nested Incus the container's **peer label is namespaced**:

```
apparmor="DENIED" operation="signal" namespace="root//incus-signara_<var-lib-incus>"
profile="docker-default" pid=… comm="postgres" requested_mask="send"
signal=urg peer="docker-default//&unconfined"
```

The stock profile grants `signal` to a peer named exactly `docker-default`. The peer is
named `docker-default//&unconfined`, so the rule does not match and **a container cannot
signal its own processes**. Two symptoms, and the second is why it took three days:

| Where | What it looks like |
| --- | --- |
| postgres | `FATAL: could not signal for checkpoint: Permission denied`, `pg_ctl: could not send stop signal (PID: 53): Permission denied` |
| any image that drops privileges with a Go helper (`gosu`) | the helper signals its own threads, is denied, and **retries forever at ~100% CPU without ever `exec`ing the server**. PID 1 stays the helper, logs stay empty, the container stays `running` — so `restart: unless-stopped` never fires and the healthcheck is the only thing telling the truth. |

The database was never at risk in the `signara` case; the volume was intact and served as
soon as signals were permitted.

## The fix, and why the profile must be *named* `docker-default`

`daemon.json` → `"apparmor-profile": "<path>"` takes the **path of the profile file**;
Docker loads that file and applies the profile *named inside it*. So the value is
`/etc/apparmor.d/docker-default` and the file must declare `profile docker-default`.
Measured 2026-11-18: putting the bare *name* in that key makes `dockerd` refuse to start
(and take every container on the host with it), which is why the installed script now
validates the file with `dockerd --validate` before bouncing anything.

The profile is loaded in **complain**: the rules are audited rather than enforced, which
is equal to allowing them while a denial still reaches the kernel log.

```sh
incus exec <guest> -- bash -s -- --check < scripts/install-docker-apparmor-profile.sh
incus exec <guest> -- bash -s -- --apply < scripts/install-docker-apparmor-profile.sh
```

`--apply` restarts Docker to make the daemon read the new key; `live-restore: true`
keeps running containers up. Containers created before the change keep their old label and
need one recreate. Verified on `signara`: `docker-default (complain)`, `CHECKPOINT`
returns `CHECKPOINT`, 245 tables, `/ready` → `200`.

## Audit of every Docker-bearing Incus guest

Read from each guest's own kernel and daemon config. `signal_denials` is
`journalctl -k | grep -c 'DENIED.*operation="signal"'`.

| Host | Guest | Containers | `apparmor-profile` | Profile loaded | Signal denials |
| --- | --- | --- | --- | --- | --- |
| i1 `.51` | `atheniq` | 21 | unset — **daemon has no such option** | `docker-default (enforce)` | 0 |
| i1 `.51` | `genesis` | 1 | unset | `docker-default (enforce)` | 0 |
| i1 `.51` | `mail` | 1 | unset | none | 0 |
| i1 `.51` | `ontrak` | 28 | `/etc/apparmor.d/docker-default` | `docker-default (complain)` | 0 before, 0 after |
| i1 `.51` | `onyx` | 14 | unset | `docker-default (enforce)` | 0 |
| i1 `.51` | `vault` | 8 | unset | none | 0 |
| i1 `.51` | `workloads` | 1 | unset | `docker-default (enforce)` | 0 |
| i2 `.52` | `atlas` | 4 | unset | `docker-default (enforce)` | 0 |
| i2 `.52` | `genie-preview` | 1 | unset | `docker-default (enforce)` | 0 |
| i2 `.52` | `monarch` | 41 | `/etc/apparmor.d/docker-default-monarch` | `docker-default (complain)` | 0 |
| i2 `.52` | `regcache` | 1 | unset | `docker-default (enforce)` | 0 |
| i2 `.52` | `rizzaura` | 5 | unset | `docker-default (enforce)` | 0 |
| i3 `.53` | `signara` | 10 | `/etc/apparmor.d/docker-default-signara` | `docker-default (complain)` | **134 before the fix, 0 after** |
| i4 `.54` | `proxy` | 24 | unset | `docker-default (enforce)` | 0 |
| i4 `.54` | `telno-turn` | 1 | unset | `docker-default (enforce)` | 0 |

## What this means

**Three guests are fixed** (`monarch`, `signara`, and `ontrak` as of 2026-11-18).
**Eleven Docker daemons still run the stock profile in enforce** — `atheniq` (21
containers), `proxy` (24), `onyx` (14), `vault` (8), `rizzaura` (5), `atlas` (4) and the
single-container guests.

None of them had logged a signal denial at the time of the audit, so none is wedged. The
exposure is real but **latent**: the denial only bites a workload that signals across the
namespaced boundary — the postgres entrypoint's checkpoint/`urg` path, or a Go privilege
drop. `ontrak` and `atheniq` both run Postgres images whose entrypoint drops with
`gosu`, which is exactly the composition that failed on `signara`, so they are the two
worth applying this to first.

Applying it is one command per guest, and the containers that already exist keep the
label they were created with until they are recreated once — the daemon-wide selection
only covers containers started after it.

### `atheniq` cannot take it at all

`atheniq` runs Docker **29.1.3**, which has no `--apparmor-profile` option: `dockerd
--help` has no match for `apparmor` (`grep` exits 1), and a `daemon.json` carrying the
key is refused outright —

```
unable to configure the Docker daemon with file /etc/docker/daemon.json:
the following directives don't match any configuration option: apparmor-profile
```

So there is no daemon-wide selection on that host. Per-container
(`--security-opt apparmor=docker-default`) is the remaining option, and that means
recreating the containers, which is a decision about a running service rather than a
config change. `atheniq` therefore stays on the stock profile in enforce; it had logged
no denial, so nothing is wedged today.

### What applying it cost, and the guard it produced (2026-11-18)

The first `--apply` on `atheniq` wrote the key, restarted Docker, and **stopped all 21
containers on the guest** — the daemon exited with the message above, and the script
reported the failure only after the fact. `atheniq` was restored from the script's own
`daemon.json.bak-<epoch>` within minutes (`docker` active, 22 containers up, 0
unhealthy, mysql/mongodb/redis/meilisearch healthy).

The script no longer has that failure mode: it asks `dockerd --validate
--config-file=` **before** restarting, and rolls the config back if the daemon would
reject it or refuses to come up, so an unsupported host is a refusal rather than an
outage. Both paths were exercised for real: `atheniq` now answers
*"this daemon does not accept apparmor-profile"* and leaves Docker running, and
`ontrak` — whose daemon does support it — ends at
`docker-default (complain)` with its 28 containers untouched.

## Why a check cannot catch this on its own

`estate-check`-style probes report a container that is up and *answering*. A wedged
`gosu` is up and answering nothing, so per-service checks that trust Docker's state are
blind to it by construction. What did catch it, in the end, was asserting the actual
resource — `pg_isready` and `/ready` — which is what `signara`'s healer does and what
this audit reproduced.
