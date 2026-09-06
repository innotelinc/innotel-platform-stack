# Innotel Extensions — Universal Addon System

Extensions are self-contained Docker Compose fragments that attach to any group.
They declare their dependencies (both local and cross-mesh), and the orchestrator
wires them together automatically.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Extension Manifest (ext.yml)                │
├─────────────────────────────────────────────────────────────────┤
│  name: monitoring                                              │
│  version: 1.0.0                                                │
│  group: any              # which group(s) this can attach to   │
│  mesh_service: true      # registers with Consul               │
│  mesh_dependencies:      # cross-group deps resolved via mesh  │
│    - authentik                                             │
│  local_dependencies:      # must run on same server           │
│    - redis                                                  │
│  compose_file: docker-compose.ext.yml                         │
│  env_file: .env.example                                       │
└─────────────────────────────────────────────────────────────────┘
```

## How it works

1. **Register**: Drop your extension in `extensions/<name>/` with an `ext.yml` manifest
2. **Enable**: `./stack.sh enable <extension-name> [group...]` or `[all]`
3. **Wire**: The orchestrator injects mesh IPs as env vars and adds Consul registration
4. **Run**: `./stack.sh up` starts everything; extensions auto-discover cross-group deps

## Creating an Extension

```
extensions/my-extension/
├── ext.yml                 # manifest (required)
├── docker-compose.ext.yml  # compose fragment (required)
├── .env.example            # env template (optional)
└── README.md               # docs (optional)
```

### ext.yml fields

| Field                | Type     | Description                                      |
|----------------------|----------|--------------------------------------------------|
| `name`               | string   | Unique extension name                            |
| `version`            | string   | Semver                                           |
| `description`        | string   | One-line description                             |
| `group`              | string   | Target group(s): `1`-`5`, `any`, `specific list` |
| `mesh_service`       | bool     | Register with Consul service mesh                |
| `mesh_dependencies`  | list     | Service names to discover via Consul             |
| `local_dependencies` | list     | Services that must run on the same server        |
| `compose_file`       | string   | Docker Compose file (relative to extension dir)  |
| `env_file`           | string   | Env template file                                |
| `ports`              | list     | Host ports this extension exposes                |
| `volumes`            | list     | Named volumes to create                          |

### Environment variables injected automatically

| Variable              | Source       | Description                        |
|-----------------------|-------------|------------------------------------|
| `MESH_REGISTRY_ADDR` | Consul      | Registry address for discovery     |
| `MESH_SERVER_IP`      | .env        | This server's mesh IP              |
| `MESH_NETWORK`        | .env        | The overlay subnet                 |
| `DEP_<NAME>_ADDR`     | Consul      | Resolved address of dependency     |
| `DEP_<NAME>_PORT`     | Consul      | Resolved port of dependency        |

### Example: adding Prometheus to Group 3

```yaml
# extensions/monitoring/ext.yml
name: prometheus
version: 1.0.0
description: Prometheus metrics + Grafana dashboards
group: any
mesh_service: true
mesh_dependencies:
  - cerulean
local_dependencies: []
compose_file: docker-compose.ext.yml
ports:
  - "9090:9090"
  - "3000:3000"
```
