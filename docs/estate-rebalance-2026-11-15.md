# Estate rebalance — inventory and options, i1 / i2 / i3 / i4

**Status: partially applied 2026-11-15 — see §11 (15:45–16:05 EST), §12 (21:10–22:00 EST), §13 (the Tix cap under load, and a check that counts zombies) and §14 (i2 → i3 displacement, in progress while i2 is reinstalled).** The inventory below is
the **pre-change snapshot**, kept as measured. §11 records what was applied with before and
after numbers, and **four corrections to findings in this page** — including one that would
have deleted the production Tix database. Read §11 before acting on §4, §5, §7 or §9.

Every number below was
read live over SSH on **2026-11-15** between 20:00 and 21:30 EST, from `free`, `lsblk`,
`zpool list`, `incus list`, `incus config show`, `docker ps`, `/proc/pressure/*` and the
cgroup files under `/sys/fs/cgroup/lxc.payload.*`. Where a figure could not be trusted it
says so instead of being estimated.

The question this page answers: the four hosts have drifted into a shape nobody chose —
`i3` is a fresh, empty machine, `i2` is the busiest and the tightest for disk, the only
SSD in the estate sits in the weakest CPU, and 31 database engines run across 27
containers. What is the best arrangement, and what does each change cost?

---

## 1. The four hosts as they are

| | **i1** `.51` | **i2** `.52` | **i3** `.53` *(new)* | **i4** `.54` |
|---|---|---|---|---|
| Machine | HP Slim Desktop, **bare metal** | **QEMU guest** (Proxmox pm3) | **bare metal** *(replaced 2026-11-15)* | Surface Pro, bare metal |
| CPU | i3-8100, **4c / 4t** @3.60 | i7-1165G7, **8 vCPU** | i3-550, **2c / 4t** @3.20 | i5-7300U, **2c / 2t** @2.60 |
| SIMD / crypto | AVX2, AES-NI | AVX-512, AES-NI | **no AVX, no AVX2, no AES-NI** | AVX2, AES-NI |
| VT-x / `/dev/kvm` | yes / yes | yes / yes | yes / **yes** | yes / no |
| RAM | 15 279 MiB (**6 165 free**) | 13 419 MiB (**3 674 free**) | 15 403 MiB (**14 843 free**) | 7 341 MiB (**3 268 free**) |
| Swap in use | 2 638 / 4 095 MiB | 893 / 12 287 MiB (4 G is zram) | **0** / 4 095 MiB | 933 / 4 095 MiB |
| Load 1 min | 0.73 | **2.57** | **0.00** | 1.22 |
| CPU PSI `some` 10 s | 3.5 % | **18.3 %** | — | 4.6 % |
| Root fs | 100 G, 48 % | 50 G, **74 %** (12 G free) | 98 G, **8 %** | 155 G, 67 % (50 G free) |
| Container storage | zfs `tank` 824 G — 374 G used, **450 G free**, 45 % cap, 37 % frag | zfs `fast` 298 G — 224 G used, **73.5 G free**, 75 % cap, **53 % frag** | **none installed**; VG has **363.76 G unallocated** | `dir` pool on the NVMe; VG has **80 G unallocated** |
| Disk device | `ST1000DM003` **ROTA=1**; `sdb` ASM1153E USB **ROTA=1** (465.8 G, 431 G free, mounted read-only) | `QEMU HARDDISK` ×2, **ROTA=1** | `WDC WD5000AVDS` **ROTA=1** (5400-class AV drive) | `Skhynix BC501` **NVMe, ROTA=0** |
| Incus containers | 12 running + 1 stopped | 9 running | **0** | 6 running |
| Docker containers inside them | **68** | 57 | 0 | 13 |
| Docker on the **host** (outside Incus) | 0 | **6** | 0 | **4** |
| Memory caps summed | 14 848 MiB on 15 279 | **16 128 MiB on 13 419 (120 %)** | — | 7 680 MiB on 7 341 (105 %) |

**The single most important line in that table is the disk-device row.** Every host in
this estate except `i4` is on spinning rust, and `i3`'s new disk is a `WD5000AVDS` — a
5400 rpm-class AV/surveillance drive. That is the slowest device here for small random
writes, which is exactly what a database, a mail index and a Docker overlay do. It is
also the only host with real headroom.

> **A speed test was attempted and discarded.** A 64 MiB `fsync` harness returned
> sub-10 ms on all four hosts including the 5400 rpm disk, which cannot be real — the
> figure was measuring page-cache acceptance, not the platter. No throughput numbers are
> claimed in this page. The device classes above, and the previously recorded
> measurements (i1 `tank` 57 MB/s fsync, i4 NVMe 372 MB/s fsync, from
> `hp-capacity-plan.md`), are what the recommendations rest on.

---

## 2. What is actually running

27 Incus containers, 138 Docker containers inside them, 10 more Docker containers on the
two hypervisor hosts themselves, and **31 stateful database engines**.

### Per host, by weight

| Host | Heaviest containers (container disk / measured RAM / cap) |
|---|---|
| **i1** 148 G | `ontrak` 29.9 G · 1 183/2 944 MiB · **27 Docker containers** (OnTrak family, 2 Guacamole pairs, 3 Postgres, claude-mem) — `workloads` 27.3 G · 2 048 cap (Coolify: `coolify`, `coolify-db`, `coolify-redis`, `sentinel`) — `atheniq` 26.1 G · 1 594/4 864 MiB · 22 (Tutor LMS/CMS, OpenMAIC, 2 SSO, MySQL+Mongo+Redis+Meilisearch) — `atlas` 25.3 G · 4 (Gitea + Convex) — `mail` 10.4 G · 368/1 024 (Stalwart + Bulwark) — plus `nas`, `blueprint`, `clearpath`, `genesis`, `genie-preview`, `regcache`, `rizzaura` |
| **i2** 86 G | **`proxy` 36.1 G · 3 536/7 424 MiB · 24 Docker containers** — NPM (209+ host configs), Authentik + its Postgres, Technitium DNS, Vault, Prometheus, Grafana, OmniRoute, ntfy, MariaDB, 3 Redis, 6 oauth2-proxy sidecars — `monitor` 14.8 G · 1 400/2 944 (SigNoz) — `signara` 9.7 G · 374/1 024 · 10 — `onyx` 8.2 G · 200/768 · 14 — `magnate` 3.8 G — `copilotkit` 2.7 G · 1 286/2 176 — `distro` 2.5 G — `pi` 1.3 G — `subscribe` 1.0 G |
| **i3** 0 | **Nothing.** 14.8 G of RAM free, 4 threads idle, 363.76 G of unallocated disk, no Incus, no Docker. |
| **i4** 34.6 G | `terminal` 9.7 G · 303 MiB (**soft cap**) — `vault` 9.3 G · 506 MiB (**soft cap**) · 7 containers (Vaultwarden, Linkwarden + PG + Meilisearch, registrar-sso, atheniq-console) — `vpn` 5.8 G · 69 MiB (**soft cap**) — `telno` 4.5 G · `sage` 3.7 G · `telno-turn` 1.6 G |

### Stateful engines, all 31

| Engine | Count | Where |
|---|---|---|
| **Postgres** | **12** | ontrak×3 (training `:5432`, tix `:5433`, sentinel `:5434`, all 16-alpine) · openmaic 16 · gitea-db 16-alpine · coolify-db **15** · signoz-metastore 16 · authentik 16-alpine · signara · linkwarden 16.16 · **`ontrak-tix-db-1` on i4's host Docker** · **`scif-database-1` 17-alpine on i2's host Docker** |
| Redis | 11 | tutor 7.4.10 · atheniq-sso-sessions · coolify · gateway-sso-sessions · authentik · cerulean-sso-sessions · signara · registrar-sso-sessions · bolt-sso-sessions · g0dm0d3-sso-sessions · linkwarden (`:6380`) |
| MySQL / MariaDB | 3 | tutor `mysql:8.4.11` · npm-db `mariadb-aria` · **`fax-staging-db-1` `mariadb:11` on i4's host Docker** |
| MongoDB | 1 | tutor `mongo:7.0.39` |
| Meilisearch | 3 | tutor **1.36.0** · signara **1.10** · linkwarden **1.13.3** |
| ClickHouse | 1 | signoz `25.12.5` (+ Keeper) |

All twelve `oauth2-proxy` sidecars point at the **one** Authentik provider.

---

## 3. Seven findings that drive every option below

### F1 — `i3` is the only spare capacity in the estate, and its disk is the worst in it
14 843 MiB free, four idle threads, zero containers, 363.76 G unallocated. But
`WDC WD5000AVDS`, rotational, single spindle. **This host is right for RAM-heavy,
IO-light, bulk work — backups, cold data, VM images — and wrong for anything doing small
random writes.** That single fact rules out the obvious "put the databases on the empty
box" move.

### F2 — the estate's only SSD is in its weakest CPU
`i4` has 2 threads and 7.3 GiB (3.3 free) but the only NVMe: 50 G free plus 80 G
unallocated in its volume group. It is the right home for small, IO-sensitive,
RAM-light services — which is exactly what already lives in `vault` and `terminal`.

### F3 — `i4`'s memory caps are not caps (measured, not inferred)
Three of its six containers are configured `limits.memory.enforce: soft`, and the cgroup
shows what that means:

| Container | `memory.max` | `memory.high` | `memory.swap.max` |
|---|---|---|---|
| `terminal` | **max** | 1 610 612 736 | **max** |
| `vault` | **max** | 2 281 701 376 | **max** |
| `vpn` | **max** | 805 306 368 | **max** |
| `sage` | 1 879 048 192 | max | 0 |

There is **no hard ceiling and no swap ceiling** on three containers, on a 7.3 GiB host
whose declared caps already sum to 7 680 MiB. `memory.high` throttles; it does not kill.
A runaway in `vault` (Vaultwarden + Linkwarden + Meilisearch + Postgres) can therefore
drive the host into swap and stall everything on it — including `vpn` and `terminal`.
This is the cheapest real fix in this report.

### F4 — `i2` is the load centre and the disk-tight host, and its edge is one failure domain
CPU PSI **18.3 %** (the only host above 5 %), load 2.57, 3 674 MiB available of 13 419,
root at **74 %** with 12 G free, `fast` at **75 % with 53 % fragmentation**, and memory
caps summing to **16 128 MiB on a 13 419 MiB host (120 %)**.

Inside it, `proxy` is a **single Incus container holding the entire public edge**: NPM
with 209+ host configs, Authentik and its Postgres, Technitium (the authoritative DNS
for the estate), Vault, Prometheus, Grafana, OmniRoute, ntfy, MariaDB, three Redis and six
oauth2-proxy sidecars — 24 containers, 36.1 G of disk, 3 536 MiB of a 7 424 MiB cap. One
`docker compose down` on one stack, or one container-level OOM, takes **every public name
and DNS** down together. That is a structural risk, not a performance one.

### F5 — three observability stacks, and one of them is orphaned
- `proxy` on i2: `metrics-prometheus` + `metrics-grafana` + `node-exporter` — where the
  estate checks publish (`innotel_estate_check`).
- `monitor` on i2: SigNoz + ClickHouse + Keeper + otel-collector + metastore-postgres —
  14.8 G disk, 1 400 MiB RAM, 2 944 MiB cap.
- `monarch` on i1: recorded here as **STOPPED**. **Wrong — see §11.2.** It is running and
  hosting a 37-container media stack, and it is the cause of an I/O storm on i1.

Nothing I can find dials `192.168.1.88`: no NPM `proxy_host` or `redirection_host`
references it, `signara-alertmanager-1`'s config contains no reference, and
`metrics-prometheus` has no scrape target for it. `service-audit.md` already records the
SigNoz/ClickHouse topology as **retired** (Capstone's collector converts spans to RED
metrics and drops them). **14.8 G on the tightest disk in the estate is serving a stack
nothing reads.** Confirm with the owner before deleting — a person may open its Grafana
by hand — but this is the largest single reclaim available.

### F6 — three services exist twice, and every duplicate sits on host-level Docker
| Service | Copy A | Copy B |
|---|---|---|
| OnTrak Tix database | `ontrak-family-tix-db-1` inside `ontrak` on i1 | `ontrak-tix-db-1` on **i4's host** Docker |
| `clearpath` | Incus container `clearpath` `.83` on i1 | container `clearpath` on **i4's host** Docker |
| `telno` | Incus container `telno` `.95` on i4 | `christ-telno` on **i2's host** Docker |

**Correction to the row above — see §11.3.** The OnTrak Tix database on i4's host Docker is
**not the duplicate**: it holds **3 884 MB and 5 001 081 tickets**. i1's copy is 14 MB and
empty. Deleting the i4 copy destroys production data.

Host-level Docker is outside every memory cap, outside the address table, and outside
every estate check — which is precisely how the duplicates survive unnoticed. i2's host
also runs `scif-api-1` + `scif-database-1` (a 12th Postgres, v17), and the `christ`
family publishing UDP `41000-41063` and `7443-7444` on the hypervisor.

### F7 — disk is not the constraint; RAM and per-host IOPS are
Free space across the estate: i1 **450 G** (`tank`) + **431 G** (`/srv/vault-ro`, read-only
by design) · i2 **73.5 G** · i3 **363.76 G** + 86 G on `/` · i4 **50 G** + **80 G**
unallocated. That is roughly **1.5 TB free** against ~2.1 TiB in use. Nothing needs a new
disk for capacity. A new disk would only be worth buying to put an **SSD** where the
databases are — and that is a different argument from this one.

---

## 4. Disk management — what to actually do

| # | Host | Action | Gain | Risk |
|---|---|---|---|---|
| **D1** | i4 | `lvextend -l +100%FREE` on `ubuntu-lv` + `resize2fs` (online, ext4) | `/` 67 % → **~44 %**, +80 G | none |
| **D2** | i4 | `docker builder prune -af` — measured **14.52 G of build cache, 13.12 G reclaimable**; plus 1.17 G unreferenced images | **~14 G** | none (rebuildable) |
| **D3** | i2 | Delete `monitor` after owner confirmation (F5) | **+14.8 G disk, +1.4 GiB RAM**, −2 944 MiB of cap on the loaded host | loses the SigNoz UI; nothing dials it today |
| **D4** | i3 | `lvcreate -L 350G -n incus ubuntu-vg`, then an Incus **`dir`** pool on it (or zfs if snapshots are wanted) — leave `/` at 98 G, 8 % | **350 G** of usable container/VM storage | none; disk is 5400 rpm so keep random-write work off it |
| **D5** | i2 | `zpool` frag is **53 %** — a trim + scrub, and confirm `autotrim` is on | better allocation behaviour, no capacity change | none, but takes hours on a spinner |
| **D6** | i1 | `tank` is healthy at 45 % / 450 G free — **leave it alone**. `/srv/vault-ro` (431 G free) is mounted read-only on purpose as the vault replica; it is *not* free space until someone decides to remount it read-write | — | changing its mode is a policy decision, not a tidy-up |

After D1–D3, **no host is above 70 % on any filesystem** except i2's `fast` (still 75 %
until more than `monitor` leaves).

---

## 5. The central-Postgres question — an honest answer

This is the request that most deserves a straight answer, so here it is with the
arithmetic rather than the vibe.

**"One Postgres for everything" is not supportable on this hardware, and I would not do
it even with better hardware.** Two reasons:

1. **No host can carry it.** The only SSD is i4 (2 threads, 7.3 GiB, 3.3 free). The only
   spare RAM is i3 (5400 rpm disk). A central instance on i4 makes it a single point of
   failure for every product at once, on the estate's least powerful CPU.
2. **The estate already decided this**, in `service-audit.md`: *"Each platform owns its
   own application database — data isolation is the tenant boundary. A shared Postgres
   would couple tenant data across platforms."* Reversing a documented decision because
   it looks tidier is how a tenant boundary gets deleted by accident.

But the request is still right about a real problem — there are 31 engines for 27
containers, most of them idle. The useful split is **by what the data is**, not by
engine:

### Tier 1 — merge these (infrastructure state, not tenant data)

| Merge | From → to | Saves |
|---|---|---|
| **Session stores** | 6 `oauth2-proxy` session Redis + `cerulean-sso-sessions`, `registrar-sso-sessions`, `bolt-sso-sessions`, `g0dm0d3-sso-sessions` → **1 Redis**, one DB index or key prefix per client | **9 containers → 1**; a flush logs people out and loses nothing |
| **OnTrak's three module databases** | `training-db` + `tix-db` + `sentinel-db` (same image 16-alpine, same host, same product) → **1 Postgres, 3 databases + 3 roles** | **3 → 1**; no tenant boundary crossed — it is one product |
| **`ontrak-tix-db-1`** (i4 host Docker) | → the OnTrak instance on i1, or delete if unused | removes a duplicate *and* a host-level stray |
| **Meilisearch** (optional) | 1.10 / 1.13.3 / 1.36.0 → one current instance | 3 → 1, but **each 1.x upgrade rewrites the index**, so every consumer needs a reindex window. Medium risk; not worth doing before the rest. |

### Tier 2 — leave per-application (name the reason)

| Engine | Why it stays |
|---|---|
| `cerulean-authentik-postgres` | Identity is the trust root; its own instance is the security boundary and its upgrade cadence follows Authentik's |
| `atlas-gitea-db` | Code plane — same reasoning |
| `coolify-db` (15) | Coolify's own lifecycle and upgrade path own it |
| `openmaic-postgres`, `signara-postgres`, `linkwarden-postgres` | Tenant data; each is the product's restore boundary |
| `tutor` MySQL + Mongo | Open edX's schema, upgraded by `tutor` |
| `signoz-metastore-postgres` | Moot — it leaves with `monitor` (D3) |

**Net after Tier 1: 12 Postgres → 10, 11 Redis → 2–3, 40 Docker containers fewer on i1.**
That is real deduplication. A single central cluster would be fewer still and worse.

### The one architectural dedup worth more than any database merge

**Twelve `oauth2-proxy` sidecars for one Authentik.** Authentik's own **embedded outpost**
handles many applications behind one container, with per-provider cookie domains and
redirect URIs. Replacing 12 sidecars with one outpost removes 11 containers, 11 images,
11 config surfaces and 11 places a `--cookie-domain` can drift. It is a real migration
(12 apps re-registered, one bad callback list breaks sign-in) — but it is the largest
container-count win in the estate, and it belongs in Scenario C, not this quarter's week.

---

## 6. Containers out of Incus, or into their own — three patterns

The request asks whether projects can leave their Incus containers. Three shapes exist
here, and only two are good.

**Pattern 1 — container → host Docker (already happened by accident; do not repeat it).**
`i2`'s host runs the `christ` family + `scif-*`; `i4`'s host runs `fax-staging`,
`clearpath` and a duplicate OnTrak DB. These have **no memory cap, no CPU cap, no
address-table row and no estate check**. It is the pattern that produced the three
duplicates in F6. Recommendation: **move them into an Incus container** (`christ`, `fax`,
`scif`) so they get a cap and a row, rather than leaving them uncapped on the hypervisor.

**Pattern 2 — one fat container → several by function (the real win).**
`proxy` on i2 holds the entire edge. Split it into four, each with its own cap and address:

| New container | Contents | Why separate |
|---|---|---|
| `edge` | NPM + its MariaDB + the 12 SSO sidecars (or 1 outpost) | It is the thing that must never die with anything else |
| `identity` | Authentik + Postgres + Redis | Identity is the trust root; it deserves its own failure domain |
| `net` | Technitium DNS + Vault + ntfy | Authoritative DNS outliving the web edge is worth a container on its own |
| `gateway` | OmniRoute + gateway-sso + its Redis | Already has its own compose and its own secrets; today it just happens to share a cgroup |

Cost, stated plainly: each split container needs a **new LAN address**, a netplan pin
(`netplan generate` + `systemctl restart systemd-networkd` — `apply` cannot run inside a
container), a row in `check-container-addresses.py`, and NPM/DNS follow-through. The
2026-11-02 lesson is exact here: a container left off that table is a `FAIL` on every run
for every host. `.64`, `.65`, `.67`, `.68`, `.72`, `.74`, `.75`, `.77`, `.78` and
`.80`–`.87` do not answer today, so addresses exist — **but "no reply" is not proof of
freedom**, and each must be confirmed against the table before use.

**Pattern 3 — Docker container → shared Incus container.** The i4 `fax-staging` pair and
the i2 `scif` pair are single-purpose; each is one small `docker compose` that would fit
in a 512 MiB container and gain a cap, an address and a check. Cheap, low risk.

---

## 7. OnTrak's Windows requirement — where the hypervisor goes

**What is there now, measured:** the `ontrak` container on i1 has `/dev/kvm` and
`/usr/bin/qemu-system-x86_64`, capped at **2 944 MiB / 2 vCPU**, and carries **two**
Guacamole+guacd pairs (`ontrak-family-guacamole-1`/`guacd-1` *and* `ontrak-guacamole`/
`ontrak-guacd` — itself a duplicate). There are **no _running_ Incus VMs** and `libvirtd`
is inactive on all four hosts. **Correction: see §11.4** — i2's `fast` pool already holds
**138 G of VM images** under project `ontrak`.

| Option | Host | Verdict |
|---|---|---|
| **W1** keep guests inside `ontrak` | i1 | Works today, zero work. But the container cap (2 944 MiB) is *below* what a Windows guest wants, so the VM and the 27 app containers fight over one cgroup, and the qcow2 sits on the 1 TB HDD shared with 68 Docker containers. |
| **W2 → recommended** dedicated Incus **VM** on `i3` | i3 | **14.8 GiB free RAM**, VT-x, `/dev/kvm`, and a 350 G LV for qcow2 images on a mostly-empty disk. An Incus VM gets its own hard memory and CPU and does not share a container cap. This is the best home in the estate. |
| **W3** host-level KVM on `i1` | i1 | Better CPU than i3 (4 cores, AVX2), but the guest would land on the same spindle as 68 Docker containers and 12 containers, with 2.6 GiB already swapped out. |
| **W4** `i2` | i2 | Best CPU (8 threads, AVX-512) and the worst place for it: PSI 18 %, 75 % disk, 53 % fragmentation. **No.** |
| W5 the Proxmox hosts `pm3` / `pm4` | — | **Unverifiable today.** `pm3.innotel.us` A-resolves to `73.68.203.71` (the public edge IP, not a host) with `:8006` closed; `pm4.innotel.us` has no A record. They used to run i2 and i3 as VMs. If either is still alive, a real hypervisor is the correct home for Windows guests and worth ten minutes of checking. |

**The honest limit of W2:** the i3-550 is 2010 silicon — 2 physical cores at 3.2 GHz, no
AVX, no AES-NI. One or two concurrent Windows lab guests will run acceptably because
they are mostly idle. A classroom of ten is not going to work on this box, and no
rearrangement of the four servers changes that: it needs a real hypervisor host. Also
note that with no AES-NI, TLS-heavy work should stay off i3.

---

## 8. Three complete scenarios

### Scenario A — tighten, don't move (≈ half a day, no downtime)
D1 + D2 + D3 + the `ontrak-tix-db` duplicate + the session-Redis merge.

- **Buys:** ~28 G of disk, 1.4 GiB of RAM and 2 944 MiB of cap back on the loaded host,
  real ceilings on `i4`, 9 fewer containers, three fewer duplicate services.
- **Costs:** one confirmed decision (delete `monitor`), no service moves, no address
  changes, no new checks.
- **Leaves:** the `proxy` single-point-of-failure, i2's 53 % fragmentation, i3 empty.

### Scenario B — one host per job *(recommended)* (≈ 2–3 days)
Everything in A, plus:

| Host | New role | Moves |
|---|---|---|
| **i1** (4c, 15.3 G, 450 G free) | **Application plane** — it has the most disk headroom and the estate's tenants are already there | keeps everything; sheds the OnTrak Windows guest to i3 (W2) once proven |
| **i2** (8t, tight disk) | **Edge + identity plane** | sheds `monitor` (A) and `copilotkit`'s k3s if unused; `proxy` split per Pattern 2 if the team can absorb one window |
| **i3** (idle, 5400 rpm) | **Bulk, cold and virtualization** | gains an Incus VM host (W2), the estate's cold/backup target on its 350 G LV, and telemetry (`prometheus`/`grafana`/`node-exporter` — write-light, RAM-light, and it frees 1.5 GiB of the i2 cap) |
| **i4** (NVMe, 2t) | **Small, fast, IO-sensitive** — unchanged in kind, fixed in shape | gains 80 G root; hosts the `fax`/`clearpath` strays as Incus containers instead of host Docker |

- **Buys:** every host above the estate's own 80 % disk warning line disappears; i2's
  PSI drops with `monitor` and telemetry gone; the edge outlives an identity or DNS
  restart; i3 stops being an empty machine.
- **Costs:** 2–3 containers' worth of address changes and one window per split; the
  ops docs (`container-placement.md`, `host-map.md`) must be updated in the same pass or
  the checks will fail on every run.

### Scenario C — rebuild the data plane (1–2 weeks + hardware)
Buy **one SSD** (1 TB SATA is enough) for the box that will hold the data tier, then move
the Tier-1 engines onto it under a single Postgres cluster with per-application databases,
roles and connection pooling, honestly labelled as **one failure domain**. Replace the 12
`oauth2-proxy` sidecars with one Authentik outpost.

- **Buys:** the largest container-count reduction available (~40 fewer containers), one
  engine to patch, snapshots/backups in one place.
- **Costs:** a hardware purchase; reversing a documented tenant-isolation decision;
  a pool-wide outage mode that does not exist today; a 12-app SSO migration.
- **Verdict:** only worth it *after* B, and only with the SSD. Without an SSD, moving
  the data tier onto the WD5000AVDS would make every product slower than it is now.

---

## 9. Recommended order

1. **D1 + D2 on i4** — 80 G and 14 G back, online, reversible, no decision needed.
2. **F3 on i4** — set `limits.memory.enforce: hard` and `limits.memory.swap: 0` on
   `terminal`, `vault`, `vpn` (sized from their measured 303 / 506 / 69 MiB, at ~2×).
3. **D3 on i2** — confirm `monitor` is unread, then remove it: the biggest single win.
4. ~~**Kill the duplicates** — `ontrak-tix-db-1` on i4~~ — **DO NOT DO THIS. See §11.3.**
   The i4 copy is the live database. The correct action is to **migrate it into a managed
   Incus container**, not delete it. `clearpath` still needs its copy decided (§11.3).
5. **Merge the session Redis** (9 → 1) and the three OnTrak databases (3 → 1).
6. **D4 on i3** — build the 350 G pool, then stand up the Windows VM (W2) there.
7. **Only then** consider splitting `proxy` (Pattern 2), because it is the one change that
   touches every public name at once and needs the address table, netns pins and docs
   updated in the same pass.

Steps 1–5 change no service's address and can be done in one evening.

---

## 10. What was not verified

- **No throughput numbers are claimed.** The quick `fsync` harness was unreliable on all
  four hosts and is discarded (see §1).
- **`monitor`'s fate is a judgement, not a fact.** Nothing in NPM, Alertmanager or
  Prometheus references `.88`; a human may still open its Grafana directly. Confirm.
- **"Free" addresses are "did not answer", not "allocated".** `.64`, `.65`, `.67`, `.68`,
  `.72`, `.74`, `.75`, `.77`, `.78`, `.80`–`.87` did not answer on 2026-11-15; the
  address table in `check-container-addresses.py` remains the authority.
- **`pm3` / `pm4` could not be checked** — see W5. If they are alive, the Windows answer
  improves and Scenario B's i3 VM becomes a fallback rather than the plan.
- **`i3`'s role is inferred from its device, not benchmarked.** `WD5000AVDS` is an AV/GP
  5400 rpm-class drive; that is a documented product class, not a measurement of this
  spindle under this workload.
- **The `i1` swap figure (2 638 MiB)** is the Tutor stack (`mysqld` 350 MiB, celery
  workers, `mineru-api`), not a runaway; it is stale pages from a 4 864 MiB cap that has
  room to spare, and no action is recommended from it.

---

## 11. What was applied, and four corrections

Applied **2026-11-15, 15:45–16:05 EST**, on i2 and i4 only. `i3` was offline for an OS
reinstall and nothing in §6/§7 touching it was attempted.

### 11.1 Applied — measured before and after

| Step | Host | Before | After | Verified by |
|---|---|---|---|---|
| **D1** `lvextend -l +100%FREE` + online `resize2fs` | i4 | LV 155.42 GiB; `/` 153 G, **50 G free, 67 %** | LV 235.42 GiB; `/` **232 G, 139 G free, 38 %** | `df -h /`, `vgs` (VFree 80 G → 0) |
| **D2** `docker builder prune -af` | i4 | build cache **14.52 GB**, 98 G used | cache **0 B**, **84 G used** (−14 G) | `docker system df`, `df` |
| **F3** `limits.memory.enforce: hard`, `limits.memory.swap: 0` | i4 | `terminal`/`vault`/`vpn` at `memory.max=max`, `swap.max=max` | `memory.max` = 1 610 612 736 / 2 281 701 376 / 805 306 368; `memory.high=max`; `swap.max=**0**` | cgroup files read back **without a restart** |
| **D3** stop then `incus delete monitor` | i2 | `fast` 224 G alloc, **73.5 G free, 75 % cap**; avail RAM 2 877 MiB; CPU PSI **18.3 %** | `fast` **210 G alloc, 88.3 G free, 70 % cap**; avail RAM **4 293 MiB**; CPU PSI **1.06 %** | `zpool list`, `free -m`, `/proc/pressure/cpu`; 9 containers → 8 |

`monitor` was deleted only after a sweep found **no** reference to `192.168.1.88` in any
container environment or config file on i2, in the host-level Docker containers, in any
NPM `proxy_host`/`redirection_host`, or in Prometheus — consistent with §F5. Its 15 G of
SigNoz/ClickHouse data is **gone and not recoverable**; the stack can be rebuilt from
compose if it is ever wanted again.

`i4` memory: 4 146 MiB used of 7 341, 3 195 available, after the cap change.

### 11.2 Correction — `monarch` on i1 is running, and it is why i1 is overloaded

This page recorded `monarch` as a stopped container. It is **RUNNING** (started
`2026/11/15 15:43 EST`, `boot.autostart: false`, so it was started deliberately) and holds
**37 Docker containers**: `transmission-daemon`, Jellyfin, Sonarr, Radarr, Lidarr,
Whisparr, Prowlarr and `next-server`. `limits.memory: 4864MiB`, `limits.cpu: 2`, currently
using **3.87 GiB**.

Measured on i1 while it ran:

| Metric | Value |
|---|---|
| Load average | **86.03 → 54.76 → 46.60** (1 min) |
| I/O PSI `some` / `full` | **98.4 % / 77.5 %** — 77 % of wall time stalled on I/O |
| `iostat sda` | **88.9 % util**, 9 fsync/s at **99 ms** `f_await`, `%iowait` 80 % |
| Blocked processes | 30–50 in `vmstat` `b` column |
| Swap | 3 095 / 4 095 MiB used |

**Nothing was stopped.** A media library scan is the user's call, not a rebalance decision,
and the stack started 2 minutes before this work began. **This is the first thing to decide
on i1:** while I/O pressure is 98 %, no migration, database merge or VM work on i1 can be
measured honestly.

### 11.3 Correction — the live OnTrak Tix database is on i4, not i1

§F6 and step 4 of §9 both assumed i1 held production. They had it backwards:

| Copy | Location | Size | Data |
|---|---|---|---|
| `ontrak-tix-db-1` | **i4 host Docker**, `:5432`, volume `ontrak-tix_ontrak-tix-db` | **3 884 MB** | **5 001 081 `Ticket`, 2 401 047 `Message`, 60 000 `User`, 1 427 `AuditEvent`** |
| `ontrak-family-tix-db-1` | inside `ontrak` on i1 | 14 MB | `pg_stat_user_tables` empty — an empty shell |

Following the original instruction would have destroyed five million tickets. **The correct
action is the opposite of what this page first said:** move the i4 instance *into* a managed
Incus container (with a cap, an address row and an estate check), and retire the empty i1
shell. No connection was open to it (`ss -tnp` showed nothing on `:5432`) at the time of
measurement, so its consumer must be identified before any move.

`clearpath`: NPM `proxy_host` 71, 274 and 275 all point at **`192.168.1.83`** (i1's Incus
container), so the served copy is i1's. i4's host-Docker `clearpath` (`:3001`, created
**2026-11-14**, i.e. the day before this audit) appears in no NPM host and is **left
running and undecided** — it is recent enough that it may be work in progress.

### 11.4 Correction — i2 already has 138 G of VM images

§7 said there were no Incus VMs. There are no *running* ones, but `fast` holds:
`fast/virtual-machines` **138 G**, all under project `ontrak` — templates
`ontrak_tpl-{hw-driver-device, net-dns-failure, net-static-ip-conflict, os-perf-startup,
sec-malware-persistence, sec-phishing-triage, sw-app-crash}` plus sessions, each with a
`@snapshot-clean` snapshot costing **~9 G**. That is why i2's pool sat at 75 %.

This matters for §7's recommendation: OnTrak's Windows guest already exists as an Incus VM
pattern here, so W2 on i3 becomes *rehosting an existing design onto better hardware*
rather than a from-scratch build — and the ~54 G of `snapshot-clean` snapshots on i2 are
candidates for pruning once the templates are confirmed good.

### 11.5 i2's root cannot be grown from inside the guest

D1 worked on i4 only because i4's volume group had **80 G unallocated**. i2 does not:

```
ubuntu-vg  1 PV  1 LV  VSize <46.95g  VFree 0
ubuntu-lv  <46.95g   ext4  /  46G  32.1G used  11.8G avail  70%
```

The group is fully allocated. `/` at 70 % on i2 — the estate's tightest filesystem, on its
busiest host — therefore **cannot be relieved with `lvextend`**. It needs either the
underlying virtual disk enlarged (i2 is a QEMU guest on `pm3`, which §W5 could not verify)
or data moved off `/` entirely. This is why §D5 (trim + scrub) and the `proxy` split matter
more on i2 than they look: there is no free LVM escape hatch there, unlike i4.

### 11.6 Not attempted, and why

- **Session-Redis merge (9 → 1)** and **OnTrak 3-database merge** — both land on i1, which
  was at 77 % I/O-stalled wall time throughout. Merging live databases under that pressure
  cannot be verified, and a bad cutover would be indistinguishable from the storm.
- **`christ` / `scif` / `fax` strays into Incus** — a real migration (new container,
  address, netplan pin, volume copy, cutover) per family. Not a thing to start minutes
  before the estate's only spare host comes back.
- **Splitting `proxy`** — still gated on a maintenance window, as §9 says.
- **Nothing on i3** — offline for reinstall.

---

## 12. Second pass, 2026-11-15 21:10–22:00 EST — i3 returns, zombie leaks fixed, Tix database moved

### 12.1 i3 is back and is now a real Incus host

Fresh Ubuntu 26.04.1 (`7.0.0-43-generic`), same `i3-550` **2c/4t**, **15 403 MiB** RAM,
`WDC WD5000AVDS` 465.8 G, **`/dev/kvm` present**, `/` 98 G at 8 %, and `ubuntu-vg`
463.76 G with **363.76 G unallocated**.

| Step | What was done | Measured result |
|---|---|---|
| Incus | `apt-get install incus` (+ `zfsutils-linux`, `xorriso`, `python3`, `sshpass`, `rsync`) | `incus --version` → **6.0.5**, `incusd` listening on `:8443` |
| **D4** pool | `lvcreate -l 100%FREE -n incus ubuntu-vg` → **zfs** pool `incus` on `/dev/ubuntu-vg/incus` | `zpool list` → **362 G, 663 K allocated, ONLINE**; `incus storage list` → `incus` zfs **CREATED** |
| Network | managed `incusbr0` bridge, `enp2s0` untouched | `incusbr0` **10.105.132.1/24**; `.53` still static on `enp2s0` |
| RAM guard | `zfs_arc_max=4294967296` in `/etc/modprobe.d/zfs-arc.conf` | ARC ceiling **4 GiB** of 15 403 MiB |
| Profile | preseeded `default`: root on `incus`, `eth0` on `incusbr0` | `incus profile show default` matches |

**D4 was applied differently from §4's plan, deliberately.** §4 offered a `dir` pool; this is
**zfs**, because that is what `i1` (`incus`) and `i2` (`fast`) run, and because `i2`'s
existing `ontrak` VM templates depend on `@snapshot-clean` snapshots — a `dir` pool would have
made the one host meant to carry VMs the only one that cannot snapshot them.

### 12.2 The Windows VM (W2) did not complete — recorded as failed, not as done

i2 already holds both halves of the build, so nothing was invented: the toolset at
`/root/ontrak/build/incus-windows/` and the ISOs
`26100.1742.240906-0331.ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso`
(**5 387 960 320 B**, Windows 11 Enterprise eval) and `virtio-win-0.1.285-1.iso`
(**789 645 312 B**). Tree and ISOs were copied to `/root/winbuild` on i3.

`build.sh -i <iso> 11e` **succeeded** at repacking the ISO with unattended data
(`unattended-11e.iso`, 733 787 sectors) and created the build VM `ontrak-winpack-build` — then
**failed** driving the install: `tools/click.py` → `OSError: [Errno 5] Input/output error` in
`waitbooting()` (`os.read` on the console fd), then `Error: Image not found`. The build VM is
left **STOPPED** and `output/win11e/` holds only the repacked ISO. **No Windows image exists
and no VM is serving on i3.**

The alternative — copying one of i2's **nine working Windows VMs** in project `ontrak` — is
blocked on cross-host trust, not on the VM: `incus config trust add` tokens expire on
redemption (`expired_token`), and although i3's client certificate *was* accepted on i2
(`i3-direct` now appears in `incus config trust list`), `incus remote add` still falls through
to the same expired device-authorization flow. That is the next thing to fix, and it is one
command pair, not a rebuild.

### 12.3 Zombies on i1 / i2 / i4 — root cause found, fixed with `init: true`

Restarting the parents cleared the backlog and both leaks came straight back, so §12.3 was
rewritten after the cause was measured rather than assumed. The three hosts leaked for three
different reasons, and only one of them is a restart-shaped problem:

| Host | Before | Parent, and where it lives | Cause | Fix | After |
|---|---|---|---|---|---|
| **i1** | **9** `curl` | `mineru-api` → `openmaic-mineru` in `atheniq` | container ran `Init=<nil>` | `init: true` in `docker-compose.mineru.yml`, stack recreated | **0** |
| **i4** | **2** `chrome-headless` | `node`/`concurrently` → `linkwarden-linkwarden-1` in `vault` | **PID 1 was `concurrently` itself**, and the zombie sat at `PPID 1` | `init: true` in `/usr/src/linkwarden/docker-compose.yml`, recreated | **0** |
| **i2** | **1** `epmd` | `beam.smp` — an Elixir pod in `copilotkit`'s k3s | a pod, not a container; no `kubectl`/`k3s` in `PATH` inside | none attempted | **1** |

**The measurement that made this a fix.** Inside Linkwarden's *own* PID namespace the tree read

```
PID 1  PPID 0  Ssl  node
   23  PPID 1  Zs   node      <-- the dead child had been reparented to PID 1
```

PID 1 was `concurrently`, a Node supervisor that reaps nothing. A child reparented to it is
reaped only if PID 1 calls `wait()` — so `docker restart` cleared the backlog and the leak
returned within minutes. With `init: true`, `docker-init` becomes PID 1 (`Init=true`) and the
tree now reads `1 docker-init` → `6 node` → …, so orphans are reaped as they die. Both
containers came back **healthy** (`Health=healthy`) and both hosts now measure **0**.

*A `Z` process is already dead* — no CPU, no memory, only a PID-table slot — so nothing can
"stop" it; only its parent can reap it. That is why the fix is a compose keyword and not a
kill. i2's single `epmd` zombie is the one case where the cheap remedy is restarting something
that works: it is a dead child of a live Erlang VM inside a **k3s pod**, it holds one PID slot,
and there is no `docker exec`/`kubectl` path to it from the container.

**Both edits live on the hosts, not in this repo, and that matters.** The Linkwarden file is
upstream's compose (`/usr/src/linkwarden/`, backup `docker-compose.yml.bak-init`). The MinerU
file is `/home/tutor/atheniq/services/OpenMAIC/docker-compose.mineru.yml` — a **checkout of
the `atheniq` repository** inside the `atheniq` container (backup `…mineru.yml.bak-init`), so
that `init: true` has to be committed in `atheniq` or the next pull will remove it and the
leak comes back.

### 12.4 The Tix database left host Docker — three failures, and the key that fixed it

Backed up first, then moved, then **verified against the old instance before anything else
was changed**:

| Step | Result |
|---|---|
| `pg_dump -Fc` of `ontrak_tix` and `ontrak` | **160 M** + **85 K** in `/root/*.dump` on i4; header reads `PGDMP` |
| Incus container `ontrak-tix-db`, `.68` | Created; `.68` was unreferenced in the repo and unanswered before use; pinned in its own netplan |
| Caps | `limits.memory=1GiB`, `limits.cpu=1`, `limits.memory.enforce=hard`, `limits.memory.swap=0`, `boot.autostart=true` — the host-Docker copy had **0 mem / 0 cpu** |
| Data copy | `/var/lib/docker/volumes/ontrak-tix_ontrak-tix-db/_data` → `/srv/tixdb`, **4.9 G**, owner `70:70` |
| Postgres inside the new container | **Failed three ways before it ran** — below |

**The three failures, in the order they were measured. The third was the answer.**

1. `runc create failed: … open sysctl net.ipv4.ip_unprivileged_port_start: permission denied` —
   the port publish. `security.privileged=true` did not help, and **`raw.lxc` is not settable on
   this build at all** (`cannot set 'lxc.mount.auto' … unknown key`, with or without spaces
   around `=`), so the documented workaround is not available here.
2. Removing the publish (`--network host`) moved the wall rather than clearing it:
   `failed to mount /tmp/containerd-mount…: fstype overlay … permission denied` — the inner
   daemon could not mount its own container root at all.
3. **`security.nesting=true` is the key that was missing.** With it set, the mounts succeed and
   `postgres:16-alpine` starts: `database system was shut down at 2026-11-15 21:31:22 UTC` —
   *that timestamp is the stop taken for the copy*, which is the proof the new instance is
   running the copied data — then `database system is ready to accept connections`.

`vault` on the same host runs Docker with no `security.*` keys at all, which is why the missing
key was not obvious from the host's own precedent: the difference is that `vault`'s stacks
publish no ports and mount no overlay of their own.

**Cut over, with nothing deleted.** The new instance reported `Ticket = 5 000 479`, exactly the
old instance's `count(*)`, so `ontrak-tix-db-1` was stopped and left in place (`Exited (0)`, its
volume intact) and `.68:5432` now serves the database. A `pg_dump -Fc` of both databases is on
`/root` on i4 as the second copy.

**One cap was wrong and was corrected.** The container's memory was reading **256 MiB**, which
is under the 128 MiB `shared_buffers` default plus connections; it is **1 GiB**, hard-enforced,
with `swap=0` and `limits.cpu=1` — against the old host-Docker copy's **0 mem / 0 cpu**.

**The counts differ from §11.3, and the difference is an estimator — not a write.** The audit
recorded **5 001 081** `Ticket`; this pass measured **5 000 479**. Asking for both in one
statement settles it:

```
estimate=5001081   (pg_stat_user_tables.n_live_tup)   exact=5000479
```

So §11.3's figure was the **planner's estimate**, and nothing has been inserting or deleting.
`pg_stat_activity` holds no external client (this session and slack backends only), the count
is stable across samples 30 s apart, and `pg_stat_database` does not move. **The alarm this
section first raised — "something is actively writing to this database" — was wrong, and is
withdrawn.** The exact count, and the rule that a row count in this page is quoted from
`count(*)` and never from `n_live_tup`, are the record.

### 12.5 What this pass did not do

- **No container was moved to i3 and no estate check names it.** `check-container-addresses.py`,
  `check-estate-inventory.py`, `check-host-disk.py`, `check-container-limits.py`,
  `check-stuck-processes.py`, `check-mount-stacks.py`, `check-address-latency.py`,
  `check-mail-traffic.py`, `trust-estate-hosts.py` and `setup-sshpass-lan.sh` are still
  three-host tables. Widening them changes what the scheduled run requires and what the
  fixtures in `scripts/tests/` assert, so it is a change of its own rather than a silent
  half-edit that would make four checks disagree about how many hosts exist.
- **`monarch` on i1 is still running** (§11.2). The decision remains the owner's.
- **i2's 138 G of `ontrak` VM images and 53 % fragmentation** are untouched.
- **The Tix backup is unverified beyond its header.** It has not been test-restored — but the
  new container starting from the copied data directory, with the old instance's shutdown
  timestamp in its log and a matching `count(*)`, is a stronger proof than a dump check would
  have been. The dump itself remains a second copy, unexercised.

---

## 13. Third pass — the Tix cap under load, and a check that counts zombies

### 13.1 The 1 GiB cap on `ontrak-tix-db` holds under concurrent load

§12.4 set the memory cap from arithmetic, not measurement: 1 GiB, hard-enforced, `swap=0`,
`limits.cpu=1`, against a host-Docker copy that had **no cap at all**. A cap that is too small
fails as an OOM kill, and this database has no consumer in production (i1's `ontrak` app points
at its own `tix-db` service), so nothing upstream would notice it dying.

Measured on the live instance at `.68`:

| Probe | Result |
|---|---|
| `count(*)` on `Ticket`, single client | **0.40–0.51 s** across samples (5 000 479 rows, seq scan) |
| `count(*)` × 36 queries, 12 concurrent clients | **14.8 s** wall, all 36 returned |
| `memory.peak` during the run | **738 926 592 B** of 1 073 741 824 (69 %) |
| `memory.events` | `oom 0`, `oom_kill 0` |
| Container after the run | `Up`, `count(*)` unchanged |

**The cap is right, with 31 % headroom at peak.** 738 MiB is the number a future cap change has
to start from: a 512 MiB cap would have killed it during this run, and 1 GiB is the smallest cap
that survives twelve concurrent full scans. Note what the probe does *not* say — it is twelve
readers of one table, not the application's write mix, because there is no consumer to drive one.

### 13.2 `check-zombie-processes.py`, and why it names four hosts

§12.3 fixed both leaks with `init: true` — a keyword in two compose files that live on two
hosts. Nothing in the estate would notice it being reverted, and the next supervisor written the
same way leaks the same way. `scripts/check-zombie-processes.py` counts the symptom on every
host: **warn at 3**, **fail at 25**, unreadable host is exit 2 and never a pass — the same
contract as the other checks, publishing `innotel_estate_check_host_zombie_processes{host=…}`.

Live run, on the estate as it stands with i2 still up:

```
check-zombie-processes: 0 failure(s), 0 warning(s)
  zombies: i1 0, i2 1, i3 0, i4 0
```

i2's `1` is the deliberate `epmd` from §12.3, and the warning line is deliberately above it so a
normal host stays quiet. This is the **one check whose host table is four-wide from the start** —
§12.5's rule was that widening the existing three-host tables is a change of its own, and that
holds; a new check does not inherit that debt, and leaving i3 out of it would have meant a check
that cannot see the host the estate is being moved onto.

### 13.3 The `atheniq` commit in §12.3 cannot be made, and that is the finding

The MinerU edit is real and live (`Init=true`, container healthy, i1 at 0 zombies), but it
**cannot be committed in `atheniq`**: `services/` is **ignored** by that repository.

```
$ git ls-files --error-unmatch services/OpenMAIC/docker-compose.mineru.yml
error: pathspec … did not match any file(s) known to git
$ git check-ignore -v services/OpenMAIC/docker-compose.mineru.yml
.gitignore:9:services/	services/OpenMAIC/docker-compose.mineru.yml
$ git status --porcelain | head -3
 M contrib/tutor-atheniq-hosts/atheniqhosts/plugin.py
 M deploy/compose/atheniq-sso.yml
```

So the deployed copy under `/home/tutor/atheniq/services/` **is** the source of truth for that
file, and a `git pull` cannot remove `init: true` (it was never tracked to be restored). The
backup `…mineru.yml.bak-init` stays beside it. §12.3's warning that "the next pull will remove
it" was wrong in that specific respect and is corrected here: the pull will not touch an ignored
path, but nothing versions this fix either — a rebuilt container or a re-cloned checkout loses
it silently. Recording it in this page is the only durable copy.

---

## 14. i2 is going offline — the estate moves onto i3

**Context.** i2 is to be reinstalled, so every container on it has to be somewhere else first.
That is a different job from §8's scenarios: not a rebalance for headroom, a **displacement**
with a deadline, onto the one host the estate had written off in §11 (2c/4t, 15.4 GiB, a
5 400 rpm `WD5000AVDS`). This section records what the move required, what has moved, and what
it costs while it lasts.

### 14.1 Three things had to exist on i3 before a single container could move

None of i2's eight containers could be copied to i3 as it stood. Each blocker was structural,
not an error message:

| Missing on i3 | Why it blocked the move | What was done |
|---|---|---|
| **A LAN bridge** | Every i2 container is `nictype: bridged, parent: br0` and pins its own LAN address **inside** the container (`.58`, `.70`, …). i3's `enp2s0` held `.53` directly and its only bridge was `incusbr0` (**10.105.132.0/24**, NAT) — an imported container would have come up on NAT and lost its address. | `br0` created as a netplan bridge taking over `enp2s0`, carrying `.53`, **same MAC** (`10:78:d2:9a:0d:52`) so the LAN and the router see no change. Applied with an **armed rollback** (restore the old netplan and re-apply after 180 s unless disarmed); disarmed after the gateway answered. |
| **i2's profiles** | Every container references `docker` (and `default`); the copy aborted with `Requested profile "docker" doesn't exist`. | `default`, `docker`, `large`, `medium`, `small` exported from i2, `pool: fast` rewritten to `pool: incus`, `used_by:` stripped, applied on i3. `default` now matches i2's: root on `incus`, `eth0` on **`br0`**. |
| **Trust between the hosts** | `incus copy` across hosts needs the target to accept the source's client certificate. §12.2 recorded the trust attempt as blocked on `expired_token`. | The certificate *was* already trusted (`i3-direct`); the CLI was falling through to device authorization. `incus remote add … --protocol incus --auth-type=tls` succeeds — the missing piece was the explicit auth type, not the certificate. i3 now reads `i2src:` directly. |

**This corrects §12.2.** The Windows VM on i2's `ontrak` project is no longer blocked on trust;
what remains blocked is the *build* (§12.2's `click.py` failure). The trust fix is one command
pair, exactly as §12.2 predicted, and it was the `--auth-type=tls` flag.

### 14.2 The copy path, and the failure that shaped it

There is **no CRIU on i3**, so a running container cannot be live-migrated:

```
Error: Failed instance creation: Unable to perform live container migration.
CRIU isn't installed on the target server.
```

So the path is **stop the source → `incus copy i2src:<name> <name> --storage incus` → set the
cap → start the target**. Transfer measured at **36–40 MB/s**, which is the constraint that sets
the timetable: 65 G of rootfs is ~30 minutes of copying, inside which each service is down for
its own copy, not for the batch.

**Two of my own mistakes on the first container, recorded because they cost real downtime.**
`subscribe` was stopped twice with nothing to start in its place: first because the copy failed on
the missing `docker` profile *after* my script had already stopped the source, and then because I
guarded the copy as `if incus copy … | tail -3` — a pipeline whose exit status is `tail`'s, so the
failure branch never ran and the source was stopped again. Both times the source was restarted
and verified on `.58` (3/3 packets, ARP `REACHABLE` from i1) before anything else was touched. The
working script now checks the copy's own status, restarts the source on failure, and copies one
container per iteration.

### 14.3 What has moved

| Container | Address | Cap before → after | State on i3 | Verified |
|---|---|---|---|---|
| `subscribe` | `.58` | 384 MiB → 384 MiB | RUNNING | ping 3/3 from i1, ARP `REACHABLE` |
| `pi` | `.70` | 384 MiB → 384 MiB | RUNNING | reported `.70` on `eth0`; 1.35 GB transferred |

Both sources are **stopped on i2, not deleted** — nothing is removed until the reinstall, and the
old rootfs stays as the fallback copy.

*One host-local oddity, written down so it is not mistaken for a fault later:* i3's **host** cannot
ping `.58` (`ip neigh` shows `FAILED`) while **i1** pings it 3/3 with a REACHABLE ARP entry, and
the container answers on the bridge. Container-to-container and LAN-to-container traffic are
fine; only the host's own path to that address is not. It does not affect the estate checks,
which reach hosts by their own addresses and containers from the edge.

### 14.4 What is left, in the order it should go

| Container | Rootfs | Cap today | Note |
|---|---|---|---|
| `distro` | 2.5 G | 512 MiB | copying now; carries a snapshot (`pre-privileged-20261020`) |
| `magnate` | 3.8 G | 512 MiB | |
| `copilotkit` | 2.7 G | **10 GiB** | cap is 75 % of i3's RAM and its measured use is 1.58 GiB — **right-sized to 4 GiB** in the move |
| `onyx` | 8.2 G | 768 MiB | NFS client; §11's mount-stack history is about this container |
| `signara` | 9.7 G | 1 GiB | |
| `proxy` | **36.1 G** | 7 424 MiB | **last, and only with a window** — see 14.5 |

**`proxy` is not a container, it is the estate's edge**: NPM (every public name), Authentik,
Technitium (the authoritative DNS), Vault's door, Prometheus, Grafana and six `oauth2-proxy`
sidecars. It keeps `.71`, and the router's port-forwards still point at `.71`, so the public path
is unchanged once it is up — but while it is stopped, **every public name and the estate's own
DNS are down together**, and a failed copy leaves them down until the source is restarted. Its
4.72 GiB of live memory also makes it the one container whose copy cannot be verified by a ping
alone: Authentik's database has to be consistent, which a stop-before-copy gives and a live copy
would not.

### 14.5 What this costs while it lasts

- **The edge moves to a 5 400 rpm disk.** §11's F1 finding is exactly this: `proxy` is the
  estate's write-heavy container (Authentik, NPM's MySQL, Prometheus) and i3 is the host chosen
  in §4 for *bulk* storage. For the duration of the reinstall that trade is accepted knowingly,
  because the alternative is no edge at all. **It is not the final placement** — once i2 is back,
  `proxy` belongs on NVMe (i4) or back on i2, and this section is the record that its month on
  i3 was displacement, not a decision.
- **i3's RAM becomes the constraint.** Caps on the moved set sum to ~14.4 GiB of 15 403 MiB,
  which is overcommit rather than reservation — i2's actual working set was ~8.5 GiB for the same
  eight containers. `copilotkit`'s 10 GiB cap was the one that had to shrink, and it was measured
  at 1.58 GiB before being set to 4 GiB.
- **i2's pool must not be touched while its containers are still the fallback.** `fast` is at
  70 % with 88.3 G free; the stopped rootfs copies are the only rollback path until the reinstall
  wipes them. Nothing on i2 is deleted as part of this move.
