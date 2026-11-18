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

`daemon.json` → `"apparmor-profile": "<path>"` selects a profile **by the name inside the
file**, not by the path. So the replacement file must declare `profile docker-default`,
and it is loaded in **complain**: the rules are audited rather than enforced, which is
equal to allowing them while a denial still reaches the kernel log.

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
| i1 `.51` | `atheniq` | 21 | unset | `docker-default (enforce)` | 0 |
| i1 `.51` | `genesis` | 1 | unset | `docker-default (enforce)` | 0 |
| i1 `.51` | `mail` | 1 | unset | none | 0 |
| i1 `.51` | `ontrak` | 28 | unset | `docker-default (enforce)` | 0 |
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

**Two guests are fixed** (`monarch`, `signara`). **Twelve Docker daemons still run the
stock profile in enforce** — `atheniq` (21 containers), `ontrak` (28), `proxy` (24),
`onyx` (14), `vault` (8), `rizzaura` (5), `atlas` (4) and the single-container guests.

None of them had logged a signal denial at the time of the audit, so none is wedged. The
exposure is real but **latent**: the denial only bites a workload that signals across the
namespaced boundary — the postgres entrypoint's checkpoint/`urg` path, or a Go privilege
drop. `ontrak` and `atheniq` both run Postgres images whose entrypoint drops with
`gosu`, which is exactly the composition that failed on `signara`, so they are the two
worth applying this to first.

Applying it is one command per guest and needs one container recreate; it is not done
here because it restarts each guest's Docker daemon and that is a change to running
production, not to this repository.

## Why a check cannot catch this on its own

`estate-check`-style probes report a container that is up and *answering*. A wedged
`gosu` is up and answering nothing, so per-service checks that trust Docker's state are
blind to it by construction. What did catch it, in the end, was asserting the actual
resource — `pg_isready` and `/ready` — which is what `signara`'s healer does and what
this audit reproduced.
