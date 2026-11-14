# Applying the Orion issuer change on the edge

The code side is in `orion-work` (`deploy/sso/provision-authentik.py`), committed as
`0f2180c`: the issuer defaults to the canonical host and is **verified against the
provider's discovery document** before it is written. This is what to run on the edge, in
what order, and what proves each step.

**Status: not applied.** Nothing here has been run against the live host.

## What the door is

| Piece | Where |
| --- | --- |
| the proxy container | `orion-sso`, from `deploy/sso/docker-compose.yml`, `network_mode: host`-less: publishes `192.168.1.71:14016:4180` |
| its configuration | `/root/orion-sso/orion-sso.env` — `env_file: orion-sso.env`, so compose must be run from that directory |
| the provider | `Orion NAS` in Authentik (pk 53), client `e_OcH…`, grants `authorization_code, refresh_token` |
| what NPM forwards | `nas.innotel.us` → the port above; the public callback is `https://nas.innotel.us/oauth2/callback` |
| the app's own copy of the fact | `ORION_OIDC_ISSUER` in the app's `.env` on `192.168.1.89` |

The issuer is one string in three places and it must be the same string in all three: the
env file, the provider it was created under, and the app. `provision-authentik.py` derives
the first from the API it talks to, so they cannot drift by typing.

## The order, and what each step proves

**1. Measure before writing anything.** The script refuses a mismatch, but knowing the
answer first is what tells you which brand the provider is actually configured under:

```bash
for host in auth.innotel.us auth.cerulean.innotel.us; do
  printf '%-32s ' "$host"
  curl -s "https://$host/application/o/orion/.well-known/openid-configuration" | jq -r .issuer
done
```

Expected today (measured 2026-11-14): the first reports
`https://auth.innotel.us/application/o/orion/`, the second reports itself. `issuer_mode` is
`per_provider`, which is why the slug is part of the string and why the trailing slash
matters.

**2. Keep the old file. It holds the client secret, so it stays on the edge.**

```bash
cp -a /root/orion-sso/orion-sso.env /root/orion-sso/orion-sso.env.pre-issuer-$(date +%Y%m%dT%H%M%SZ)
```

**3. Re-run the provisioner, from the edge, on the host that holds the Authentik token.**

```bash
cd /root/orion-sso
python3 /root/orion-work/deploy/sso/provision-authentik.py
```

It reads `AUTHENTIK_API_URL` and `AUTHENTIK_BOOTSTRAP_TOKEN` from
`/usr/src/projects/complete/1-primary/cerulean/.env` (override the output with
`ORION_SSO_ENV`, the issuer with `ORION_OIDC_ISSUER`). Idempotent: the provider is found by
name, the cookie secret already in use is preserved so nobody is signed out, and the only
things rewritten are the four lines in the env file. It prints:

```
wrote /root/orion-sso/orion-sso.env with the client id, secret, cookie secret and the verified issuer
issuer: https://auth.innotel.us/application/o/orion/
```

A refusal here — `… reports issuer '…', not '…'` — means the provider answers under a
different name than the one asked for. Use what it reports; do not force it. Forcing that
string is exactly the 500 being repaired.

**4. Read the file, do not trust the print.**

```bash
stat -c '%a %U:%G' /root/orion-sso/orion-sso.env     # 600 root:root
grep -E '^OAUTH2_PROXY_OIDC_(ISSUER_URL|CLIENT_ID)=' /root/orion-sso/orion-sso.env
```

The issuer must end in `/orion/` — the string the discovery document returns, character for
character.

**5. Recreate the container. A restart keeps the old value**, because a container's
environment is fixed when it is created:

```bash
cd /root/orion-sso
docker compose -f /root/orion-work/deploy/sso/docker-compose.yml up -d --force-recreate orion-sso
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' orion-sso | grep OIDC_ISSUER
```

The second command is the one that says the *running process* has the new value.

**6. Prove it with the thing that measured the failure.**

```bash
./scripts/check-orion-signin.py            # on the edge: reads the local env file
./scripts/check-orion-signin.py --sso-host root@192.168.1.71   # from anywhere else
```

Exit 0 with the note `sign_in_agrees` is the pass. The check is worth reading rather than
just running, because of what it compares: the env file, that issuer's discovery document,
and the `Location` of `/oauth2/start` — which is emitted by the **running** process. So a
forgotten step 5 is caught: the file says one name and the redirect goes to the other, which
is `redirect_wrong_brand`. A check that read only the file would agree with itself and prove
nothing.

```bash
curl -sI https://nas.innotel.us/oauth2/start | grep -i '^location'
# location: https://auth.innotel.us/application/o/authorize/?client_id=e_OcH…&…
```

That header is the same fact from outside the estate, and it is what a person's browser
follows.

**7. The app's copy.** `ORION_OIDC_ISSUER` in the app's `.env` on `192.168.1.89` must be the
same string; recreate the app container if it changed. The proxy is what verifies the
id_token — the app does not — so a stale value here does not produce the 500, which is
precisely why it is easy to leave wrong. `check-orion-signin` does not read it.

## Rollback

```bash
cd /root/orion-sso
cp -a /root/orion-sso/orion-sso.env.pre-issuer-<stamp> /root/orion-sso/orion-sso.env
docker compose -f /root/orion-work/deploy/sso/docker-compose.yml up -d --force-recreate orion-sso
```

If the restored value names a brand the provider does not issue under, the door goes back to
500 at the callback — that is the state being restored, not a new fault, and
`check-orion-signin` will name it (`issuer_mismatch`). The two states are distinguishable,
which is the whole reason the check exists: from outside they were both "a generic 500".

## After it

`estate-checks.service` runs `check-orion-signin.py` every 30 minutes and publishes
`estate-check-orion-signin.prom`; its absence is covered by `EstateCheckAbsent`, so a door
that stops being measured is a page rather than a quiet green. `docs/identity-hosts.md`
records the decision and the consumer table this change moves one row of.
