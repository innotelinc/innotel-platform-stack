# Edge vhosts — the check was grading a stale copy, 2026-11-09

**Report:** four estate checks red after the webmail→oasis merge:
`edge-vhost-targets`, `media-doors`, `address-latency`, `stuck-processes`.
**What it was:** two independent faults that hid each other. The checks were
reading a config tree the edge does not serve, and several vhosts pointed at
names that only resolve inside Docker's network or at hosts with nothing
listening.
**Fix applied:** the edge check now reads the live mount; five vhosts corrected,
one created, two parked.
**Now watched:** `scripts/check-edge-vhost-targets.py` and
`scripts/check-media-doors.py`, run by `estate-checks.service`.

## The stale tree

NPM runs as the container `cerulean-npm` and mounts
`/usr/src/proxy/data -> /data`. On the proxy host, `/data/nginx` is a **different,
older copy** of the same layout — 183 files against the live tree's 204, and it
does not contain the vhosts edited through the UI since the split.

`check-edge-vhost-targets.py` defaulted to `/data/nginx`, so it graded the
leftover. A vhost edited in the panel read as current while the edge actually
served something else — `tv.innotel.us` was still dialling a Sonarr OIDC gate
because only the stale copy had ever been checked. The unit now passes
`--config-dir /usr/src/proxy/data/nginx`; an operator run in that tree reports
`89 finding(s): 0 drift, 0 dead target(s)`.

That change immediately surfaced a dead target the stale copy had been hiding —
see the apex row below. It is worth remembering that the check can only be as
honest as the tree it points at: when a vhost "passes" but the site is wrong,
check the mount first.

## What changed

| Vhost | Before | After | Why |
| --- | --- | --- | --- |
| `blueprint.innotel.us` | `192.168.1.52:8787` | `192.168.1.76:8787` | nothing listening on the old address; the app answers on i4 |
| `sage.innotel.tech` | **no vhost** | `443` → app, `*.innotel.tech` cert | the 200 was NPM's default server, not Sage — port 443 had no host for the name |
| `innotel.us` (apex) | `innotel-site:80` | `192.168.1.71:8086` | `innotel-site` resolves only inside Docker; the check and the host cannot use it |
| `tv.innotel.us` | `sonarr-sso:14002` | `192.168.1.56:14016` | TV clients cannot complete a browser flow, so this door must be Jellyfin's own accounts, not an Authentik gate — now pinned in `scripts/edge-vhost-targets.tsv` |
| `i3.innotel.us` | live upstream | `127.0.0.1:9` (parked) | i3 is retired in `docs/container-placement.md`; parking is the estate's way of keeping a name without pretending it serves |
| `pm4.innotel.us` | live upstream | `127.0.0.1:9` (parked) | no host answers on it |`check-edge-vhost-targets.py` treats `port == 9` as parked, so the two retired names stay declared without counting as dead targets.

`tv.innotel.us` is in `scripts/edge-vhost-targets.tsv` too, as of the same day. Its
target had gone wrong before, and the first repair changed only the vhost file while
the `proxy_host` row still said `14002` — NPM regenerated the file from the row and
the SSO gate was back in front of a door that cannot complete a browser flow. Nothing
in the estate noticed until `check-media-doors.py` failed. With the target pinned, the
rewrite fails at the edge instead of at somebody's TV.

## How the edits were made

NPM's database and the container's `/data` are authoritative; the host's
`/data/nginx` is not. Each change was written to the live
`/usr/src/proxy/data/nginx/proxy_host/<id>.conf` **and** the matching `proxy_host`
row, then `nginx -t` and `docker exec cerulean-npm nginx -s reload`. Editing only
the file leaves the panel ready to regenerate it; editing only the row leaves the
edge serving the old config until the next reload.

## What is still open

- Sage's vhost is a plain proxy to the app. If it grows its own auth, it needs
  the same decision `tv` got: a browser flow is fine there, a TV client is not.

## Afterwards, the media door closed clean

Once `tv.innotel.us` reached Jellyfin again, `check-media-doors.py` reported
**0 failures, 0 warnings** — the branding warning it had been raising was never
the splash. `https://tv.innotel.us/Branding/Splashscreen` had been answering
`200 image/png` all along (344 514 bytes); the check could not see it because
the name dialled the Sonarr gate, so the splash URL went to the wrong product.
A warning about an asset was really a warning about the vhost: worth remembering
when a "missing" thing lives behind a door that is pointing somewhere else.
