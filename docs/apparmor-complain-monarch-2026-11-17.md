# AppArmor enforcement removed for monarch's Docker — one daemon setting, not per-service overrides

2026-11-17. Supersedes the per-service `security_opt: ["apparmor=unconfined"]`
trade recorded in `media-apparmor-sigsegv-2026-11-17.md`. That fix worked, but it
had to be re-applied to every new .NET/Go service, and the services that never
got it kept dying.

## What is in force now

Inside the incus container `monarch`, the Docker daemon loads a permissive
default profile instead of its built-in one:

```
/etc/apparmor.d/docker-default-monarch     profile docker-default flags=(complain, ...)
/etc/docker/daemon.json                    "apparmor-profile": "/etc/apparmor.d/docker-default-monarch"
```

`complain` audits rules but never enforces them, so every container that daemon
starts is effectively unconfined without a per-service setting. Confirmed with
`grep docker-default /sys/kernel/security/apparmor/profiles` → `docker-default (complain)`,
and a throwaway container reporting `AppArmorProfile=docker-default` while
running unenforced.

The ten `security_opt: ["apparmor=unconfined"]` entries were then removed from
`3-media/monarch/docker-compose.yml` (jellyfin, authentik-ldap, jellyseerr,
sonarr, radarr, lidarr, whisparr, prowlarr, bazarr, sabnzbd) and the containers
recreated. `groups/3-media.yml` was regenerated from the member repos.

Scope: this daemon only. AppArmor stays enabled on i2 and in every other incus
container, and no other host changed.

## Why the default profile cannot work here

`docker-default` grants `signal (send, receive) peer=docker-default`, but inside a
nested incus instance the peer label is namespaced, so the peer match fails. The
runtime's own signal path is then denied:

```
apparmor="DENIED" operation="signal" profile="docker-default"
  namespace="root//incus-monarch_<var-lib-incus>"
  comm="ldap"   requested_mask="send" signal=urg   peer="docker-default//&unconfined"
apparmor="DENIED" operation="signal" profile="docker-default" comm="Sonarr" signal=abrt
```

.NET dies by SIGSEGV with `createdump` unable to run (no exception, no shutdown
line); Go loses preemption signals.

## What it fixed on 2026-11-17

| Symptom | Evidence before | After |
|---|---|---|
| Jellyfin restarted constantly; TV clients could not stay signed in | 477 `Startup complete` in one day, zero shutdown lines, `oom_kill 0`, peak 465 MB of a 3 GiB cap | one start at 21:33, still up 15 min later, API 200 |
| LDAP login denied for `dhunter` although the search found the user in `paid_users` | outpost logged `"No provider found for request"` on the user bind; Jellyfin surfaced `LdapException: Insufficient Access Rights (50)` | wrong password now yields `flow error password: Invalid password` — the provider resolves |

## Reversing it

Delete the `apparmor-profile` key from `/etc/docker/daemon.json`, remove
`/etc/apparmor.d/docker-default-monarch`, restart Docker. Containers pick the
default profile up on their next start, so the services that crash under
`docker-default` must keep a per-service `apparmor=unconfined` until the peer
label namespacing is fixed upstream.

## Not covered by this change

`transmission` is still reported unhealthy by its own healthcheck (exit 1), and
its denials come from a path-attached `transmission-daemon` profile inside
`monarch` rather than from `docker-default`; that is a separate profile to
handle, and this setting does not affect it.

## transmission: the profile that lives on the host

`transmission` stayed `unhealthy` after the daemon-level change, and its daemon
never started: `Error relocating /usr/lib/libstdc++.so.6: RELRO protection
failed`. That was not the image. The same image, a pristine container from it,
Alpine's own packaged daemon and Debian's glibc build all failed identically —
including under `--security-opt apparmor=unconfined` and `--privileged` — because
Docker's profile was never the one denying. The kernel audit log named the real
one:

    apparmor="DENIED" operation="file_mprotect" profile="transmission-daemon"
    name="/var/lib/incus/.../rootfs/usr/lib/libcurl.so.4.8.0" requested_mask="r"

That is the **host's** `/etc/apparmor.d/transmission`, which attaches by path to
`/usr/bin/transmission-daemon` and therefore mediates the binary executed *inside*
the container. Its per-container rendering was already `(complain)`; the host copy
was `(enforce)`. Putting the host copy in complain and reloading with
`apparmor_parser -r` let the daemon start immediately (healthy, RPC answering).

Rule this adds: a service-level `security_opt` cannot see profiles that attach by
path. Any distro profile whose path matches a binary in a container applies
whatever the compose says, so such profiles must be relaxed on the host.
