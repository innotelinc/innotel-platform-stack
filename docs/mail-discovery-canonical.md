# Moving the mail server's discovery onto `auth.innotel.us`

**Status: not applied.** This is the ordered change, written down before it is run, because
every step of it fails the same way — a mail server that starts happily and signs nobody in
— and because the failure it prevents is the one this document describes.

`docs/identity-hosts.md` records the decision (`auth.innotel.us` is canonical) and why this
consumer was left alone: since 2026-11-09 the name is not a configuration string on the mail
container, it is a **trust anchor**. Changing it in this repository first would make the
next refresh fail while every unit still reported `active`.

## What is baked in, and where

Read from the code that installs it (`scripts/stalwart-oidc-discovery-setup.sh`) and the
pieces it installs. All paths are inside the `mail` container.

| Piece | Holds | Named where |
| --- | --- | --- |
| `/etc/stalwart-oidc/tls/server.pem`, `server.key` | a leaf whose SAN is `DNS:auth.cerulean.innotel.us` (the `server.ext` that carried that name is deleted after signing, so the cert is the only record) | setup script, `openssl req -subj "/CN=auth.cerulean.innotel.us"` |
| `/etc/stalwart-oidc/tls/ca.pem`, `ca.key` | the private anchor (`CN=Stalwart OIDC local discovery CA`, 10 years) that signs that leaf — **name-independent**, so it survives a rename | setup script |
| `/etc/stalwart-oidc/ca-bundle.pem` | the system bundle plus that anchor | setup script |
| `SSL_CERT_FILE` in `/etc/stalwart/stalwart.env` | points the server's fetch at the bundle above | setup script |
| `/etc/stalwart-oidc/hosts` | `/etc/hosts` plus `127.0.0.1 auth.cerulean.innotel.us` | setup script |
| `stalwart.service.d/hosts.conf` | `BindReadOnlyPaths=…/hosts:/etc/hosts` — **Stalwart alone** gets that file | setup script |
| iptables NAT `OUTPUT` | `127.0.0.1:443` and one rule per resolved upstream IPv4 → `127.0.0.1:8443`; the proxy's own uid is exempt so its refresh still reaches the provider | `scripts/stalwart-oidc-redirect.sh` |
| `UPSTREAM` / `DOC_PATH` | the name the cache refreshes from, and `/application/o/stalwart-webui/.well-known/openid-configuration` | `scripts/stalwart-oidc-discovery-proxy.py`, `scripts/stalwart-oidc-redirect.sh` |
| `ISSUER` | must equal the document's `issuer` exactly, or the check fails | `scripts/check-stalwart-oidc-cache.py` |
| `IDP` | the watchdog refuses to restart Stalwart unless **this** URL answers 200 | `scripts/stalwart-directory-watchdog.sh` |
| the directory's own issuer | Stalwart's config, which this repository does not own: the build has no separate discovery-URL setting, it appends the well-known path to the issuer and rejects a document naming another one | Stalwart, not here |

That last row is the one to **read** rather than assume before starting:

```bash
incus exec mail -- sh -c "grep -rno 'https://auth\\.[a-z.]*innotel\\.us/application/o/stalwart-webui/' /opt/stalwart/etc 2>/dev/null | head"
```

## Why one piece, in this order

Each step below depends on the one before it, and the failure of any half-done subset is
identical: **Stalwart's start-up fetch fails, the failure is cached for the life of the
process, and every bearer token answers 500** while anonymous requests answer 200 — the
2026-11-06 fault (`docs/mail-sso-directory-2026-11-06.md`), which is why the local copy
exists at all.

* Cert before hosts: a hosts line pointing the new name at loopback, with a leaf that does
  not carry it, is a TLS name mismatch on every fetch.
* Hosts and proxy before the issuer: Stalwart appends the well-known path to whichever
  issuer it is configured with, so the cache has to answer **that** name first. The proxy
  forwards every other request upstream unchanged, which is what makes the reverse order
  safe: during the window both names work.
* Issuer last, because it is the only step whose effect lands on the next `stalwart`
  restart, and it is the one a person can undo in one line.

The watchdog does not rescue a partial switch: it restarts only when `IDP` — still the old
name — answers 200, so a broken new name produces a restart every 15 minutes instead of a
page. Do not rely on it.

## The change

Run as root in the `mail` container. Steps 1–2 are read-only.

**1. Preflight — record the state you are about to change.**

```bash
systemctl is-active stalwart stalwart-oidc-proxy stalwart-oidc-redirect
stalwart-oidc-redirect.sh status                      # every DNAT rule, current
jq -r .issuer /etc/stalwart-oidc/cache/discovery.json # what is served now
cp -a /etc/stalwart-oidc /root/stalwart-oidc.pre-canonical   # certs, hosts, copy (0600 dir)
tail -n 50 <(journalctl -u stalwart --no-pager) | grep -iE 'directory|discovery|issuer'
curl -s -o /dev/null -w 'provider %{http_code}\n' \
  https://auth.innotel.us/application/o/stalwart-webui/.well-known/openid-configuration
```

The last one must answer 200 **from inside the container**: the proxy's refresh will read
exactly that URL, and it is exempt from the DNAT, so this proves the refresh path.

**2. The leaf, for both names.** Do not re-run the setup script — it generates only when
`ca.pem` is missing, and it hardcodes one SAN. Mint a leaf that carries **both** names, so
the old one keeps verifying while the switch is in progress:

```bash
cd /etc/stalwart-oidc/tls
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=auth.innotel.us"
printf 'subjectAltName=DNS:auth.innotel.us,DNS:auth.cerulean.innotel.us\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n' > server.ext
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.pem -days 3650 -extfile server.ext
rm -f server.csr server.ext
chown root:oidcproxy server.pem server.key && chmod 0644 server.pem && chmod 0640 server.key
openssl x509 -in server.pem -noout -ext subjectAltName    # both names, or stop here
```

The anchor does not change, so `ca-bundle.pem` and `SSL_CERT_FILE` stay as they are.

**3. Let Stalwart resolve the new name too.** Add the line, keep the old one:

```bash
printf '127.0.0.1 auth.innotel.us\n' >> /etc/stalwart-oidc/hosts
systemctl restart stalwart          # re-reads the bound hosts file
```

**4. Teach the proxy and the DNAT the new name.** Edit the **installed** copies — the repo
is updated in step 8, after the host works:

```bash
sed -i 's/^UPSTREAM=auth\.cerulean\.innotel\.us/UPSTREAM=auth.innotel.us/' \
  /usr/local/bin/stalwart-oidc-discovery-proxy.py /usr/local/bin/stalwart-oidc-redirect.sh
/usr/local/bin/stalwart-oidc-redirect.sh install   # add is `-C || -A`: old rules stay
systemctl restart stalwart-oidc-proxy
stalwart-oidc-redirect.sh status                   # rules for both names now
```

**5. Prove the new name is served before anything depends on it.**

```bash
curl -s --resolve auth.innotel.us:443:127.0.0.1 \
  https://auth.innotel.us/application/o/stalwart-webui/.well-known/openid-configuration \
  | jq -r .issuer
# must print https://auth.innotel.us/application/o/stalwart-webui/
```

`--resolve` deliberately bypasses the DNAT so this tests the cert and the proxy, not the
redirect. If it fails here, nothing has been changed that matters: stop, steps 6–7 undo
easily.

**6. Move the directory's issuer** (the setting from the table above) to
`https://auth.innotel.us/application/o/stalwart-webui/`, then:

```bash
systemctl restart stalwart
sleep 10
curl -s -o /dev/null -w 'bearer %{http_code} (401 is healthy)\n' \
  -H 'Authorization: Bearer canonical-switch-probe' http://127.0.0.1:8080/jmap/session
journalctl -u stalwart --since '-2min' --no-pager | grep -ciE 'discovery fetch failed|failed to initialize'
```

`401` and a `0` are the pass. `500` means the cached-failure state, and the answer is the
rollback below, not a retry.

**7. Verify from the estate, not from inside the container.**

```bash
./scripts/check-mail-sso.py --webmail https://mail.innotel.us --base http://192.168.1.15:8080
./scripts/check-stalwart-oidc-cache.py --host root@192.168.1.52 --container mail
```

The second one fails `issuer_mismatch` until its `ISSUER` constant is updated in step 8 —
that failure is the check working, not a second fault.

**8. Now the repository, as one commit** (they are one fact): `ISSUER` in
`check-stalwart-oidc-cache.py`, `IDP` in `stalwart-directory-watchdog.sh`, `UPSTREAM` in
both the proxy and the redirect script, and in `stalwart-oidc-discovery-setup.sh` the SAN,
the `/etc/stalwart-oidc/hosts` line, the seed URL for the first copy, and its comments.
Until this lands, a container rebuilt from these scripts would recreate the old name.

**9. Retire the old name**, once a full day of `check-stalwart-oidc-cache` notes show fresh
refreshes: remove the `auth.cerulean.innotel.us` line from `/etc/stalwart-oidc/hosts`, delete
its DNAT rules explicitly (`stalwart-oidc-redirect.sh remove` now scans the *new* upstream's
addresses, so it will not find them), and re-issue the leaf with the single canonical SAN.

## Rollback

`stalwart-oidc-discovery-setup.sh rollback` is **not** this rollback — it restores the
pre-2026-11-09 world (disables both units, restores `stalwart.env.pre-oidc-cache`) and was
written for a failed first install.

To go back: restore `/root/stalwart-oidc.pre-canonical` over `/etc/stalwart-oidc`, put the
directory's issuer back, `systemctl restart stalwart`, then `stalwart-oidc-redirect.sh
status` and delete any new-name rules by hand. The bearer probe must be `401` again before
anything else is touched.

## What watches this after it is done

| Check | Would catch |
| --- | --- |
| `check-stalwart-oidc-cache.py` | a wrong or foreign document, `proxy_inactive`, `redirect_inactive`, an ageing copy, and either user losing read access |
| `check-mail-sso.py` | a person who cannot sign in, and the cached-directory 500 (`directory_unavailable`) |
| `check-orion-signin.py` | the same brand question on the other door, so this change cannot leave the estate half-canonical by accident |

None of them reads the directory's issuer directly — Stalwart does not expose it — which is
why step 6's bearer probe is the evidence for that step and not a check.
