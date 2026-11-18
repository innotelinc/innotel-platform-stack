# The identity host is `auth.innotel.us`

Decided 2026-11-14. The estate runs **one** authentik that answers to two names —
`auth.innotel.us` and `auth.cerulean.innotel.us` — and `auth.innotel.us` is the canonical
one: the host new consumers name, and the one the estate's own doors are on.

## Why this had to be decided at all

Neither name is an alias for the other. Each reports **itself** in the fields that are
compared during a sign-in, so from a relying party's point of view they are two different
providers sharing a database:

```bash
for host in auth.innotel.us auth.cerulean.innotel.us; do
  printf '%s → ' "$host"
  curl -s "https://$host/application/o/orion/.well-known/openid-configuration" | jq -r .issuer
done
# auth.innotel.us          → https://auth.innotel.us/application/o/orion/
# auth.cerulean.innotel.us → https://auth.cerulean.innotel.us/application/o/orion/
```

The client ids match because the instance is the same one — the `Orion NAS` provider's
`client_id` in `authentik-backup-20261102.json` is the id the live authorize redirect asks
for. The **issuers** do not match, and `iss` is verified against the configured issuer even
with `--skip-oidc-discovery` (`docs/sign-in-posture.md`). So a relying party configured for
one name and handed tokens issued under the other fails at the callback with a generic 500,
after the person has already signed in correctly. That is what the Orion NAS door did on
2026-11-14, and why `scripts/check-orion-signin.py` now compares the three statements.

## What "canonical" means here, and what it does not

- **It does not make the other name wrong.** A door consistently on
  `auth.cerulean.innotel.us` authenticates people perfectly; the check reports it as a
  *warning* (`brand_not_canonical`), not a failure. Consistency is the correctness
  requirement; the brand is the estate's direction.
- **It does mean new things name `auth.innotel.us`.** New projects'
  `OIDC_ISSUER_URL` (`scripts/conform-project.sh`), the mailbox mirror's default API
  (`scripts/sync-mail-identities.py`) and `.env.example` templates follow it.
- **It does mean the two sides of one door must be the same name.** The proxy's
  `OAUTH2_PROXY_OIDC_ISSUER_URL`, the app's `ORION_OIDC_ISSUER`, and the provider the
  client was created under are one fact. `orion-work` derives the proxy's value from the API
  it created the provider against rather than typing it.

## Consumers, and where each one stands

| Consumer | Names | Why |
| --- | --- | --- |
| `orion-work` proxy + app | `auth.innotel.us` | the door whose callback failed; issuer now written from the provisioning API |
| `scripts/conform-project.sh` | `auth.innotel.us` | template for new projects — this is where the policy is enforced |
| `scripts/sync-mail-identities.py` | `auth.innotel.us` | default `AUTHENTIK_API_URL`; both names serve the same instance, measured by the shared client id |
| `scripts/check-orion-signin.py` | both, canonical warned | asserts the *agreement*, not the brand |
| the classroom gate (`classroom-sso`, `deploy/compose/atheniq-sso.yml` in `atheniq`) | `auth.innotel.us` | moved 2026-11-14 and the check's note cleared the same day. The gate was never the problem: NPM held two hosts naming `auth.innotel.us` and served the cerulean one (see the section at the end of this file) |
| the generation gate beside it (`generation-sso`, same file) | `auth.cerulean.innotel.us` | **not** part of that change — a separate door (`ai.lms.innotel.us`), left as it was |
| Stalwart's directory discovery (`scripts/stalwart-oidc-discovery-proxy.py`, `stalwart-directory-watchdog.sh`, `stalwart-oidc-discovery-setup.sh`) | `auth.cerulean.innotel.us` | **captured, not chosen** — see below |
| `docs/sso-cleanup-2026-11-02.md`, `docs/mail-sso-directory-2026-11-06.md` | `auth.cerulean.innotel.us` | incident records: they say what was measured then, and are not rewritten |
| `scripts/check-media-doors.py`, `check-sso-redirects.py` | the brand they were measured against | assertions about a regression, not configuration |

## The one that is not a rename

Since 2026-11-09 the mail server does not fetch discovery from the identity provider at
all. It reads a copy inside its own container that refreshes every fifteen minutes and keeps
its last good bytes when a refresh fails (`docs/mail-sso-directory-2026-11-06.md`), and that
copy is served under the name `auth.cerulean.innotel.us` by a local proxy with a **trust
anchor generated on that container** (`stalwart-oidc-discovery-setup.sh` writes the cert, the
`hosts` entry and the `server.ext` SAN). The name is therefore baked into three things that
must change together: the container's trust store, its `/etc/hosts` line, and the directory's
`issuer` setting. Changing it in this repository alone would make the next refresh fail —
or worse, leave the old anchor trusting a name the config no longer uses.

So it stays until that change is done as one piece on the mail container, in that order:
new cert → new `hosts` entry → directory issuer → verify with `check-mail-sso.py` and
`check-stalwart-oidc-cache.py` → only then the scripts here. Doing it in this repo first is
the failure mode this document exists to prevent. The ordered runbook — every path, the
proof for each step, the traps, and the rollback — is
[mail-discovery-canonical.md](mail-discovery-canonical.md).

## How it is checked

`scripts/check-orion-signin.py`, run by `estate-checks.service`, reads the proxy's env file
on the edge, the configured issuer's discovery document, and the redirect the door actually
emits — three statements of one fact, and it names which two disagree. `issuer_mismatch` is
the 500-at-the-callback class; `redirect_wrong_brand` is the drift as soon as it appears.

The edge side of the Orion change — what to re-run, in what order, and why the redirect the
running process emits is what proves it rather than the env file — is
[orion-issuer-apply.md](orion-issuer-apply.md).

## Two edge faults found while clearing the classroom's note (2026-11-14)

`check-atheniq-doors.py` kept reporting `issuer_alias` for `class.atheniq.courses` long
after the gate itself was moved. The gate was not the cause. NPM held **two** proxy hosts
naming `auth.innotel.us` — 35 (the cerulean record) and 37 — and nginx says so itself on
every config test: `conflicting server name "auth.innotel.us" on 0.0.0.0:80, ignored`.
The first one loaded serves the name, so the door answered with the cerulean issuer while
`192.168.1.59:3001` answered `auth.innotel.us` to a direct request. Dropping the name from
host 35 (it keeps `auth.cerulean.innotel.us`) made the public door answer
`https://auth.innotel.us/application/o/authorize/…`, and the check went to
`0 failure(s), 0 warning(s), 1 note(s)`; the remaining note is `alias_moved`, which is the
documented end state for a retired name.

The same duplicate explains the mail mirror's `exit-code 2`, which had been retrying
quietly. From inside the `mail` container a handshake with `auth.innotel.us` returned
`21 (unable to verify the first certificate)` while the same handshake from the edge
returned `0`; the name was being served by the cerulean host record. Two changes closed it:

* the mirror keeps HTTPS, and the container pins the name to the LAN edge:
  `/etc/hosts` gained `192.168.1.71 auth.innotel.us` (backup `/etc/hosts.bak-*`), and
  `AUTHENTIK_API_URL` stayed `https://auth.innotel.us`. This is not a workaround for the
  chain — from inside that container the *public* address hairpins to a different edge and
  answers `CN = auth.cerulean.innotel.us`, which is a name mismatch, while the LAN edge
  answers `CN = innotel.us` with `Verify return code: 0`. Going through the edge the
  estate actually runs is both correct and verifiable; it is the public hairpin that is
  wrong, and it is a router question, not a certificate one. First run afterwards:
  `Result=success ExecMainStatus=0`.
* certificate 157 (`ai`, `catalog`, `learn` — no `classroom.`) was issued and hosts 291 and
  289 moved onto it; 128 was deleted once no host referenced it. The served SANs for all
  three names now list exactly those three, which finishes the retirement's fourth step.

Worth knowing before the next identity change: a test that passes on the edge can fail on a
container because *which* host record wins is decided by load order, not by the API's
listing. `nginx -t` names the conflict; the API does not.

## The mail container's own names, pinned to the edge (2026-11-16)

Three units in the `mail` container (`mailjet-inbound-check`, `resend-inbound-check`,
`oasis-mail-admin`) were failing their own health checks with
`SSL: CERTIFICATE_VERIFY_FAILED … Hostname mismatch` on `mailjet.innotel.us` and
`mail.innotel.us`. The certificate was never the problem — asked on the edge,
`mail.innotel.us` answers `CN=mail.innotel.us` and `mailjet.innotel.us` answers the
`*.innotel.us` wildcard, both verifying. What the container received was
`CN = auth.cerulean.innotel.us`, and the reason is the discovery cache's outbound rule:
`stalwart-oidc-redirect.sh` installs one DNAT per *resolved upstream IPv4*, and
`auth.cerulean.innotel.us` resolves to `73.68.203.71` — the estate's **single WAN
address**, shared by every public name. So
`-A OUTPUT -d 73.68.203.71 --dport 443 -j DNAT --to-destination 127.0.0.1:8443`
captured *every* HTTPS request the container made to an estate name and handed it to the
proxy, which answers with its own leaf. No certificate change can satisfy a client whose
request was routed to a different service.

The remedy is the one already used for `auth.innotel.us`: pin the names to the LAN edge in
the container's `/etc/hosts` (`/etc/hosts.bak-<epoch>` kept), so the lookup never reaches
the public address the DNAT matches.

These pins are **provisioning**, not a hand edit. `scripts/stalwart-oidc-discovery-setup.sh`
carries the list (`PINNED`) and applies it in `install`; `… pins` applies just them,
idempotently, to a container that already exists. That is what makes them survive a
rebuild — the container has no cloud-init at all, so the setup script is the only build
path, and the hand-applied version was one `incus delete` away from taking mail, the
inbound checks and the notifier with it.

| Name | Edge host → upstream | SAN it is served with |
| --- | --- | --- |
| `mail.innotel.us` | 95 → `192.168.1.15:3000` | `mail.innotel.us` |
| `mailjet.innotel.us` | 282 → `192.168.1.15:8100` | `*.innotel.us` |
| `admin.mail.innotel.us` | 221 → `192.168.1.15:8080` | `admin.mail.innotel.us` |
| `admin.oasis.innotel.us` | 223 → `192.168.1.15:8091` | `admin.oasis.innotel.us` |
| `alerts.innotel.us` | 202 → `192.168.1.71:2586` | `*.innotel.us` |

The last row is the notifier's own name: the same pin was why the mail server's checks
could deliver mail but not push a notification (`ntfy: failed: … certificate is not valid
for 'alerts.innotel.us'`). Any name a container has to reach by HTTPS belongs in this list
— the DNAT matches the **address**, not the name, so every unlisted name is redirected to
the discovery cache. The table records the names whose edge row was checked by hand; the
authoritative list is `PINNED` in the setup script, which also pins `resend.innotel.us`
and `auth.cerulean.innotel.us`.

Stalwart is deliberately untouched: it reads its own `/etc/stalwart-oidc/hosts` through
`BindReadOnlyPaths`, and the loopback pin in that file is what the discovery cache needs.
The general lesson matches the one above — *which* host answers a name is decided by the
path the request takes, so a container that must verify a public name needs the address it
can reach, not the address the world sees.

## Retired with the guest, and the last duplicate (2026-11-14)

`192.168.1.93` was retired, so `deploy.innotel.us` and `fax-staging.lab.innotel.us` were
deleted from the edge (backups `/root/npm-host-321.json` and `/root/npm-host-281.json`)
rather than left answering 502. `check-deploy-door.py` now carries empty door and tunnel
maps and exits 0 with a `retired` note, because a series that disappears reads exactly like
a check that stopped running.

The last `conflicting server name "auth.innotel.us" on 0.0.0.0:80` came from a
hand-written block in `/data/nginx/custom/http.conf` that duplicated the redirect NPM's own
`:80` block already generates. Removing it leaves `nginx -t` reporting no conflicts at all,
and `http://auth.innotel.us/` still answers 301 to HTTPS.

## deploy.innotel.us is back, on the panel's own host (2026-11-15)

The name had been left dead by the guest retirement, not by anything wrong with Coolify.
It still resolved to the public edge (`73.68.203.71`), but NPM held no vhost for it, so
443 answered `TLS alert, unrecognized name` — the symptom this page already records for a
name with no loaded `server_name`.

Measured before the repair: none of the 215 proxy hosts named the door, no door rode a
tunnel any more, and the only estate listeners on `:8090` were telno's `christ-telno` on
`192.168.1.113` and a stale docker-proxy on `i2`.

**Corrected 2026-11-18: `.113` was never a VM.** This page said "host `dev`, a VM of its
own rather than an Incus guest", and that sentence sent a later investigation looking for a
hypervisor that does not exist. `192.168.1.113` is the **`workloads` container on `i1`**
(`incus list i1`), and what answered `:8000` there was not Coolify at all but
`coolify-webhook-forward.service` — a `node` forwarder whose whole job was to relay
webhook deliveries to the panel, then on `i2`'s **host Docker** (`192.168.1.52:8000`, with
`coolify-sentinel`, `coolify-db` and `coolify-redis`, `coollabsio/coolify:4.7.5`).

That shape was the reason three address-keyed Authentik callbacks existed for one provider:
the panel was reached by address from two different addresses, so both were registered, and
the name (`https://deploy.innotel.us`, which is what `instance_settings.fqdn` actually held)
was registered beside them. On 2026-11-18 the panel moved to its own container (`coolify`,
`.85`, on `i2`) and the two name-keyed callbacks were left as the only ones — see
`docs/container-placement.md`, §*Changes applied 2026-11-18 (evening)*.

Two faults stacked, which is why pointing a proxy host at it was not enough on its own:

* the panel container was **Up but unhealthy**: inside it only `:9000` (php-fpm) was
  listening, the web server answering `:8080` was not, so its own health check
  (`curl 127.0.0.1:8080`) was refused and the host's published `:8000` reset every
  connect. `docker restart coolify` brought NGINX + PHP-FPM back (`✅ NGINX + PHP-FPM is
  running correctly.`) and the panel began answering;
* the edge had no vhost, so the name could not have served anything even with the panel up.

Proxy host **342** now serves `deploy.innotel.us` → `http://192.168.1.85:8000` with
websockets, HTTP/2 and forced SSL on certificate **153** (which already named the door and
was unused; it was created while the tunnel was still the path). Verified through the
edge, from `i2` and from inside the `proxy` container: `/` → `302
https://deploy.innotel.us/login` → **200**, the login page carries the `coolify` marker,
TLS `CN=deploy.innotel.us` valid to **2027-02-01**, and `:80` → 301.

`check-deploy-door.py` was restored with the door — it had been reporting the retirement
as a note — and `TUNNELS` stays empty because this door rides no tunnel. The lesson worth
keeping: the retirement note was accurate the day it was written and became wrong the
moment the panel was reachable again, and a check that emits a note instead of probing
cannot notice that by itself.

## The panel lives on `i2`; the `.113:8000` shim is gone (2026-11-18)

Coolify no longer runs on `.113`. It runs in an Incus container named `coolify` on `i2`
(`192.168.1.52`), address **`192.168.1.85`**, publishing `0.0.0.0:8000->8080/tcp` for
`coollabsio/coolify:4.7.5` beside `coolify-db` and `coolify-redis`. `.113` is a different
machine — the Incus container `workloads` (hostname `dev`) on `i1`.

What made them look identical: `.113:8000` was a 992-byte TCP shim,
`coolify-webhook-forward.mjs`, piping every connection to `.85:8000` so GitHub webhook
deliveries kept working at the address the repository already used. Next to it,
`coolify-npm-tunnel.service` carried `-R 172.18.0.1:8090:127.0.0.1:8000` to the edge for the
old `deploy.innotel.us` path.

Both were removed on 2026-11-18: the shim because proxy host **342** already dials
`http://192.168.1.85:8000` directly, and the tunnel because it had been failing with
`203/EXEC` (`/usr/bin/autossh` no longer exists) and restarting every 2 seconds, so the
`8090` path had been dead already. Its other forward, `8091`, served only host **281**,
which is `is_deleted=1`. Units and script are archived at `/root/removed-20261118/` on
`workloads`.

After the removal: `.113:8000` refuses, `.85:8000/login` is `200`, and through the edge
`https://deploy.innotel.us/` → `302 /login` → `200` with the `coolify` marker. If a webhook
sender still posts to `.113:8000`, it now fails loudly instead of silently crossing hosts;
the panel's own URL is the `deploy.innotel.us` door.
