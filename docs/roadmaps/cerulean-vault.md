# cerulean-vault — roadmap

- **Job:** SecretOps
- **Classification:** platform service
- **Runs on:** server 1 (Cerulean)
- **Consumes:** Authentik
- **Target version:** 2.0.0

## What it owns

HashiCorp Vault KV v2: the only place a credential is stored at rest. `.env` carries `vault://` references, never values.

## 2.0 baseline — in force

- [x] **One job, written down.** This page names the single responsibility and the boundary beside it.
- [x] **Consumption, not embedding.** Authentik are consumed as platform services; no second copy is shipped here.
- [x] **Recorded placement.** server 1 (Cerulean) is the recorded group placement, so a deployment is a lookup in this repository.
- [x] **One release path.** The product repo cuts releases with `scripts/release-repo.sh`, which tags, builds and attaches artifacts.
- [x] **Version agreement.** `VERSION`, `docs/versions.md` and this roadmap all say 2.0.0.

## Next on this line

- [ ] Finish the remaining `infisical://` sweep and keep rotation evidence per mount.

## Wiring

- Identity: Authentik (`auth.<platform>.innotel.us` is an edge alias, never a second IdP).
- Secrets: Cerulean Vault; `.env` carries `vault://` references and is derived.
- Edge: NPM Edge terminates TLS with the zone wildcard, issued by Cerulean over DNS-01.
- Billing: Magnate owns plans and entitlements; this product never prices a seat locally.

## The OIDC origin drift (2026-11-20)

Signing in to the UI through Authentik failed with

    Missing auth_url. Please check that allowed_redirect_uris for the role include this mount path.

The message points at the role, and it is right, but not for the reason it reads. The role
`auth/oidc/role/operator` allowed
`https://secrets.cerulean.innotel.us/ui/vault/auth/oidc/oidc/callback` and
`http://localhost:8250/oidc/callback`; the Authentik provider *Vault* allowed the same public
URI; and discovery of `https://auth.cerulean.innotel.us/application/o/vault/` works from inside
the container (200, correct issuer). What the browser actually sent was the **http** form of
both the public name and the estate address, which Vault logged verbatim:

    [WARN] auth.oidc: unauthorized redirect_uri: redirect_uri=http://secrets.cerulean.innotel.us/ui/vault/auth/oidc/oidc/callback
    [WARN] auth.oidc: unauthorized redirect_uri: redirect_uri=http://192.168.1.71:8200/ui/vault/auth/oidc/oidc/callback

The estate proxy answers http as well as https for that name, so a login started at the http
origin is an ordinary thing to do — and Vault compares the origin verbatim, refuses to build an
authorize URL, and the UI then reports the *missing auth_url* rather than the mismatch.

Fixed in both places, because either one alone still refuses the flow: the role's
`allowed_redirect_uris` and the Authentik provider's redirect URIs now carry the https URI, its
http form, the http estate address and the CLI callback. Verified by reading both back.

The bootstrap would have re-created the narrow list: `scripts/vault-entrypoint.sh` wrote the
role with `$OIDC_REDIRECT` plus the CLI callback only. It now derives the http form from
`$OIDC_REDIRECT`, so the two forms cannot drift apart when the public name changes
(`scripts/vault-entrypoint.sh.bak-oidchttp-20261120` is the previous copy; that script lives in
the deployment checkout, not in this repo).

Plainly: the http origin is what made this fail, and it should not be an entrance. A vault
session over plain http across the LAN carries its cookie in clear, and this estate's rule
elsewhere is that a name answering over http is a bypass. The URI is allowed because it is what
people reach today; closing http for `secrets.cerulean.innotel.us` is the real fix, and it is a
proxy change rather than a Vault one.
