# Innotel Platform Stack — Architecture

## Overview

The Innotel Platform Stack is a modular, multi-server infrastructure where each
service group runs independently on its own server (physical, VM, or Incus/LXC
container) and connects via a WireGuard mesh overlay network.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          WireGuard Mesh (10.10.0.0/16)                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│  │ Server 1 │ │ Server 2 │ │ Server 3 │ │ Server 4 │ │ Server 5 │        │
│  │ .10.1.1  │ │ .10.2.1  │ │ .10.3.1  │ │ .10.4.1  │ │ .10.5.1  │        │
│  │          │ │          │ │          │ │          │ │          │        │
│  │ Cerulean │ │ Capstone │ │ Monarch  │ │ Rizzaura │ │  Atlas   │        │
│  │ AthenIQ  │ │   Zeus   │ │ Jellyfin │ │  ONYX    │ │  Oasis   │        │
│  │ Magnate  │ │OmniRoute │ │   *arr   │ │          │ │  Gitea   │        │
│  │ Consul   │ │          │ │   NPM    │ │          │ │  Chef    │        │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘        │
│                                                                             │
│  Consul Registry (10.10.1.1:8500) ← every group registers here            │
└─────────────────────────────────────────────────────────────────────────────┘
```

## The 5 Groups

| # | Name      | Role                      | Services                          | RAM    |
|---|-----------|---------------------------|-----------------------------------|--------|
| 1 | Primary   | Auth backbone + LMS + billing | Cerulean, AthenIQ, Magnate, Consul | ~8 GiB |
| 2 | Voice     | PBX + VoIP + LLM gateway  | Capstone, Zeus, OmniRoute, coturn | ~6 GiB |
| 3 | Media     | Streaming + automation    | Jellyfin, *arr, NPM edge          | ~8 GiB |
| 4 | Social    | Leaderboard + storage     | Rizzaura, ONYX                    | ~6 GiB |
| 5 | Dev       | Code + mail               | Atlas (Gitea+Chef), Oasis         | ~4 GiB |

## Cross-Network Connectivity

### WireGuard Mesh

Every server runs a WireGuard container that joins the `10.10.0.0/16` overlay.
Containers on different physical networks can reach each other by their mesh IP.

```
Server A (10.10.1.1) ──WireGuard──► Server B (10.10.2.1)
   │                                      │
   g1-cerulean ◄── HTTP ──► g2-omniroute
```

### Service Discovery (Consul)

Group 1 runs the Consul server. Every group registers its services at startup.
Any service can discover another by name:

```bash
# Find where OmniRoute lives
./stack.sh discover omniroute
# → 10.10.2.1:20128

# Find where Authentik lives
./stack.sh discover authentik
# → 10.10.1.1:9000
```

### Environment Variable Injection

When the orchestrator starts a group, it exports:
- `MESH_SERVER_IP` — this server's mesh IP
- `DEP_<NAME>_ADDR` / `DEP_<NAME>_PORT` — resolved addresses of dependencies
- `REGISTRY_ADDR` — Consul address for runtime discovery

## Extension System

Extensions are self-contained Docker Compose fragments that attach to any group.

### Creating an Extension

```
extensions/my-extension/
├── ext.yml                 # manifest (required)
├── docker-compose.ext.yml  # compose fragment (required)
├── .env.example            # env template (optional)
└── README.md               # docs (optional)
```

### Enabling Extensions

```bash
# Enable on a specific group
./stack.sh enable monitoring 3     # Prometheus+Grafana on Monarch

# Enable on all groups
./stack.sh enable cerulean-auth all  # Authentik on every server

# Check what's enabled
./stack.sh status
```

### Built-in Extensions

| Extension       | Description                          | Dependencies       |
|-----------------|--------------------------------------|---------------------|
| `cerulean-auth` | Authentik SSO                        | —                   |
| `cerulean-dns`  | BIND authoritative DNS + ACME        | cerulean-auth       |
| `omniroute-llm` | OpenAI-compatible LLM gateway        | —                   |
| `monitoring`    | Prometheus + Grafana                 | cerulean-auth       |
| `minio-storage` | S3-compatible object storage         | —                   |

## Single-Server Mode

To run everything on one server, use the unified mode:

```bash
./stack.sh up all    # starts all 5 groups + mesh
```

All groups share the mesh network. Consul runs once (Group 1). Each group
gets its own Docker network to avoid collisions.

## Multi-Server Mode

1. Clone the repo on each server
2. Fill in `.env` with that server's IP and keys
3. On Server 1: `./stack.sh up 1` (starts Consul server + mesh)
4. On Servers 2-5: `./stack.sh up <group>` (joins mesh, registers with Consul)
5. Any server: `./stack.sh discover <service>` (finds it anywhere)

## Flexible Grouping (any combination, any host)

Groups are independent Docker projects that all join the **same shared mesh
network** — so you can run any combination on any server:

```bash
# Group 2 completely alone on one server
./stack.sh up 2

# Groups 3 + 4 together on a single server (verified working)
./stack.sh up 3 4

# Everything on one box
./stack.sh up all

# Spread differently: voice+social on one box, media alone
./stack.sh up 2 4     # on server A
./stack.sh up 3       # on server B
```

Each group keeps its own Docker network (`g1-net` … `g5-net`) to avoid
collisions, and every group joins the shared `innotel-mesh-net` where the
mesh's Consul agent lives. Services on the same host reach Consul directly as
`mesh-consul:8500`; services on other hosts reach it over the WireGuard mesh.

## Port Map

| Group | Service          | Host Port | Mesh Address            |
|-------|------------------|-----------|-------------------------|
| 1     | Cerulean         | 3003      | 10.10.1.1:3003          |
| 1     | Authentik        | 9000      | 10.10.1.1:9000          |
| 1     | Infisical        | 8080      | 10.10.1.1:8080          |
| 1     | Vault            | 8200      | 10.10.1.1:8200          |
| 1     | Consul           | 8500      | 10.10.1.1:8500          |
| 1     | LMS origin       | 18000     | 10.10.1.1:18000         |
| 1     | CMS origin       | 18001     | 10.10.1.1:18001         |
| 1     | Magnate          | 3010      | 10.10.1.1:3010          |
| 2     | Capstone API     | 3000      | 10.10.2.1:3000          |
| 2     | FreePBX          | 8083      | 10.10.2.1:8083          |
| 2     | Zeus Portal      | 3001      | 10.10.2.1:3001          |
| 2     | coturn           | 3478      | 10.10.2.1:3478          |
| 2     | OmniRoute        | 20128     | 10.10.2.1:20128         |
| 3     | NPM              | 80/443    | 10.10.3.1:80            |
| 3     | Jellyfin         | 8096      | 10.10.3.1:8096          |
| 3     | Prowlarr         | 9696      | 10.10.3.1:9696          |
| 4     | Rizz API         | 8000      | 10.10.4.1:8000          |
| 4     | Rizz App         | 3010      | 10.10.4.1:3010          |
| 4     | ONYX API         | 8080      | 10.10.4.1:8080          |
| 4     | ONYX ObjectStore | 9001      | 10.10.4.1:9001          |
| 5     | Gitea            | 3000      | 10.10.5.1:3000          |
| 5     | Convex           | 3210      | 10.10.5.1:3210          |
| 5     | Oasis Mail       | 587       | 10.10.5.1:587           |

## Security Model

- **Authentik** (Cerulean) is the single identity provider for all services
- **OmniRoute** (Zeus) is the single LLM gateway for all AI services
- **Coturn**: Zeus is primary; Capstone runs coturn only behind `standalone` profile
- **WireGuard** encrypts all cross-server traffic
- **Consul** gossip encryption prevents unauthorized service registration
- Every extension gets its own Docker network; cross-group access goes through mesh
