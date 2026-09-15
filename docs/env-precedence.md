# Ambient environment precedence — the stale-export footgun

Every repo in the stack resolves its settings the same way, and it is the reason
one bad shell can retarget a different service:

```
real environment  →  this repo's .env  →  built-in default
```

*The real environment wins.* That is deliberate — operators need to override a
value for one run — but it means a variable exported for one stack silently
becomes the value for every other stack that reads the same key. The whole
ecosystem uses the same names (`NPM_BASE_DOMAIN`, `NPM_API_URL`, `NPM_EMAIL`,
`NPM_PASSWORD`, `NPM_UPSTREAM_HOST`, `FREEPBX_AMI_SECRET`, …), so this is not a
theoretical hazard.

## What it has already broken

| Symptom | Cause |
| --- | --- |
| `rizzaura`'s NPM sync reported "missing certs on `capstone.innotel.us`" and would have pruned 11 Capstone hosts | the operator shell exported `NPM_BASE_DOMAIN=capstone.innotel.us`; Rizz Aura's script trusted it |
| Zeus showed **AMI Offline** while Asterisk logged `failed to authenticate as 'zeus-portal'` every 30s | the portal's `ASTERISK_AMI_SECRET` came from Compose interpolating the shell's `FREEPBX_AMI_SECRET`, while the PBX section was written from `.env` — two sources, one credential |
| Cerulean's NPM sync was writing Capstone's hosts | same stale `NPM_BASE_DOMAIN`, same trust |

## The rule

A script that resolves **which domain or credential it owns** from the ambient
environment must refuse when the ambient value contradicts the repo's own
`.env`. Env-first is fine for tunables (timeouts, ports); it is not fine for
*identity*.

```python
ambient = (os.environ.get("NPM_BASE_DOMAIN") or "").strip().lstrip(".").lower()
own = (args.env.get("NPM_BASE_DOMAIN") or "").strip().lstrip(".").lower()
if ambient and own and ambient != own and not args.base_domain:
    print(f"FAIL NPM_BASE_DOMAIN={ambient} is exported in the environment but this "
          f"repo's .env says {own} — refusing to touch {ambient} hosts "
          f"(unset the variable, or pass --base-domain explicitly).", file=sys.stderr)
    return 1
```

The explicit flag stays an escape hatch, so an intentional cross-domain run is
still possible — it just has to be said out loud.

## Audit

**Guarded — refuse a foreign ambient domain**

| Script | Watches | Writes / deletes |
| --- | --- | --- |
| `2-voice/capstone/scripts/npm-proxy-hosts.py` | `NPM_BASE_DOMAIN` | creates + prunes NPM hosts |
| `2-voice/capstone/scripts/authentik_bootstrap.py` | `NPM_BASE_DOMAIN` | writes the external host + cookie domain into Authentik |
| `4-social/rizzaura/scripts/npm-proxy-hosts.py` | `NPM_BASE_DOMAIN` | creates + prunes NPM hosts |
| `2-voice/zeus/scripts/npm-proxy-hosts.py` | `NPM_BASE_DOMAIN` | creates + prunes NPM hosts |
| `1-primary/cerulean/scripts/npm-proxy-hosts.py` | `NPM_BASE_DOMAIN` | creates NPM hosts |

**Reviewed — no guard needed**

| Script | Why |
| --- | --- |
| `2-voice/capstone/dashboard-backend/app/main.py` | reads `NPM_BASE_DOMAIN` to build its own links; runs inside the Capstone container, whose environment is the Capstone stack's |
| `1-primary/signara/infra/nginx/npm-proxy-hosts.py` | reads `BASE_DOMAIN`, not `NPM_BASE_DOMAIN`, and defaults to its own domain — the shared `NPM_*` exports cannot retarget it |
| `1-primary/signara/scripts/provision-edge.sh`, `infra/cerulean/provision.py` | distinct variable names (`BASE_DOMAIN`, `CERULEAN_BASE_DOMAIN`) with their own defaults |
| `3-media/monarch/scripts/npm-proxy-hosts.py` | derives its zone from its own `.env` keys, never from `NPM_BASE_DOMAIN` |
| `ips/scripts/{subscribe-hosts,sync-subscribe-pages}.py` | the host list comes from the portal's own pages, not from the environment |
| `*/scripts/stack-lib.sh` → `stack_lib_env` | env-first *by convention*; the guard belongs at the call site, not in the helper |

**Credentials**

| Item | Status |
| --- | --- |
| Zeus AMI secret (`FREEPBX_AMI_SECRET` / `ASTERISK_AMI_SECRET`) | fixed: the portal adopts the password from the mounted `manager_custom.conf` on boot, and the PBX entrypoint rewrites its `[<user>]` secret every start — the credential now has one owner regardless of what the shell exports |
| `2-voice/zeus/scripts/{setup-portal,smoke-test}.sh` | read `FREEPBX_AMI_SECRET` from the ambient environment; they write/verify local config rather than sync remote state, so a stale value fails loudly instead of deleting anything |

## When adding a script

1. If it decides **which domain, tenant, or credential it owns**, add the guard
   above — before the first write, not after.
2. Prefer a name unique to the service for service-specific values; the shared
   `NPM_*` names are reserved for the edge that all of them talk to.
3. Test the guard negatively (export a sibling stack's domain and confirm it
   refuses), then run the script with a clean environment and confirm it is
   still green. A guard that passes its own happy path proves nothing.
