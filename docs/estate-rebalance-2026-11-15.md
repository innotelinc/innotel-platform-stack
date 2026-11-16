# Estate rebalance — inventory and options, i1 / i2 / i3 / i4

**Status: partially applied 2026-11-15 — see §11 (15:45–16:05 EST), §12 (21:10–22:00 EST), §13 (the Tix cap under load, and a check that counts zombies) §14 (i2 → i3 displacement), §18 (i3 back — and no check could reach it) and §19 (**`i2` came back the same evening — recovered and parked**, not decommissioned).** The inventory below is
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
| `distro` | `.61` | 512 MiB → 512 MiB | RUNNING | ping 3/3 from i1 |
| `magnate` | `.57` | 512 MiB → 512 MiB | RUNNING | ping 3/3 from i1 |
| `copilotkit` | `.114` | **10 GiB → 4 GiB** | RUNNING | ping 3/3 from i1 |
| `signara` | `.44` | 1 GiB → 1 GiB | RUNNING | ping 3/3 from i1 |
| `onyx` | `.60` | 768 MiB → 768 MiB | **did not move, left stopped** | imported on `i3`, not started there: it mounts a **USB block device** — see 14.3.2 |

Every source is **stopped on i2, not deleted** — nothing is removed until the reinstall, and the
old rootfs stays as the fallback copy. Measured transfer rate across the six: **36–40 MB/s**
(1.35 GB took 36 s; 36.1 G of `proxy` is therefore ~17 min of copying, before its boot).

#### 14.3.1 Two containers did not move on the first attempt, for reasons worth keeping

**`onyx` — a device on a storage pool the target did not have.** The copy aborted before creating
anything:

```
Failed add validation for device "onyx-data": Failed to get storage pool "tank": Storage pool not found
```

The container mounts a custom volume (`tank/onyx-data`) at `/mnt/onyx`, and **`tank` exists on i2
only** (`default`, `fast`, `tank` there; `incus` here). The volume is small — i2's `tank` reports
**58.04 MiB used of 79.16 GiB** — so the cost was not the data but the name. Fixed by creating a
matching pool on i3 backed by a dataset of the existing zpool (`incus storage create tank zfs
source=incus/tank`), which keeps the device definition valid without a second block device, then
`incus storage volume copy i2src:tank/onyx-data tank/onyx-data` (**VOLUME_OK**).

**The instance copy then failed a second time, and the reason was not the pool.** The zfs send
died at **6.55 GB** with `Failed migration on target … zfs send failed: signal: broken pipe`.
Two attempts, same shape, so `onyx` moved to the tar path instead. **The obvious version of that
path is a trap on this host:** `incus export` writes the archive where you point it, and i2's `/`
had **6.4 G free against an 8.2 G instance** — the export reached 5.5 G and was heading for a full
root filesystem *on the host that still runs the edge*. The archive was aborted and removed. What
worked is streaming it instead of storing it: `incus export onyx -` writes to stdout (documented
in `incus export --help`), piped over ssh into `/root/onyx.tar` **on i3**, then `incus import
… --storage incus`. Nothing is written on i2 at all, and the archive lands on the host that has
the room (77 G). Import took the full 8.2 G and reported `IMPORT_OK`.

*The `incusd` unit that `systemctl is-active` reports as `inactive` is `incusd.service`, which
does not exist on this build; the daemon is `incus.service` and was active throughout. That
misread cost a detour and is recorded so nobody repeats it. Likewise `pkill -f "incus export"`
matches the shell that is running it — the bracketed form (`incus expor[t] onyx`) is what actually
kills the export.*

#### 14.3.2 `onyx` did not move, and I could not make it start on i3

**Recorded as not done.** The import succeeded, the volume is on `i3`, and the container will not
run there:

```
incus start onyx      ->  rc=0, and `incus list` still reports STOPPED
incus info --show-log ->  Log: (empty)
journalctl -u incus   ->  -- No entries --
```

Every probe agrees the start *operation* completes (`status: Running`, `err: ""`) while the
instance is stopped — so the container is exiting immediately and writing nothing to its console.
What was ruled out: the missing storage pool (created, `COPY_OK`), the absent `/dev/sdc` behind its
optional `main-pool-usb` device (**device removed — still stopped**), and the `--console` start,
which reported `Error: inappropriate ioctl for device` and **briefly showed the instance as
RUNNING**, which is why an earlier draft of this section claimed it had moved. It had not: forty
seconds later it was `STOPPED`, and every start since then never leaves `STOPPED` at all.

**The reason, as the owner gave it: `onyx` was connected to a USB device.** Its config carries
`main-pool-usb` — a `unix-block` device mapping host **`/dev/sdc`** into the container. Measured on
both hosts, and the measurement is the opposite of what this section first assumed:

```
i3: brw-rw---- 1 root disk 8, 32 Nov 15 21:10 /dev/sdc      <- present
i2: ls: cannot access '/dev/sdc': No such file or directory  <- absent
```

So the device is *optional in practice*: `required: "false"` on the mapping, which is why `onyx`
runs on `i2` today without it and why the copy's failure to start on `i3` was **not** the missing
disk. The owner's instruction is nonetheless to **leave the copy stopped**, and that is what is
done — the container is bound to a USB device and is not to be brought up detached from it without
a decision about what that disk is for. **The start failure itself is therefore still unexplained**,
and this section says so rather than inventing a cause to close it.

*I removed `main-pool-usb` while testing whether the device was the blocker. That was the right
test and the wrong lasting state: the device has been **restored** on the i3 copy
(`unix-block, source=/dev/sdc, path=/dev/sdc, required=false`), so the instance definition still
describes what `onyx` actually is.*

The instance and its copied volume are **left in place, stopped, on `i3`** (rootfs 10.6 G in
`incus/containers/onyx`) — the evidence for the move once the USB disk goes with it, not a rollback
to be deleted. **`onyx` still runs on `i2`**, at `.60`, where its disk is. The honest count for this
pass is **six of eight containers moved**: `onyx` waits for its hardware, `proxy` for a window.

**`signara` — a NIC that lived in a profile and a netplan that needs udev.** It came up with **no
LAN address at all**, only its docker bridges:

```
signara,RUNNING,"172.19.0.1 (br-b48b1f1033a0)
```

Its `eth0` is defined nowhere in its own config — it comes from profile `default` (its profiles are
`default`, `docker`, `medium`) — and the copy did not reproduce it: `incus config show` listed
`root` and nothing else, and inside the container `ip -br link` showed only the four docker veths.
Adding it explicitly (`incus config device add signara eth0 nic nictype=bridged parent=br0`)
created the interface but **still no address**, because `netplan apply` inside a container aborts
before configuring anything:

```
subprocess.CalledProcessError: Command '['udevadm', 'control', '--reload']' returned non-zero exit status 1
```

There is no udev in the container to reload. `netplan generate` + `systemctl restart
systemd-networkd` applies the same config without the udev step, and `.44` came up (`192.168.1.44/24
on eth0`, 3/3 from i1). **The lesson generalises: on this estate a container's NIC may live in a
profile rather than its config, and `netplan apply` is not usable inside these containers** — the
two-host pattern that worked for six containers does not cover either case.

*One host-local oddity, written down so it is not mistaken for a fault later:* i3's **host** cannot
ping `.58` (`ip neigh` shows `FAILED`) while **i1** pings it 3/3 with a REACHABLE ARP entry, and
the container answers on the bridge. Container-to-container and LAN-to-container traffic are
fine; only the host's own path to that address is not. It does not affect the estate checks,
which reach hosts by their own addresses and containers from the edge.

### 14.4 What is left, in the order it should go

| Container | Rootfs | Cap today | Note |
|---|---|---|---|
| `onyx` | 8.2 G | 768 MiB | **left stopped on `i3`** — it mounts the USB disk `/dev/sdc`, which lives on `i2` (14.3.2); still serving from `i2` |
| `proxy` | **36.1 G** | 7 424 MiB | **not started** — see 14.5 |

**i2 runs `proxy` and `onyx`.** The other six containers' rootfs are `STOPPED` copies kept as the
rollback path, and nothing on i2 has been deleted.

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

---

## 15. The edge to i4's NVMe — executed 2026-11-15, 27 minutes dark

**Moved on 2026-11-15.** `proxy` has run on `i4` since 18:57 EDT. The window it cost was the copy,
not the start — **18:30 → 18:57**, 27 minutes in which every public name and the estate's DNS were
dark together. The preflight in 15.1 was right about the three bind mounts and the two config
settings, and the 15.2 deficit was paid by moving `vault` and `sage` to `i1` first, measured rather
than estimated.

### 15.1 What actually has to travel

`proxy` is not one rootfs. Its config, measured on i2:

| Piece | Where it is now | Size | What breaks if it is missed |
|---|---|---|---|
| `root` | pool `fast`, declared **`size: 65GiB`** | **36.1 G used** | i4 has no `fast` pool — the device must be repointed to `tank` or the copy validates and then fails |
| `eth0` | `nictype: bridged`, **`parent: br0`**, pinned MAC | — | i4's LAN bridge is **`br-lan`**, not `br0`; the NIC must be repointed or the edge comes up dark on `.71` |
| `raw-mount` → `/data` | **host path** `/usr/src/proxy/data` | **140 M** | NPM's 209 `proxy_host` confs and its data live here; an empty `/data` is a running edge with no doors |
| `site` → `/usr/src/innotel-site` | host path | 92 K | the site deploy targets |
| `sitedeploy` → `/usr/src/innotel-deploy` | host path | 20 K | the deploy door |

Those three host paths are **bind mounts, not storage volumes** — `incus copy` carries the device
*definitions*, not the data behind them, so they have to be `rsync`ed to i4 separately or the copy
starts with empty directories.

Good news, verified on i4: it already has the `default` profile with **`eth0` on `br-lan`** and
`root` on `tank`, and the `docker` profile with `security.nesting` — the two profiles `proxy`
references. The copy's config surgery is therefore two settings, not a rebuild.

### 15.2 The deficit, and how it was closed

| | i4 (measured 2026-11-15) | `proxy` |
|---|---|---|
| Memory total | **7 341 MiB** | cap 7 424 MiB |
| Memory in use | 4 220 MiB | **live 4.84 GiB** |
| Memory available | **3 121 MiB** | needs 4.84 GiB |
| Swap | 4 G file, **1.2 G already used** | — |
| `/` free | 132 G | 36.1 G |

**3.1 GiB available against a 4.84 GiB working set is a ~1.7 GiB deficit**, and the only place for
it is a 4 G swap file that is already a third consumed. i4 already runs `vault` (1.14 GiB), `sage`
(1.01 GiB), `terminal` (489 MiB), `telno`, `telno-turn`, `vpn` and `ontrak-tix-db` (284 MiB).

**The deficit closed, and the numbers are the record.** `vault` (1 166 MiB live, 9.3 G rootfs) and
`sage` (1 034 MiB, 3.7 G) were stopped on i4 and `free -m` moved from **3 104 MiB available to
4 701 MiB** in the same minute — 1 597 MiB back, which covers `proxy`'s 4 977 MiB working set once
its own page cache (the 37 G of sites and certs) is counted as reclaimable rather than resident.
Both were shut down in place, copied to `i1` over a TLS remote, and started there:

| Step | What it took | Evidence |
|---|---|---|
| trust | `i1`'s client certificate added to i4's trust store, then `incus remote add i4src … --auth-type=tls --accept-certificate` on `i1` | the OIDC remote cannot authenticate without a device flow, so it cannot carry a copy |
| copy | `incus copy i4src:vault vault --storage incus`, then the same for `sage` | 9.3 G + 3.7 G; `i1` has 50 G free |
| fix | `vault`'s `eth0` repointed `br-lan` → `br0`, `root` pool → `incus`; `sage` needs neither (its NIC and root come from the shared profiles) | `default` on `i1` is `eth0 br0` + `root incus` |
| start | both RUNNING on `i1`, `systemctl is-system-running` = `running` | `.73` and `.94` answered 3/3 pings and `vault` answered `HTTP 200` at 18:28 |
| keep the rollback | `incus config set vault boot.autostart=false`, same for `sage`, on `i4` | without it a reboot would have two hosts claiming `.73` and `.94` |

### 15.3 The window, and the rollback at every step

Whichever host it goes to, the sequence is the same and the source is never deleted:

| # | Step | Rollback if it fails |
|---|---|---|
| 0 | **Preflight, no outage:** `rsync` the three host paths to the target, compare sizes; capture `incus config show proxy` to `/root/proxy-config-before.txt`; confirm the target's `default`/`docker` profiles and bridge | nothing to roll back — the edge never stopped |
| 1 | Stop `proxy` on i2 → **every public name and the estate's DNS go dark together** | `incus start proxy` on i2, ~30 s |
| 2 | Copy: `incus copy i2src:proxy proxy --storage <pool>` (measured **36–40 MB/s**, so **~17 min** for 36.1 G) | `incus start proxy` on i2; delete the partial copy |
| 3 | Fix the two settings — `root` pool → the target's pool, `eth0 parent` → the target's bridge — then start | stop the copy; `incus start proxy` on i2 |
| 4 | Verify in this order: `ping .71` → NPM admin on `:81` → **Technitium answers a zone query** → Authentik discovery document → one public name end to end | stop the copy; `incus start proxy` on i2 |
| 5 | Leave the i2 copy **stopped for a week**, not deleted | — |

**Rollback is one command and it is the same at every step:** `incus start proxy` on i2 restores the
edge with the address, the config and the data it had a minute earlier, because nothing on i2 was
deleted, moved or edited. The `.71` address and the router's port-forwards never change, so DNS and
the public doors come back as soon as the container runs — the zone's own answers depend on the
container, not on its host.

**What the copy actually cost:** 37 G at **34 MiB/s** (11 MiB/s in the middle stretch), so the
window was 27 minutes against the 17 the preflight predicted from the earlier 36–40 MB/s reading.
The estimate was optimistic because it came from fresh containers, not from a 37 G zfs rootfs
carrying NPM's `letsencrypt` tree. The three host paths were `tar`-streamed to `i4` twice: once
before the stop to prove the copy path, and again after it so `database.sqlite` reflects a stopped
NPM rather than a racing one.

**Verified after the start, in the order §15.3 asks for:** `ping .71` 3/3 from `i4` itself; NPM's
containers up inside the container (`innotel-site`, `metrics-node-exporter`, `access-denied`,
`gateway-sso` healthy, `orion-locker-sso`); `:80` and `:443` listening on both stacks; and from
`i1`, `--resolve innotel.us:443:192.168.1.71` → **200**, `www.innotel.us` → **301**, plain `:80` →
**301**. The `000` seen twice on the way was curl sending the IP as SNI with no `--resolve`, not a
dark edge — worth writing down because it reads exactly like one.

**Rollback was armed on `i2` for exactly one day, and it is gone.** `i2` kept the stopped original
with its data untouched, so `incus start proxy` there restored the edge on the same address — and
nothing on the router ever moved. `i2` was **decommissioned on 2026-11-16** (§19) and the stopped
copy went with it, so that sentence is now history rather than a plan. Read §19 before anything
here is used as a recovery procedure.

## 16. The estate checks, installed on the edge for the first time

The unit had never run on a schedule anywhere. `i4` carried 34 stale files in
`/opt/innotel/estate-checks` from a manual run, there was no
`/etc/systemd/system/estate-checks.service` on any of the four hosts, and no timer. With the edge
now on `i4`, the merged checks (37 files, commit `9a6f7d3`) were installed there with
`scripts/install-estate-checks.sh`, the timer enabled, and one run forced: `Result=success`,
`ExecMainStatus=1`, 2 min 50 s wall, **35 `.prom` files** written.

Two things had to be true for the checks to answer anything, and neither was:

| Failure in the first run | Why | What fixed it |
|---|---|---|
| `installed_checks`, `unit_drift`: *“/usr/src/projects/complete/ips has no origin/main”* | both default to the estate's checkout path; the edge's checkout is `/root/platform-stack` | a symlink `/usr/src/projects/complete/ips → /root/platform-stack` on the edge — a host fact, not a repo change |
| every ssh-reading check: *“Permission denied (publickey,password)”* | `estate_check.ssh` runs `BatchMode=yes`, so the edge's key has to be in each host's `authorized_keys` | the edge's `id_ed25519.pub` appended (deduplicated) to `i1`, `i2` and `i3`; key-only ssh now returns `hostname` from all three |

**What they say now:** `installed_checks` **1** (clean), `host_zombies` **1**, `unit_drift` **0**
with **2** findings — `swap-ceiling-check.service` and `wifi-powersave-off.service` differ between
the hosts and `systemd/` (`55b5bd7` vs `878395d`, `39ef6ec` vs `20a0a79`). That is the check working,
and neither unit is one this change touched.

**15 of 35 checks are red, and that is the estate's first honest verdict rather than a new failure.**
The unit has never run, and the reds include `container_address` (the address table has just been
moved to match tonight's placements — §15 and §17), `mail_traffic`, `dmarc_alignment`,
`edge_vhost_pins`, `edge_vhost_targets`, `edge_sso_door`, `npm_config_drift` (still missing
`NPM_ADMIN_EMAIL`), `media_doors`, `mount_stacks`, `orion_signin`, `alert_delivery`,
`authentik_privilege`, `mail_relay`, and `address_latency` — which dials every address in the table,
including `.60`, and `onyx` is deliberately stopped (§17).

**The scrape path needed the checks to run *inside* the edge, and that is where they run now.** No
host runs node-exporter on the metal, and `metrics-node-exporter` is a container inside `proxy` that
mounts its textfile directory **from its own filesystem** — a file written to
`/var/lib/node_exporter/textfile` on `i4`'s metal never appears inside the container (proved by
writing one and failing to find it). So those 35 host-written files are still read by nobody; the
host copy is a spare, not the publisher. The container runs its own copy of the same unit and the
same checks (`/opt/innotel/estate-checks`) on its own timer, and Prometheus scrapes
`node-exporter:9100` there: **32 series before, 33 after**, with `host_zombies` arriving as the new
one. An earlier version of this paragraph said the checks "publish and nothing consumes them" — that
was true of the host copies only, and it is why the checks were moved to run where the scrape
happens.

## 17. `onyx` — on `i3`, stopped, and staying stopped

`onyx` is off `i2` and off the estate on purpose. It mounts the USB block device (`main-pool-usb` →
`/dev/sdc`), that device is attached to `i3` (`/dev/sdc`, `8,32`), and `i2` has no `/dev/sdc` at
all — so the instance that was running there was running without its device. Both are stopped now:

| Host | State | rootfs dataset | data volume |
|---|---|---|---|
| `i2` | **STOPPED** (was RUNNING) | `fast/containers/onyx`, 8.23 G | `fast/tank/custom/default_onyx-data`, 56.9 M |
| `i3` | **STOPPED** — the placement | `incus/containers/onyx`, 10.6 G, created today | `incus/tank/custom/default_onyx-data`, 56.3 M |

`i3`'s copy came from `incus export onyx -` streamed over ssh today, so it is the newer of the two.
The 10.6 G against 8.23 G is `used` on two different zfs pools, not a difference in file count: a
stopped zfs dataset is unmounted, so a byte-for-byte comparison needs both mounted and that was not
done. `i2` was decommissioned on 2026-11-16 (§19), so the copy this was waiting to return to no
longer exists: **`i3` holds the only `onyx`**, still stopped.

> **One correction to "stays stopped": it did not stay stopped on its own.** `onyx`'s imported
> config carried `boot.autostart=true`, so `i3`'s first clean boot after the move started it. §18 has
> the measurement; `boot.autostart` is `false` now, which is what makes "stays stopped" true across a
> reboot rather than only until the next one.

## 18. `i3` is back — seven containers, one wrong start, and no check could reach it

`i3` was powered off for the physical move and answered again at **01:21 UTC on 2026-11-16**
(`uptime`: 3 minutes at 01:24). Six of its seven containers started themselves and kept their
addresses; `copilotkit` did not start; `onyx` started and should not have.

| Container | After the clean boot | Address | What was checked |
|---|---|---|---|
| `distro` | RUNNING | `.61` | ping from `i3`, `i4` |
| `magnate` | RUNNING | `.57` | ping `2/2` from `i1` |
| `pi` | RUNNING | `.70` | ping from `i3`, `i4` |
| `signara` | RUNNING | `.44` | `signara-alertmanager-1` up, `9093/-/healthy` → `OK` |
| `subscribe` | RUNNING | `.58` | ping from `i3`, `i4` |
| `copilotkit` | **STOPPED** — started by hand | `.114` | 1.59 GiB, `node` + 3 `postgres`, 8 listening sockets |
| `onyx` | **started, stopped again** | `.60` | §18.1 |
| `ontrak-winpack-build` | STOPPED | — | the Windows build VM, stopped on purpose (§12.2) |

**A ping sweep five minutes after the boot reported 0/3 against all six.** That was the bridge while
the containers were still bringing `eth0` up, not a network fault: seconds later `magnate` answered
2/2 from `i1` and all six answered from `i3` and `i4`. Worth writing down because "0/3 on every
address" reads exactly like the failure §14 spends its length avoiding.

### 18.1 `onyx` started because the import carried `boot.autostart`

§17 said `onyx` stays stopped. It did — until the host rebooted. The imported config carries
`boot.autostart=true`, so the clean boot started a container whose device is **not on this host**:
`ls -l /dev/sdc` on `i3` returns nothing, because the USB disk did not move with the machine. It ran
with only its `tank` volume — `incus/tank/custom/default_onyx-data`, 40 G with **57 M** used against
the 56.3 M §17 measured — and its `onyx-data.img` is still 21 474 836 480 bytes dated **Oct 19
22:50**, with the volume's directories carrying their October dates, so nothing was written while it
was up. It is stopped again and `boot.autostart` is now **`false`**, which is the part §17 was
missing: the placement stayed stopped by hand, not by policy, and one reboot showed the difference.

`incus stop onyx` also outlived the 90-second ssh call that issued it — the call timed out and the
stop completed anyway. A timeout there is not a failed stop.

**`copilotkit` did not start on its own**, and `boot.autostart` is unset on it (which Incus treats as
true), so why it stayed down is not established. Started by hand it is healthy. It is the one
container whose boot behaviour differed from the other six.

### 18.2 No check could read `i3`, and the reason was one line of ssh config

The edge asks every host for its inventory with key-only ssh, through a block that
`scripts/trust-estate-hosts.py` writes into `/root/.ssh/config`:

```
Host 192.168.1.51 192.168.1.52 192.168.1.54
  IdentityFile /root/.ssh/container-address
```

**`.53` was not in that list**, and i3 was rebuilt on 2026-11-15, so the key was not authorized there
either: `authorized_keys` on i3 held one line (`codebuff@i4`) and ssh from the edge answered
`Permission denied (publickey,password)`. Both halves are fixed — the key is in i3's
`authorized_keys` (2 lines now) and `.53` is in the host block — after which
`ssh -o BatchMode=yes root@192.168.1.53 hostname` answers `i3`.

**This is why `unit_drift`'s host list has to be read carefully.** It printed *"0 finding(s) of 15
unit(s) compared (this host, i1, i2, i3)"* while `i2` was unreachable and named in the same run — so
the parenthesis is the hosts it **tried**, not the ones that answered. Adding `i3` to the unit's host
list makes the check *ask* i3; the ssh block is what makes an answer arrive.

### 18.3 The verdict with all four hosts answering

`alert_delivery` is **green** (`0 failure(s)`, `last_status 1`) — the probe runs `amtool` inside
`signara-alertmanager-1` where that container now lives — and `installed_checks` is green again
(*"every installed check and both unit files match origin/main"*).

**12 checks are red.** §16 recorded 15, but the two runs did not see the same estate, so the count
moved for several reasons at once:

| Check | What it says now | Cause |
|---|---|---|
| `container_address`, `container_limits`, `estate_inventory`, `host_disk`, `host_zombies`, `mount_stack`, `stuck_processes`, `unit_drift` | *"could not read a host: i2 (root@192.168.1.52) … exit status 255"* | `i2` is off for the reinstall — `ssh: connect to host 192.168.1.52 port 22: No route to host` |
| `edge_vhost_targets` | 101 findings: 0 drift, **25 dead targets** | the vhosts pointing into `i2`'s Docker stacks, plus `.56` (`monarch`, stopped) and `.60` (`onyx`, stopped) |
| `address_latency` | **3 failures of 31 dialled**: `.52`, `.56`, `.60` | `i2` off, `monarch` stopped, `onyx` stopped on purpose |
| `media_doors` | 2 failures: `media.innotel.us` and `iptv.innotel.us` → **502** | `monarch` (`.56`) is stopped |
| `mail_traffic` | *159 message(s) accepted in the last 24 h and not one delivery completed* | real, and not caused by the move |

`address_latency` going from **32 dialled addresses to 31** is the `monitor` row leaving the table:
`.88` was one of its four failures, and SigNoz no longer exists to answer it.

**Do not read `Result=success ExecMainStatus=0` as "everything passed".** Every `ExecStart=` in the
unit is `-`-prefixed so one failing check cannot stop the others, which leaves the unit's status as
whatever the *last* check returned: this run reported `0` with 12 red, §16's first run reported `1`
with 15. The `.prom` files are the verdict.

### 18.4 Three assumptions fixed, and a deploy path that could not verify itself

`17a12bc` follows the containers that moved:

- `check-alert-delivery.py` had Alertmanager pinned to `i2` from the 2026-11-02 move. `signara` came
  back to `i3`, so the check ran `incus exec signara` on a host that no longer holds it. The target
  follows the container now, verified first on `i3`.
- `estate-checks.service` passed only `i1` and `i2` to `check-unit-drift.py`, so the host carrying six
  of `i2`'s containers was compared nowhere. `i3` joined the list.
- `check-container-addresses.py` kept `monitor` (`.88`) as an `i2` row after SigNoz was removed, and
  `check-address-latency.py` dials every row in that table — so a removed service was reported as a
  dark door. The row and its `ROLES` entry are gone, recorded the way `capstone`'s removal is.

**Deploying it exposed a deploy path that could not verify itself.** The edge container installs from
`/root/platform-stack`, a **copy with no `.git`**, while its real checkout is
`/usr/src/projects/complete/ips` — a sparse checkout whose `scripts/` was empty on disk. The
installer compares every file it lands against `origin/main` and refused to install the units from a
tree it could not check (*"something did not land — do NOT reload the unit yet"*), leaving
`installed_checks` red with **8** findings for one run. That is the guard working. The fix is the one
it asks for: `git sparse-checkout disable` in the real checkout (56 scripts materialised at
`17a12bc`), install from there, then `daemon-reload`.

**There is one `onyx` left, not two.** `i2` was decommissioned on 2026-11-16 (§19) and its copy went
with it, which retires the "never both run" risk this paragraph was written for: `i3` holds the only
instance, stopped, with `boot.autostart=false`, waiting for its disk or for the replacement host.
That also means there is no second copy to fall back to — the estimator's data is the `tank` volume
on `i3` and nothing else.

## 19. `i2` came back the same evening — recovered and parked, not decommissioned

`i2` (`192.168.1.52`) booted again at **21:20 UTC on 2026-11-15** with everything still on it: its
eight Incus instances (`proxy`, `distro`, `magnate`, `onyx`, `pi`, `signara`, `subscribe` and the
stopped `copilotkit`), the pools `default`, `fast` and `tank`, and **all six host-Docker stacks** —
`christ`, `christ-gateway`, `christ-switch`, `christ-telno`, `scif-api-1`, `scif-database-1`, every
one of them `(healthy)`. The decommission recorded in `d4027bd` therefore did not happen, and the
data-loss warning about those stacks is closed: they are running.

**It came up with seven of its instances running, which is the hazard.** Those instances are still
configured to start on boot and they duplicate addresses that now answer from `i3` and `i4`: `proxy`
`.71` on both `i2` and `i4`, and `distro` `.61`, `magnate` `.57`, `pi` `.70`, `signara` `.44`,
`subscribe` `.58` on both `i2` and `i3`, every one pinned. Two instances of one address on one L2 is
a MAC-level fight that `incus list` cannot show, and it reads as intermittent, unexplained failures
in whatever dials it. `onyx` also started there — without `/dev/sdc`, which `i2` does not have.

**Every instance on `i2` is stopped now**, with `boot.autostart=false` and no profile that sets it
(all five profiles: `sets_autostart=0`), so the next boot leaves them down. `i2`'s only address on
the wire is `192.168.1.52/24`. After the stops, each moved address resolves to exactly one MAC, and
it is the intended host's: `.57` → `i3`'s `10:66:6a:f2:52:db`, `.71` → `i4`'s copy, while `i2`'s own
`magnate` carried `10:66:6a:7b:75:d3`. They were never the same instance, only the same address.

**The six Docker stacks on `i2` were left running on purpose.** They publish no estate LAN address,
so they are not part of the conflict, and `christ-telno` is telephony — stopping them would be the
outage the whole warning in §14 was trying to avoid. They stop the day someone says so.

**What stands:** the check tables read three hosts (`i1`, `i3`, `i4`) as of `d4027bd`. That is right
while `i2` is parked and wrong the moment it rejoins, so rejoining is one revert plus moving the
address rows back to wherever the instances run. The sentences in §15, §17 and §18.4 that say "no
rollback copies" describe the 2026-11-16 interval only; this section supersedes them, and the copies
are intact on `i2`.

## 20. pm3's reinstall: the staged kernel was staged correctly and triggered wrongly

Measured 2026-11-15, and it needs a hand on the box.

**What worked.** The installer was prepared and load-tested on pm3: the Ubuntu 26.04.1
live-server ISO (`2,927,861,760` B), `vmlinuz` (`0ad39b13…`, 17,295,752 B) and `initrd`
(`7aeb757e…`, 99,677,109 B) extracted, and `kexec -l` with
`autoinstall ds=nocloud-net;s=http://192.168.1.54:8080/ console=tty0 console=ttyS0,115200 ip=dhcp ---`
returned success, then `kexec -u` unloaded it with Proxmox still running. The autoinstall
served from i4 validates (`version: 1`, `hostname: i2`, addresses `.52` + `.3`, two keys,
six late-commands) and pm3 fetched it itself (`200/200`).

**What broke.** At 22:19 UTC the trigger was `systemctl kexec`, not `kexec -e`. systemd
re-loads a kernel of its own instead of executing the staged image, so the Ubuntu kernel
came up **without** `autoinstall`, `ds=` or `ip=`. The result, measured 22:28–22:31 UTC:
`.3` answers ICMP in 0.54 ms, **every port 1–200 plus 3128/8006/8080/8443/9100 closed**,
VM 200 `i2` silent, and the config server logged **7 GETs total — all of them my own
preflights, none from the installer**. It never received a storage plan, so
`local-lvm` was never wiped: the box is running Proxmox's userspace under a foreign
kernel, with `vmbr0` up and no daemons alive.

**Recovery needs a power cycle**, because pm3 has no BMC, no serial console and no
watchdog (`RuntimeWatchdogUSec` empty, no `/dev/watchdog`). EFI order is unchanged —
`Boot0001* proxmox` → `\EFI\PROXMOX\SHIMX64.EFI` — so the next boot is Proxmox again,
and VM 200 has `onboot: 1`, so `i2` and its `.52` come back with it. Nothing in this
incident touched the disks.

**The corrected trigger** is `scripts/reinstall-pm3-as-i2.sh`: preflight (`pveversion`,
the two image files, the autoinstall fetch, `qm list`), then `kexec -l` with the same
command line minus `console=ttyS0` (that desktop has no serial port) plus `nomodeset`
(unknown GPU on a headless install), then **`kexec -e`**, which executes exactly what was
staged. It never returns.

**Preserved before any of this**, on i4 (`.54`), all readable:

| file | size | sha256 |
|---|---|---|
| `i2-preserve-docker-stacks.tgz` | 212 M | `a23bc7ea9e7fc2ff0cac9a0fc6373750d42df4b269e5c511e2aebeb0f4a1687b` |
| `i2-preserve-configs-20261115.tgz` | 118,534 B | `5faf6d9e1626e0b6772d6ca5e87cad5db4cf5c27dc247e7a3eca7b3e1abcfbed` |
| `i2-preserve-full-20261115.tgz` | 15.9 G | integrity check outstanding |
| `pm3-config-20261115.tgz` | Proxmox network/WireGuard/pve/ssh config | — |
| `pm3-facts-20261115.txt` | `lsblk`, `pvesm`, `qm config 200`, `efibootmgr` | — |

### 20.1 i4 was not full of busy containers — it was full of my own verification job

`uptime` read **34.44 / 37.28 / 21.19 on 2 CPUs** at 22:16 UTC with 158 MB free. Every
Incus container measured **0.0 % of a core** at that moment. The load was host-side, and
the largest single consumer was the `tar tzf` integrity check of the 15.9 G archive
(`gzip` 31.9 %, 4:56 elapsed) that I had started on the same box. Killing it dropped load
to **1.63 / 3.91 / 9.57** and raised available memory from **1721 MB to 2272 MB** within
a minute. That is the honest shape of "CPU pressure on i4".

Of what remained, one container mattered: `proxy` at **62.6 % of one core** and
**4.59 GiB RSS** — the edge, Prometheus and Grafana, so it was not stopped.

Stopped for relief: `terminal` (396 MiB), `ontrak-tix-db` (156 MiB), and the staging
Docker pair `fax-staging-portal-1` (4.49 % CPU) + `fax-staging-db-1`. Both Incus
containers still carry `boot.autostart=true`, so a reboot brings them back until that is
changed.

## 21. 2026-11-16 — `pm3` reinstalled as `i2`: bare metal Ubuntu 26.04.1

`pm3` (`192.168.1.3`) is `i2` now: Ubuntu 26.04.1 LTS, kernel `7.0.0-43-generic`, 8 cores,
15 150 MB RAM, one 476.9 G NVMe with a 468 G root (13 G used), hostname `i2`, root and
`innotel` by key, **`192.168.1.52` and `192.168.1.3`** on `enx50a0300d1cc4`, and Incus 6.0.5
from `resolute/universe` with one pool, `fast` (driver `dir`).

Three kexec attempts were needed, and each failure left one rule: `systemctl kexec` re-loads a
kernel of systemd's choosing (the Ubuntu kernel came up without `autoinstall`, `ds=` or `ip=`,
answering ICMP with every port closed); `kexec` carries a kernel and an initrd but not the ISO
mount, and casper only searches mountable devices for `/casper/filesystem.squashfs` unless told
where the image is; and casper's no-medium path (`scripts/casper:970`) is an interactive shell
unless `panic=` is on the command line (`scripts/functions:45-85`), which on a box with no
monitor is a machine that needs hands on it. The install that worked added
`url=http://192.168.1.54:8080/ubuntu-26.04.1-live-server-amd64.iso` (fetched with `wget`,
`scripts/casper:267`, into initramfs RAM) and `panic=120`, so a failure reboots back in two
minutes. Evidence: ISO GETs at 23:13:56, 23:14:30, 23:15:04, `/meta-data` + `/user-data` at
23:15:41, reboot at 23:21:11, `root@.52` reporting `hostname i2` by 23:23:22.

**The container restore did not come from the archive.** `i2-preserve-full-20261115.tgz` (15.9 G)
passes `tar tzf` (272 486 entries, 143 s), but its Incus volume directories were empty when it
was taken: the eight estate instances are a single directory entry each, and only four OnTrak
rootfs trees carry data (~950 MB each). `incus admin recover` was driven as far as it goes — pool
`fast` recreated as `dir`, `backup.yaml` rewritten from `driver: zfs` to `driver: dir`, metadata
symlinks rebuilt — and then parked the volumes with no config rather than inventing data. Nothing
is lost: six are `RUNNING` on `i3`, `onyx` is `STOPPED` there, `proxy` is `RUNNING` on `i4`, all
at their original addresses. Moving any of them to `i2` is an `incus copy` from the live host,
with the source stopped first.

`192.168.1.3` is deliberately still on the box — a second address on the same NIC, and a lifeline
on a machine with no console. Removing it is one netplan edit, left to the operator.

## 22. 2026-11-16 — `.3` retired, `monarch` revived without a NIC, and the outage I caused on `i1`

Three follow-ups were run: verify `i2` after the move, remove the old `192.168.1.3`, and get
`monarch` back with its stacks, then start moving instances to `i2`.

**`i2` verified after the move.** `hostname i2`, Ubuntu 26.04.1 LTS, kernel `7.0.0-43-generic`,
8 cores, 15 150 MB, 468 G root at 3 % used, key login working. A root password was set with
`chpasswd` and then **verified by an actual password login** (`OK root@i2`) rather than assumed
from the command's exit code. Incus 6.0.5 with pool `fast` (`dir`), `core.https_address :8443`
set, zero instances, 14 339 MB available.

**`.3` retired.** The address was removed from `/etc/netplan/00-installer-config.yaml` (a
timestamped backup sits beside it), `netplan generate` validated the result, and the address was
deleted from the live interface with `ip addr del`. `netplan apply` was deliberately not run on a
headless box whose only access is SSH; deleting the address directly is the reversible half of
the same change and cannot strand the host. Measured after: `.3` no longer answers, `.52`
unaffected.

**`monarch` was running blind, not just stopped.** It was `STOPPED` with
`boot.autostart=false`; starting it brought `.56` back but the container had **no `eth0` device at
all** — only `lo` and docker bridges — so nothing was reachable and most of its stack had exited.
It was given `eth0` with the `nictype`/`parent` values taken from `mail` on the same host, which
produced an address on `.56`. With the NIC in place the compose project was re-applied:
**37 containers running, 0 exited (255)**, `.56:14011`, `:14012` and `:14002` open, `:3301`
loopback-only by design. The missing NIC is the real defect here: a container restored from
config without its device section starts, looks healthy in `incus list`, and serves nothing.

**i1's Incus database collapsed, and then I made it worse.** `incusd` was found looping with
`Failed loading instance from backup file … sql: database is closed` and
`no available cowsql leader server found`, 14 `incusd` processes, load average 85 on 4 cores. To
clear it I ran `systemctl stop incus.service` on `i1` — which **shut down all 15 containers on
that host**. Six addresses went dark within the minute: `.56` (monarch), `.59` (atheniq), `.15`
(mail), `.90` (atlas), `.76` (blueprint), `.83` (clearpath). Stopping the daemon is an action on
the instances, not a read of them; it is not a safe way to clear a wedged database.

Recovery, measured: `incus.socket` and `incus.service` stopped, remaining `incusd` killed, then
`socket` and `service` started once. The database reopened — the fresh log shows instances
auto-starting instead of `database is closed`. Three containers (`monarch`, `mail`, `clearpath`)
had booted into a half-started state under the earlier load (`Failed to retrieve PID of executing
child process`), so they were force-stopped and restarted. `monarch`'s compose project was
re-applied (37 containers). Final state: **15/15 containers `RUNNING`** on `i1`, `mail` answering
25/587/993, `monarch` 14011/14012/14002, `atheniq .59`, `atlas .90`, `blueprint .76`,
`genesis .66` all answering. Load fell 85 → 36.

`clearpath` (`.83`) was the last address not answering, and it turned out not to be damage.
It is `RUNNING`, `exec` works, `eth0` is up with `192.168.1.83/24` and `systemd-networkd` is
active, but nothing listens on 22/80/443 because **there is no service to listen**: it is a bare
Ubuntu 24.04 image container with `limits.memory: 256MiB`, `limits.cpu: "2"`, no
`/usr/sbin/sshd`, and `systemctl list-unit-files --state=enabled` returns nothing. It has never
served SSH; its `.83` answering ICMP is the container's stack, not a service. The first reading
of this — that its services failed to come back after the forced restart — was wrong.

**No instance was moved to `i2`.** The move path was prepared — `i2` listens on `:8443` and issued
a trust token, and `incus remote add` on `i1` only stopped at its interactive fingerprint
confirmation — but pushing host-to-host copies while `i1` was recovering from a daemon failure
would have risked a second outage. `genesis` and `rizzaura`, stopped during the aborted attempt,
are `RUNNING` again on `i1`. `proxy` was left untouched on `i4`, as required.

Dedupe mapping, measured across hosts: only `sage` and `vault` exist on two hosts, `RUNNING` on
`i1` and `STOPPED` on `i4`. The `i4` copies are inert rollback copies, so no duplicate process
runs anywhere in the estate; nothing was deleted.

### What a move actually requires: four prerequisites, none of them obvious

Issuing `incus copy monarch i2:monarch` failed four times before it could start, each time for a
different reason, and each failure is a fact about the estate rather than about the command:

1. **Trust.** `incus remote add i2 https://192.168.1.52:8443 --token=<332-char token>
   --accept-certificate` succeeded only after `core.https_address :8443` was set on `i2`. Without
   `--accept-certificate` the add stops at an interactive fingerprint prompt and dies on EOF in a
   non-interactive shell. Proof it worked: `incus info i2:` returns `api_status: stable`,
   `auth: trusted`.
2. **Profiles.** `Requested profile "docker" doesn't exist`. `i1` defines `default`, `docker`,
   `large`, `medium`, `small`; a fresh `i2` has only `default`, and even that one had **no devices
   at all** — no `root` disk, no `eth0` — so a copied instance would have neither. All five were
   copied across (`incus profile show <p>` on `i1`, `incus profile edit <p>` on `i2`), and `i2`'s
   `default` was given `root` (pool `fast`) and `eth0` (`nictype=bridged parent=br0`) to match
   `i1`'s. Note `incus profile show` in 6.0.5 has **no `--format` flag**; asking for it writes an
   empty profile that `edit` then accepts, so the failure is silent.
3. **A bridge.** The estate's NICs are `parent: br0`, and `i1` runs `br0` as a bridge over
   `enp1s0` with the host's own `192.168.1.51/24` **on the bridge**. `i2` had no bridge at all,
   only `enx50a0300d1cc4` with `192.168.1.52/24`. `br0` was created on `i2` with the address
   moving onto it and the physical port as a member, guarded by a `systemd-run --on-active=180`
   rollback that would have restored the previous netplan had SSH been lost; reachability was
   confirmed on `.52` and the rollback cancelled. Incus's API stayed reachable over the new bridge
   (`auth: trusted`).
4. **Host storage, and the honest limit of a copy.** `monarch` bind-mounts
   `/tank/media-downloads` (131 GB, from the `tank` ZFS pool) at `/data/media`. The copy aborts on
   `Missing source path "/tank/media-downloads"`, which is correct: copying the container without
   its media would move a media server that has no media. The path was created on `i2` and the
   131 GB was copied with `rsync -a --partial` at **~96 MB/s**, measured.

Two failures in that sequence were self-inflicted and worth recording: starting `rsync` seconds
before `netplan apply` killed it at 0 bytes when the interface was rebuilt, and `rsync`'s own
`ssh` does not inherit `-o StrictHostKeyChecking=no`, so it refused at `Host key verification
failed` because `i1`'s `known_hosts` still carried **pm3's pre-reinstall ED25519 key for
`.52`** (line 20). `ssh-keygen -R 192.168.1.52` plus a fresh `ssh-keyscan` fixed it; a stale host
key after a reinstall fails every non-interactive tool that talks to the box, not just `ssh`.
`i1` and `i2` differ in the ways that matter for a move: `i1` = 4 cores, `tank` ZFS pool (458 GB,
407 GB used) with container volumes in `tank/incus/containers`, root filesystem 98 GB with 50 GB
free; `i2` = 8 cores, 468 GB root with 432 GB free, 14 357 MB RAM free, pool `fast` of type `dir`.
The `dir` pool choice matters: it means no ZFS snapshot/replication path exists for these moves,
so every transfer is a filesystem copy over the network.

### Two instances are on `i2`, measured

`clearpath` (`.83`) and `blueprint` (`.76`) were stopped on `i1`, copied with
`incus copy <name> i2:<name> --instance-only`, started on `i2`, and verified from outside: `.83`
answers 2/2 pings at 0.67 ms and `.76` answers 2/2 at 0.52 ms, both `RUNNING` on `i2` while the
`i1` sources are left `STOPPED` as rollback copies. Those two were chosen first because they are
the only candidates that need nothing from the host: `blueprint` is 587 MiB and `clearpath` is a
bare 256 MiB image, and neither binds a host path.

Each move needs `incus start` to be retried after the copy: the first attempt returns
`Instance is busy running a "create" operation` while the transfer is still finalising, and
`incus list i2:<name> --format csv -c s` returns **empty** during that window rather than a
state, so an empty answer is "still copying", not "absent".

### `monarch`: stopped for the move, and that is what is still open

`monarch` was stopped to make a consistent copy, which took its media stack down; the copy then
aborted on the host bind, and restarting the container on `i1` did **not** restore the stack —
`monarch` returns to `RUNNING` with **0 docker containers** and `.56:22/14011/14012/14002` all
closed. The same failure happened earlier in this session and was fixed by re-applying the compose
project (`docker compose -p monarch --env-file .env -f docker-compose.yml up -d --no-build`),
which is what produced the 37 containers the first time. Restarting the instance is not the same
as starting its stack.

Getting the stack back took four passes, and each pass failed for a different reason, so the
sequence is worth keeping:

1. `incus start monarch` returns cleanly and `.56:22` opens, but `docker ps` is still 0.
2. `systemctl start docker` inside the container answers `Failed to connect to bus: No such file
   or directory` — systemd in the container is not up yet, so the command is a no-op that must be
   retried, not an error to act on.
3. On the next pass dockerd is `active` and compose starts 4 -> 6 containers, then fails with
   `Conflict. The container name "/authentik-ldap" is already in use by container 94d2c4e8...`.
   The containers survived the stop, and compose will not adopt a name it does not own, so
   `docker rm -f <name>` has to clear them before `up` can proceed.
4. At load 140 the `incus exec` call itself starts failing with `Error: websocket: bad handshake`
   and `Operation not found` — the same starvation that kills `sshd` also breaks the Incus API,
   so a garbled container count from that window is not a measurement.

One trap worth naming: `pkill -f "rsync -a --partial /tank"` run over SSH **kills its own shell**,
because the pattern appears in the script's argv, and the session dies mid-script looking like a
network failure. Bracketed patterns (`pkill -f "[r]sync -a --partial"`) are the only safe form.

### monarch's copy to i2 rolled back, and why its stack would not start

The 30 GiB rootfs was copied after the media mirror proved complete (the sync timer's own pass
sent 17 309 bytes against a 145 306 004 461-byte total with `SYNC_EXIT=0`), the sync timer was
stopped so it could not compete, and the copy ran as a detached job for over 7 minutes.

It did not land. After the copy's `create`/`migrate` operations finally drained on both remotes,
`incus start i2:monarch` failed twice with
`Failed to handle idmapped storage: Failed expanding symlinks of "": lstat /var/lib/incus/containers/monarch: no such file or directory`
and then with `Failed to fetch instance "monarch" in project "default": Instance not found` — the
instance had been rolled back, leaving nothing on `i2`. `i2`'s pool is `dir`, which is where this
differs from the five moves that did work: those were 256 MiB - 2.7 GiB instances, while this one
carries a 30 GiB rootfs plus a 138 GB host bind.

**While the copy was being retried, monarch's own stack would not come back, and the reason was
inside the container:** all three docker units were masked.

```
docker-restore.service    masked   enabled
docker.service            masked   enabled
docker.socket             masked   enabled
```

A masked `docker.service` fails `systemctl start docker` with `Unit docker.service is masked`, so
compose returns 1 and the host looks broken when nothing is wrong with the stack. Masking
`docker.socket` and `docker-restore.service` in the same state is what makes this a single act
rather than a leftover, and it has to be undone for all three together: unmasking only
`docker.service` is not enough.

### `rizzaura` and `regcache` moved; `nas` is blocked on a custom volume

`rizzaura` and `regcache` are `RUNNING` on `i2`; `nas` is not, and the reason is the useful
part. Three failures in a row, each hiding the next:

1. `incus copy rizzaura i2:rizzaura --instance-only` while the source was still running returns
   `Unable to perform live container migration. CRIU isn't installed on the target server`. The
   `incus stop --timeout 90` had reported nothing and the container was still `RUNNING` when the
   copy started, so **the stop has to be confirmed by reading the state back**, not by the exit
   code of `incus stop`.
2. `incus copy nas i2:nas --instance-only` then fails on its own: `Failed add validation for
   device "data": Failed to get storage pool "incus": Storage pool not found`. `nas` carries a
   device pointing at the pool by name, and `i2` has no pool called `incus` — its pool is `fast`.
3. Retrying with `--device data,pool=fast` still fails, and for a different reason:
   `Failed loading custom volume: Storage volume not found`. Overriding the pool name does not
   create the volume — the custom volume has to be copied into `i2`'s pool first, and only then
   can the instance be created. Renaming a pool in the copy arguments is not a substitute for
   moving the volume.

`regcache` answers on `192.168.1.79` (2/2 pings at 0.48 ms) with port 22 **closed**, which is
consistent with a cache node that never ran an SSH server rather than with a failed boot — the
same shape as `clearpath` earlier.

### Two `rsync` jobs were copying the same 131 GB

When the copy was inspected there were **two** jobs writing `/tank/media-downloads/` to `.52`: one
wrapped in `ionice -c 3 nice -n 19` with `--bwlimit=30M --info=stats2 --log-file=/var/log/media-sync.log`,
and a plain `rsync -a --partial`. The destination had reached 122 955 MB of 131 000 MB. The
throttled job is the one to keep (it logs to `/var/log/media-sync.log` and yields to production
work); the duplicate only added disk contention to a host already at load 140.

### The media copy, and the load it caused

`/tank/media-downloads` is 131 GB. Throughput was measured rather than assumed: ~96 MB/s while
the transfer was mostly large files (23 214 MB in the first 240 s), then ~13 MB/s, then ~3 MB/s as
it reached directories of small files — 118 959 → 120 415 MB took seven minutes. It was killed
twice: once by my own `netplan apply` (see above), and once by `SIGHUP` because
`nohup cmd &` inside an SSH session did not survive its parent; `setsid nohup` is what detaches
properly. Resumed with `--partial` each time, it reached 121 122 MB of 131 000 MB.

The cost landed on `i1`: after the moves and during the copy, load average (4 cores) went
85 → 36 → **137.89**, and at that load `sshd` accepts the TCP connection but never completes the
banner exchange, so every command to the host fails as `Connection timed out during banner
exchange` while `ping` and a raw connect to port 22 both succeed. A retry loop is what gets
commands through; the port being open is not evidence the host can answer.

## Where the estate stands (2026-11-16)

On `i2`, seven instances now run: `monarch` (`.56`, 37 containers, `14002/14011/14012` open),
`clearpath` (`.83`), `blueprint` (`.76`), `regcache` (`.79`), `rizzaura` (`.62`), `sage` (`.94`)
and `nas` (`.89`, samba on 139/445, with its 23 GiB `nas-data` volume copied into `fast`). Each
`i1` source is kept `STOPPED` as a rollback copy — `monarch-retired`, `nas-retired`, and the
others under their own names. `i2` has 225 G free.

The `/tank/media-downloads` mirror is complete and in sync (145 306 004 461 bytes, with the
timer's last pass sending 17 309 bytes and exiting 0), and `media-sync.timer` was re-enabled
after the failed copy.

On `i1`: `atheniq` (`.59`), `atlas` (`.90`), `genesis` (`.66`), `genie-preview` (`.24`),
`mail` (`.15`), `ontrak` (`.21`), `vault` (`.73`) and `workloads` (`.113`) still run there.

Still open:
- `atlas` (25.6 GiB) is still on `i1`; its copy is expected to hit the same stalled-create
  behaviour that `atheniq` did, so it needs a watched job rather than a fire-and-forget copy.
- `sage`'s address drifted and cannot be pinned from the host. It answered `.71` on `i1`; `.71` still answers ping
  and offers OpenSSH 8.9p1 from MAC `10:66:6a:80:7b:e8`, which matches no instance on `i1` or
  `i2` — an outside device picked up that lease. The moved copy is not the same guest identity:
  its volatile MAC changed from `10:66:6a:fb:a7:48` to `10:66:6a:0e:d1:59`, so the LAN DHCP gave
  it `.94` instead. It cannot be pinned at the Incus layer: `br0` is an unmanaged parent bridge, so
  `incus config device override sage eth0 ipv4.address=192.168.1.94` is rejected with
  `Cannot use manually specified ipv4.address when using unmanaged parent bridge`. A stable
  address therefore needs a DHCP reservation on the LAN router, or static configuration inside
  the guest; `.94` currently answers on 22 and 443. Any reference to `sage` by its old `.71` needs
  updating, because that address now belongs to something else.
- `ontrak`, `workloads` and `genie-preview` carry host binds and have not been examined for
  moves.

### The network regression on i2 was a bridge mismatch, not a guest failure

`blueprint`, `clearpath` and `regcache` reported `RUNNING` with correct addresses on `eth0` and
still could not reach the gateway from inside, while `rizzaura` on the same host was fine. The
host side explains it:

```
20: veth3bd442fc@if19: ... master br0        <- rizzaura
29: vethb2598520@if28: ... master incusbr0   <- blueprint
31: vethce6c4caa@if30: ... master incusbr0   <- regcache
33: veth8747a217@if32: ... master incusbr0   <- clearpath
```

Their veths were enslaved to `incusbr0` (the bridge Incus creates by name default), not `br0`,
so their traffic never left the host. A guest with a valid address, an `UP` link and 100% packet
loss to its own gateway is a bridge membership problem on the host, and restarting the guest
cannot fix it — a restart recreates the veth on the same wrong bridge. The fix is the device
parent (`incus config device override <instance> eth0 parent=br0`), which persists, rather than
moving the live veth, which does not.

Correction to the fix above: the durable change is in the **profile**, not the instance. `i2`'s
`default` profile carried `eth0` as `network: incusbr0`, and an instance-level override is
rejected outright:

```
Error: Invalid devices: Device validation failed for "eth0":
Cannot use "parent" property in conjunction with "network" property
```

`network` and `parent` are mutually exclusive, so the profile device has to be replaced with a
bridged one (`incus profile device add default eth0 nic name=eth0 nictype=bridged parent=br0`)
before any veth can land on `br0`. `i1`'s `default` profile already uses `parent: br0`, which is
why the same instance behaves differently on the two hosts.

### monarch is serving from i2 (the earlier rollback reading was wrong)

The move was recorded above as rolled back on the strength of `incus start i2:monarch` answering
`Instance not found` and `i1` still listing `monarch` as `RUNNING`. Both readings were wrong in
the same way: the 30 GiB copy had landed, `i1` had renamed its own copy to `monarch-retired`
(`STOPPED`), and the `i2` instance appeared once its `create` operation had fully drained. A
host that says "not found" while an operation list is still draining is not a host that has
nothing.

The instance started cleanly on `i2`'s `dir` pool — no idmapped-storage error — 25 docker unit
lines still read `masked` inside the copy, and after unmasking `docker.service`,
`docker.socket` and `docker-restore.service` and running compose, the host answers from the
moved copy:

```
i2 monarch: RUNNING
compose rc=0
containers: 37
.56:22 open   .56:14002 open   .56:14011 open   .56:14012 open
```

`i1` keeps `monarch-retired` as the rollback copy, stopped. Five instances now run on `i2`:
`monarch`, `clearpath`, `blueprint`, `regcache` and `rizzaura`.

Two partial artifacts are left on `i2` and are not production state: a `genesis` instance at
`STOPPED` from an interrupted copy, and a `nas-data` volume record whose transfer died with
`websocket: close 1006 (abnormal closure): unexpected EOF`.

## Second pass: sage moved, atheniq reverted, i2 cleaned

`sage` moved the same way as the small instances — stop, `incus copy --instance-only`, start on
`i2` — and `.71` answers from the moved copy. `i2` now runs six: `monarch`, `clearpath`,
`blueprint`, `regcache`, `rizzaura` and `sage`.

The two 25 GiB instances behave differently from monarch's. `atheniq` (26.3 GiB) and `atlas`
(25.6 GiB) never finished: after ten minutes the `create` operation was still open on both hosts
with a **completely empty log**, `incus start` on either side answered `Instance is busy running a
"create" operation`, and cancelling on `i2` was refused with `This operation can't be cancelled`.
atheniq was put back on `i1` (`RUNNING`, `.59` answering) and the partial `i2` copy deleted.
Leaving a production instance stopped behind an operation that cannot be cancelled is worse than
not moving it.

The rule this leaves: a copy whose log stays empty while both operation lists stay non-empty is
stalled, not slow — restore the source instead of waiting on it, and treat 25 GiB-and-up copies
as jobs that need watching for tens of minutes rather than a single command.

Cleanup on `i2` also removed the stale `genesis` copy, so the only volumes left there are the six
instance rootfs volumes; the failed `nas-data` volume record had already disappeared with its
transfer.

## nas moved, with its data volume

`nas-data` (23.1 GiB, `tank/incus/custom/default_nas-data`) copied cleanly once it was launched as
its own transient unit: `incus storage volume copy incus/nas-data i2:fast/nas-data` ended with
`Storage volume copied successfully!` and 25.4 GB of volume landed in i2's dir pool. The transfer
takes minutes and writes nothing until it finishes, so an empty log is not evidence of a stall for
a volume copy — the on-disk growth of `/var/lib/incus/storage-pools/fast/custom/default_nas-data`
is what distinguishes slow from stuck.

The instance then moved with both devices repointed:

    incus copy nas i2:nas --instance-only --device root,pool=fast --device data,pool=fast

The first attempt added `source=nas-data` to the data override and failed at once with
`Failed add validation for device "data": Failed to get storage pool "fast,source=nas-data"`. A
`--device` override is a comma list of `key=value` pairs for that device, and `pool` took the rest
of the string; `source` cannot be appended there. Overriding only `pool` is both correct and
sufficient, because `source: nas-data` is already in the device config and a volume of that name
now exists in the target pool. `i2:nas` started and answers on `.89`; `i1` keeps the stopped copy
as `nas-retired`.
