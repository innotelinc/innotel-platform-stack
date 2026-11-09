# The edge's custom nginx snippets

NPM's `nginx.conf` includes four hooks from `/data/nginx/custom/`, and these are
the only places a directive can be added without editing an image:

| File | Included in | What lives there |
| --- | --- | --- |
| `root_top.conf` | main context, before `events{}` | `worker_shutdown_timeout 30s` — so a reload's retiring worker exits even if a client keeps a connection open |
| `events.conf` | `events{}` | `worker_connections 4096`, `multi_accept on` — the image default of 512 was logging "worker_connections are not enough" and turning into 502s |
| `http_top.conf` | top of `http{}` | `large_client_header_buffers 4 32k`, `client_header_buffer_size 16k` — the webmail's per-slot cookies overflowed the defaults and the edge answered 431 before the request reached Next.js |
| `http.conf` | end of `http{}` | `upstream coolify_panel_retry` — the panel arrives over a reverse SSH tunnel, and a lone peer gives nginx nothing to retry against |

Until now these existed only on the proxy host. A rebuilt proxy came back with
the image defaults and none of the reasons above, which is exactly the kind of
loss that shows up later as "the panel 502s" or "sign-in fails sometimes".

They are kept here so the change is reviewable and so a rebuild has something to
restore from. **This directory is not the live copy** — the edge reads
`/usr/src/proxy/data/nginx/custom/` (the host's `/data/nginx` is a stale tree;
see `docs/edge-vhosts-2026-11-09.md`).

## Using it

On the proxy host:

```sh
scripts/sync-proxy-nginx-custom.sh           # report drift, change nothing (exit 1 on drift)
scripts/sync-proxy-nginx-custom.sh --apply   # install, nginx -t, reload
```

`--apply` refuses nothing silently: it writes all four files, runs `nginx -t` and
only then reloads, so a snippet that fails the test leaves the running config
untouched (the files are already on disk, so fix them and re-run).

Edit the checked-in file first, then apply — the reverse (hand-editing the host)
is how these drifted out of review to begin with.

`atheniq-proxy-host-206.conf.bak` in the live directory is leftover debris from a
removed vhost and is deliberately not captured here.
