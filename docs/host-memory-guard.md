# Host memory guard (`memguard` + `earlyoom`)

## The incident it answers

On 2026-10-27 `ord-atheniq` (6 vCPU / 16 GB) stopped answering on every TCP
port while ICMP and WireGuard still replied. The kernel log held **no OOM
record**: the host was not killed, it was thrashing. Swap had filled and every
page fault became a disk wait, so `sshd`, `dockerd` and every service stopped
making progress. It needed a reboot through the Vultr API.

The pressure came from building the MFE image (`tutor images build mfe`), which
compiles thirteen frontends in parallel and peaks around 23 GB of demand on a
16 GB box. The same build later made `earlyoom` (installed at the time of the
freeze) kill `mysqld`, which restarted cleanly but is obviously not acceptable.

## What runs on the host now

| Piece | Purpose |
|---|---|
| `earlyoom` with `--avoid` | Last resort. Kills a runaway *before* swap is gone; the avoid list keeps it away from `mysqld`, `mariadbd`, `redis-server`, `mongod`, `meilisearch`, `dockerd`, `containerd`, `sshd`, `caddy`. |
| `/swapfile2` (16 GB, pri -3) | Headroom for a build. `/swapfile` stays 8 GB at pri -2. Both are in `/etc/fstab`. |
| `memguard.service` | Samples every 15 s; warns below 12 % memory or 15 % swap free, and **cancels a running MFE build** when swap free drops to 6 % (or memory to 4 % with swap under 15 %). It never kills anything except a build it recognises. |

`memguard` is deliberately narrow: low `MemAvailable` on its own is a *warning*,
because a webpack build legitimately fills RAM with reclaimable page cache.
Only swap exhaustion — the actual freeze mechanism — is critical.

Samples land in `/var/log/memguard.jsonl` (rotated at 5 MB) and in the journal
as `memguard`:

    journalctl -t memguard -n 50
    tail -f /var/log/memguard.jsonl

There is no metrics path from this host to the estate Prometheus: `wg0` is a
`/30` to i4 only, and Vault/NPM are LAN-only. To alert off-host, set
`MEMGUARD_WEBHOOK` in `/etc/default/memguard` to any internet-reachable URL; the
guard POSTs `{"text": "..."}` on WARN and CRIT. With no webhook it stays local.

## Installing elsewhere

    install -m755 scripts/memguard.py /usr/local/bin/memguard.py
    install -m644 systemd/memguard.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now memguard
    /usr/local/bin/memguard.py --once     # one sample, no action

Tests: `python3 -m unittest discover -s scripts/tests -p 'test_memguard.py'`.
