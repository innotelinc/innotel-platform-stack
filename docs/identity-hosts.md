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
