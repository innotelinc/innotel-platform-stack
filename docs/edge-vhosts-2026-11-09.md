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

### Where a pin is checked, and which copy is the authority

Two checks read the declaration, and between them they covered every pair except the one
that broke:

| compared | check |
| --- | --- |
| the declaration ↔ the generated `.conf` | `check-edge-vhost-targets.py` |
| the NPM row ↔ the generated `.conf` | `check-npm-config-drift.py` |
| **the declaration ↔ the NPM row** | `check-edge-vhost-pins.py` |

The row is the authority: NPM writes `proxy_host/NN.conf` **from it**, on its own
schedule. That is why a file-only repair is temporary, and why the third comparison is the
one that fails first — before the regeneration, not after it. `--apply` writes a drifted
pin back through the panel's API, so the row, the file and the running nginx move together
in one write; it then reads the whole row back and fails if any field other than the
target changed, because `advanced_config`, `certificate_id` and `ssl_forced` are load
bearing for a door that serves video.

The same day's audit, read from the database rather than the API so deleted and disabled
rows could not hide, came back clean and is worth writing down as the baseline:

| | proxy hosts | redirections |
| --- | --- | --- |
| rows | 302 | 44 |
| files serving names | 204 | 23 |
| enabled rows with no file | 0 | 0 |
| row/file target mismatch | 0 | 0 |
| served name with no live row | 0 | 0 |
| files whose only row is deleted | 21 | 4 |

The last line needs reading carefully: those 25 names each also belong to a **live** row,
because the estate deleted and recreated them. The file is the live row's, the deleted row
is history, and the drift check's silence about them is correct. Twenty-one other entries
beside the doors are `.bak` copies from earlier hand repairs; they are not included (a
`.conf.bak` is not a `.conf`) and are not doors.

### The names that must stay redirects

Twenty-eight names on 2026-11-09 are redirects rather than doors — the retired hostnames
(`lidarr.monarch`, `radarr.monarch`, the `atheniq.online`/`innotel.site`/`innovative.tel`
TLD family, and the old `www.` forms) and each points at a customer-facing destination, not
at a container. They are pinned in `scripts/edge-redirect-targets.tsv`, and the pin check
compares the same three copies as it does for a door: the redirection row, the generated
`redirection_host/NN.conf`, and the destination each answers with.

The fourth comparison is the one the file exists for. A redirect is how this estate retires
a name; an **enabled proxy host** listing a pinned redirect name means the name is being
served again rather than moved — and every file involved would still look right, because
the redirection file and the proxy file both exist and both match their rows. Nothing else
in the estate asks the proxy-host list about a name it expects to be retired, so the check
reports `PROXIED` for it. A **disabled** proxy host on the name is history (that is how the
name was served before it was redirected) and is not a finding.

Redirection drift is never repaired by `--apply`: a destination is a product decision
(`pbx.it.com`, `atheniq.institute`), so a wrong one is a person's call rather than a
rewrite of a row. It has its own switch, `--apply-redirects`, because once a person has
written the destination into the declaration it *is* that decision. The flag writes the
destination of a drifted row and nothing else; a name that came back as a proxy, a
redirection that was disabled, and a name NPM has no row for all stay in the findings,
because each is a different decision.

The panel's redirection schema is stricter than its proxy one: `enabled`, `hsts_enabled`
and `hsts_subdomains` are rejected as additional properties
(`/app/schema/paths/nginx/redirection-hosts/post.json`), so the update carries
`domain_names`, `forward_scheme`, `forward_domain_name`, `forward_http_code`,
`preserve_path`, `certificate_id`, `ssl_forced`, `block_exploits`, `advanced_config` and
`http2_support` — the fields the redirect was configured with — and no `enabled`, which
the flag never changes anyway.

### Do the destinations still exist?

A redirect to a name that has left DNS, or whose address accepts nothing, sends every
visitor of a retired name into a browser error — while the row, the file and the
declaration all agree. `--check-destinations`, which the timer passes, resolves each
declared destination once and opens 443 on the first address: `REDIR-DEAD` for no address,
`REDIR-NO-ANSWER` for an address that accepts nothing. It is transport only — a 403 from
Cloudflare still means the name takes the visitor somewhere, and what a destination *says*
is a product question.

A host that has lost its view of the internet must not report 28 dead names, so the check
probes a control (`innotel.us`) first: if that does not answer, the facet reports
`destination_unchecked` as a warning and the rest of the run's findings stand, rather than
exiting 2 and pretending the whole check could not run.

### Proved end to end, 2026-11-09

The alert path was exercised rather than assumed. A declaration pinning
`atheniq.innotel.us` at `example.invalid` was published through the real check
(2 findings, `rc=1`), with the timer stopped so the textfile would survive the wait:

| when | what |
| --- | --- |
| t+15 s | `innotel_estate_check_failures{check="edge_vhost_pins"} 2`; alert `pending` |
| t+319 s | alert `firing` — the rule's 5 m hold, elapsed |
| then | Alertmanager holds `EstateCheckFailing`, `check="edge_vhost_pins"`, `severity=critical`, summary *The edge_vhost_pins check is failing* |
| 04:35:26Z | Stalwart logs `delivery.completed … from = "alertmanager@innotel.us", to = ["admin@innotel.us"], size = 3325` |

That delivery is to the human inbox; the earlier `to = ["alertmanager@innotel.us"]`
entries in the same log are the delivery probe's own short-lived alerts, which route to a
separate receiver on purpose. The clean textfile was rewritten afterwards and the timer
restarted from a `trap`, so the probe cannot outlive its own run.

The same pass drifted `bank.innotel.us` to `innotel.site` on the real panel: the check
reported `REDIR-DRIFT` and `REDIR-LAG`, `--apply-redirects` put row 29 and `29.conf` back to
`innotel.us`, and `nginx -t` stayed successful.

### What makes the pin page somebody

A check that only reddens a panel is a check somebody has to be looking at. The pin check
publishes under `edge_vhost_pins`, and `EstateCheckAbsent` in
`extensions/monitoring/prometheus/rules/estate-checks.yml` now names it — one branch per
check is what makes a check that never reports loud, rather than the exact silence of
2026-10-01. `EstateCheckFailing` then fires `critical` on any finding (5 m), so a drifted
pin, a redirect that moved, or a retired name that came back as a door reaches the mail
rather than a dashboard nobody has open. `EstateCheckCouldNotRun` covers the other shape:
exit 2 when the panel or the declaration cannot be read is not a pass. Its branches are
mirrored in the promtool cases beside the rules, so adding a check without a branch fails
CI rather than going quiet.

That last sentence was not true of four checks until this pass: `deploy_door`, `mail_sso`,
`stalwart_oidc_cache` and `telno_legacy_import` published a series with no `absent()`
branch, which is the same silence one check at a time — the textfile stops arriving and no
rule can name what went missing. They have their branches now, and the test that compares
the rule's list against the checks' own `CHECK` constants covers all 23 that publish, so
the next one cannot slip through.

### The `.bak` copies, archived

The 21 `.conf.bak*` files beside the doors were moved out of nginx's data directory to
`/root/edge-vhost-bak-2026-11-09/` with a `MANIFEST.sha256`, rather than deleted: they are
earlier hand repairs, the row and the file are the authority, and a copy nobody serves is
worth keeping only where it cannot be mistaken for a door. The five `.marker-reload-*`
files were left alone — those are NPM's own reload markers, and deleting another process's
markers is not cleanup. Nothing in the estate reads either set; the estate checks and
`nginx -t` were re-run afterwards and stayed green.

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

## 2026-11-11 — the owner settled `tv`

`tv.innotel.us` had moved between Jellyfin (`192.168.1.56:14016`) and the Monarch SSO
gateway in front of Sonarr (`14002`) twice, and the 2026-11-09 repair settled it on
Jellyfin. On 2026-11-11 the owner asked for the other arrangement — `tv.innotel.us` opens
**Sonarr** — so the pin now names `14002`, and a rewrite to Jellyfin is what fails the
check. `--apply` writes the row, the file and the running nginx from the declaration in
one pass, which is the point: a regeneration cannot put the door back the way it was.

Two things follow, both deliberate rather than collateral:

* `tv.innotel.us` is now a **browser** door — Sonarr's UI behind the Authentik gate — which
  is exactly what the old pin said it must never be. No TV client speaks to that name any
  more.
* Jellyfin keeps two doors that serve the watch client, measured while making the change:
  `media.innotel.us` (its gateway, published with `SKIP_AUTH_ROUTES ^/.*$`) and
  `iptv.innotel.us` (the direct `14016` publish). `check-media-doors.py` now watches
  `iptv.innotel.us`, because its assertions are about that publish — `/web/` answers 200
  naming Jellyfin, `/Branding/Splashscreen` answers the 344 514-byte Gizmo splash — and not
  about a particular name. Both doors answered identically to `tv`'s before the change.
