# NPM target audit — 2026-11-25

Every proxy host in the cerulean NPM instance was checked against the port it
forwards to, to answer two questions: does the target still listen, and does it
match what the deployment actually publishes?

Method: list `GET /api/nginx/proxy-hosts` (237 hosts), reduce to unique
`forward_host:forward_port` pairs (107), TCP-connect to each **from the NPM host
itself** (192.168.1.71), then re-check every failure by hand and ping its host.
The re-check matters: several targets are docker-bridge or loopback addresses
that only resolve from the NPM host, and a scan from anywhere else reports them
as dead when they are fine.

## Result

| | count |
| --- | --- |
| proxy hosts | 237 |
| unique forward targets | 107 |
| reachable | 101 |
| dead — upstream host powered off | 6 targets / 15 domains |
| dead — stale port | 1 target / 1 domain (fixed) |
| deliberate sink | 1 target / 1 domain |

## Dead: upstream host powered off

None of these five hosts answer ping or any port; the NPM hosts are correct and
will work again when the hosts come back.

| target | NPM hosts | domains |
| --- | --- | --- |
| 192.168.1.61:20140 | 68, 72, 183 | distro.innotel.us, cp.distro.innotel.us, admin.distro.innotel.us |
| 192.168.1.57:3002 | 6, 23, 53, 196 | magnate.innotel.us, admin/app/billing.magnate.innotel.us |
| 192.168.1.44:3000 | 11, 28, 131, 132, 288 | signara.innotel.us, www/app/admin.signara.innotel.us, sign.innotel.us |
| 192.168.1.44:8002 | 19 | api.signara.innotel.us |
| 192.168.1.114:34283 | 327 | cpk.lab.innotel.us |
| 192.168.1.94:443 | 315 | sage.innotel.tech |

Coolify still lists `signara` as a resource (`degraded:unhealthy`), which is
consistent with its host being down rather than with a proxy fault.

## Fixed: stale port

`voice.innotel.site` (NPM host 388) forwarded to `192.168.1.52:8201`. Nothing
ever listened on 8201 — no container publishes it and no Coolify resource claims
it — so the gate redirected to Authentik and the post-login request then failed
upstream. It now points at `http://192.168.1.52:8083`, the same FreePBX portal
that `voice.innotel.us` uses (8089 is the same app over HTTPS and is what
`voice.capstone.innotel.site` and `voice.telno.services` use). The managed
Authentik gate in the host's advanced config was preserved unchanged, and the
chain now resolves `302 -> auth.innotel.site/outpost.goauthentik.io/start ->
auth.innotel.us/application/o/authorize`.

## Deliberate sink

`ontrak.technology` (NPM host 407) forwards to `127.0.0.1:9`, the discard port.
That produces a 502 by design and is the usual way to park a host that must keep
its certificate and DNS record. Left alone; confirm it is still intended before
changing it.

## Reported dead by a naive scan but actually live

Check these from 192.168.1.71 before acting on any future audit:

| target | domains | why |
| --- | --- | --- |
| 192.168.1.71:81 | npm.cerulean.innotel.us | NPM's own admin port |
| 172.17.0.1:8200 | secrets.cerulean.innotel.us | docker-proxy on the NPM host |
| 172.18.0.8:8920 | empire.telno.services | container IP, reachable only from its docker host |
| 172.31.42.10:4180 | admin.monarch.innotel.us, proxy.innotel.us, dns.proxy.innotel.us | in-cluster address |

`empire.telno.services` proxying straight to a container IP is the fragile one
here: the address changes if the container is recreated. It should publish a
host port like everything else.

## Ingress change made in the same pass

Coolify's Traefik proxy on i2 is no longer in the request path. NPM now forwards
`atheniq.innotel.site` and `registrar.innotel.site` (host 412) to
`http://192.168.1.52:8098`, the port the `institution` container publishes via
`atheniq-evo/docker-compose.coolify.yml`. `coolify-proxy` and
`coolify-proxy-error-pages` are stopped, and `coolify-proxy-guard.service` on i2
stops them again within seconds if a Coolify deploy or the panel recreates them:

```bash
systemctl status coolify-proxy-guard.service   # on i2
journalctl -t coolify-proxy-guard -n 20
```

To allow the proxy again: `systemctl disable --now coolify-proxy-guard.service`,
then `docker update --restart=unless-stopped coolify-proxy && docker start coolify-proxy`.

## Standing checks

- A host that stops publishing a port still has an NPM host pointing at it. Run
  this audit after any deployment change; the six entries above are pre-existing,
  not created by this pass.
- Five upstream hosts are off. Either power them on or park their NPM hosts
  (`127.0.0.1:9`, as `ontrak.technology` does) so the failure is deliberate.
