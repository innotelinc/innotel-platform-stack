#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# Innotel Platform Stack — Unified Orchestrator
# ══════════════════════════════════════════════════════════════════════════════
# Manages all 5 groups, the WireGuard mesh, and extensions.
#
# Usage:
#   ./stack.sh <command> [group|extension] [options]
#
# Commands:
#   up      [group|all]   Start group(s) and their extensions
#   down    [group|all]   Stop group(s)
#   status  [group|all]   Show status of all groups and services
#   logs    [group]       Tail logs for a group
#   enable  <extension> [group|all]  Enable an extension on a group
#   disable <extension> [group|all]  Disable an extension from a group
#   list                  List all groups, extensions, and services
#   mesh                  Start only the mesh network
#   discover <service>    Find a service across all groups
#   register <service> <addr> <port> [tags]  Register a service manually
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

STACK_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${STACK_DIR}/.env"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ── Group definitions ─────────────────────────────────────────────────────────
declare -A STACK_GROUPS=(
  [1]="primary|Cerulean + AthenIQ + Magnate + Signara|~10 GiB"
  [2]="voice|Capstone + Zeus + OmniRoute|~6 GiB"
  [3]="media|Monarch (Jellyfin + *arr + NPM)|~8 GiB"
  [4]="social|Rizzaura + ONYX|~6 GiB"
  [5]="dev|Atlas + Oasis|~4 GiB"
)

# ── Helpers ────────────────────────────────────────────────────────────────────
info()  { echo -e "${BLUE}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[error]${NC} $*" >&2; }

get_group_dir() {
  local num="$1"
  echo "${STACK_DIR}/groups/${num}-$(echo "${STACK_GROUPS[$num]}" | cut -d'|' -f1)"
}

get_mesh_ip() {
  local num="$1"
  grep "SERVER_${num}_" "$ENV_FILE" 2>/dev/null \
    | grep "_IP=" | grep -v "PUBLIC_IP" | head -1 | cut -d= -f2
}

get_public_ip() {
  local num="$1"
  grep "SERVER_${num}_PUBLIC_IP=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2
}

mesh_subnet_for() {
  # Derive the /24 WireGuard subnet from the server mesh IP (10.10.x.1 → 10.10.x.0)
  local ip="$1"
  echo "${ip%.*}.0"
}

export_group_env() {
  local num="$1"
  local mesh_ip public_ip
  mesh_ip=$(get_mesh_ip "$num")
  public_ip=$(get_public_ip "$num")

  if [ -n "$mesh_ip" ]; then
    export MESH_SERVER_IP="$mesh_ip"
    export MESH_SUBNET="$(mesh_subnet_for "$mesh_ip")"
    export INTERNAL_SUBNET="$(mesh_subnet_for "$mesh_ip")"
  fi
  if [ -n "$public_ip" ]; then
    export SERVER_PUBLIC_IP="$public_ip"
  fi

  # Group 1 runs the Consul server; all others are clients
  if [ "$num" = "1" ]; then
    export CONSUL_SERVER_FLAG="-server=true -bootstrap-expect=1"
    export CONSUL_SERVER_ADDR="$mesh_ip"
  else
    export CONSUL_SERVER_FLAG="-server=false"
    export CONSUL_SERVER_ADDR="$(get_mesh_ip 1)"
  fi

  # Registry address for service registration/discovery (mesh IP of Consul)
  export REGISTRY_ADDR="$(get_mesh_ip 1):8500"
  export CONSUL="$REGISTRY_ADDR"
}

ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    err ".env not found. Copy .env.example to .env and fill in values."
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$ENV_FILE"
}

compose() {
  local dir="$1"; shift
  docker compose -f "${dir}/docker-compose.yml" "$@"
}

# ── Commands ───────────────────────────────────────────────────────────────────

cmd_mesh() {
  info "Starting WireGuard mesh network..."
  docker compose -f "${STACK_DIR}/mesh/docker-compose.mesh.yml" up -d
  ok "Mesh network running. Consul UI: http://localhost:8500"
}

cmd_up() {
  ensure_env

  # Multiple groups: ./stack.sh up 2 3 4  (runs them together on one host)
  local all_targets="$*"
  if [ -z "$all_targets" ]; then all_targets="all"; fi

  if [ "$all_targets" = "all" ]; then
    # Start mesh first
    cmd_mesh
    # Start all groups
    for num in 1 2 3 4 5; do
      cmd_up "$num"
    done
    return
  fi

  # Multiple explicit groups: start mesh once, then each group
  local num_count=0
  for t in $all_targets; do
    case "$t" in
      [1-5]) num_count=$((num_count+1)) ;;
    esac
  done
  if [ "$num_count" -gt 1 ]; then
    if ! docker network ls 2>/dev/null | grep -q innotel-mesh-net; then
      cmd_mesh
    fi
    for t in $all_targets; do
      case "$t" in
        [1-5]) cmd_up "$t" ;;
      esac
    done
    return
  fi

  local target="${1:-all}"

  if [ "$target" = "mesh" ]; then
    cmd_mesh
    return
  fi

  local dir
  dir=$(get_group_dir "$target")
  if [ ! -d "$dir" ]; then
    err "Group $target not found at $dir"
    exit 1
  fi

  local info_str="${STACK_GROUPS[$target]}"
  local name=$(echo "$info_str" | cut -d'|' -f1)
  local desc=$(echo "$info_str" | cut -d'|' -f2)
  local ram=$(echo "$info_str" | cut -d'|' -f3)

  info "Starting Group ${target} — ${desc} (${ram})..."

  # Export this group's mesh config (IP, subnet, Consul role)
  export_group_env "$target"

  # Bring up the mesh if not running
  if ! docker network ls 2>/dev/null | grep -q innotel-mesh-net; then
    cmd_mesh
  fi

  # Start the group (include: merges mesh + consul from this file)
  compose "$dir" up -d

  # Enable any active extensions
  local ext_state="${STACK_DIR}/.extensions.${target}"
  if [ -f "$ext_state" ]; then
    while IFS= read -r ext_name; do
      [ -z "$ext_name" ] && continue
      local ext_dir="${STACK_DIR}/extensions/${ext_name}"
      if [ -d "$ext_dir" ] && [ -f "$ext_dir/docker-compose.ext.yml" ]; then
        info "  Enabling extension: ${ext_name}"
        docker compose -f "$ext_dir/docker-compose.ext.yml" up -d 2>/dev/null || true
      fi
    done < "$ext_state"
  fi

  ok "Group ${target} (${name}) is up"
}

cmd_down() {
  ensure_env

  # Multiple groups: ./stack.sh down 2 3 4
  local all_targets="$*"
  if [ -z "$all_targets" ]; then all_targets="all"; fi

  if [ "$all_targets" = "all" ]; then
    for num in 1 2 3 4 5; do
      cmd_down "$num"
    done
    info "Stopping mesh..."
    docker compose -f "${STACK_DIR}/mesh/docker-compose.mesh.yml" down
    ok "All groups stopped"
    return
  fi

  local num_count=0
  for t in $all_targets; do
    case "$t" in
      [1-5]) num_count=$((num_count+1)) ;;
    esac
  done
  if [ "$num_count" -gt 1 ]; then
    for t in $all_targets; do
      case "$t" in
        [1-5]) cmd_down "$t" ;;
      esac
    done
    return
  fi

  local target="${1:-all}"

  local dir
  dir=$(get_group_dir "$target")
  if [ ! -d "$dir" ]; then
    err "Group $target not found"
    exit 1
  fi

  info "Stopping Group ${target}..."
  compose "$dir" down

  # Stop extensions too
  local ext_state="${STACK_DIR}/.extensions.${target}"
  if [ -f "$ext_state" ]; then
    while IFS= read -r ext_name; do
      [ -z "$ext_name" ] && continue
      local ext_dir="${STACK_DIR}/extensions/${ext_name}"
      if [ -d "$ext_dir" ] && [ -f "$ext_dir/docker-compose.ext.yml" ]; then
        docker compose -f "$ext_dir/docker-compose.ext.yml" down 2>/dev/null || true
      fi
    done < "$ext_state"
  fi

  ok "Group ${target} stopped"
}

cmd_status() {
  local target="${1:-all}"

  echo -e "\n${BOLD}═══ Innotel Platform Stack ═══${NC}\n"

  if [ "$target" = "all" ] || [ "$target" = "mesh" ]; then
    echo -e "${CYAN}── Mesh Network ──${NC}"
    docker network ls 2>/dev/null | grep -E "innotel-mesh|NAME" || echo "  (not running)"
    echo ""
  fi

  for num in 1 2 3 4 5; do
    if [ "$target" != "all" ] && [ "$target" != "$num" ]; then
      continue
    fi

    local info_str="${STACK_GROUPS[$num]}"
    local name=$(echo "$info_str" | cut -d'|' -f1)
    local desc=$(echo "$info_str" | cut -d'|' -f2)
    local ram=$(echo "$info_str" | cut -d'|' -f3)

    echo -e "${CYAN}── Group ${num} — ${name} (${desc}) ${ram} ──${NC}"

    local dir
    dir=$(get_group_dir "$num")
    if [ -d "$dir" ]; then
      # Export placeholder values so status works even without a full .env
      export AUTHENTIK_PG_PASSWORD="${AUTHENTIK_PG_PASSWORD:-placeholder}"
      export AUTHENTIK_SECRET_KEY="${AUTHENTIK_SECRET_KEY:-placeholder}"
      export TUTOR_MYSQL_ROOT_PASSWORD="${TUTOR_MYSQL_ROOT_PASSWORD:-placeholder}"
      export OASIS_PG_PASSWORD="${OASIS_PG_PASSWORD:-placeholder}"
      export REDIS_PASSWORD="${REDIS_PASSWORD:-placeholder}"
      export INFISICAL_PG_PASSWORD="${INFISICAL_PG_PASSWORD:-placeholder}"
      local ps_out
      ps_out=$(compose "$dir" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null) || true
      if [ -n "$ps_out" ] && ! echo "$ps_out" | grep -q "^NAME"; then
        echo "  (not running)"
      elif [ -z "$ps_out" ]; then
        echo "  (not running)"
      else
        echo "$ps_out"
      fi
    fi

    # Show active extensions
    local ext_state="${STACK_DIR}/.extensions.${num}"
    if [ -f "$ext_state" ] && [ -s "$ext_state" ]; then
      echo -e "  ${YELLOW}Extensions:${NC} $(tr '\n' ', ' < "$ext_state")"
    fi
    echo ""
  done
}

cmd_logs() {
  local target="${1:?Usage: stack.sh logs <group>}"
  ensure_env

  local dir
  dir=$(get_group_dir "$target")
  if [ ! -d "$dir" ]; then
    err "Group $target not found"
    exit 1
  fi

  compose "$dir" logs -f --tail=100
}

cmd_enable() {
  local ext_name="${1:?Usage: stack.sh enable <extension> [group|all]}"
  local target="${2:-all}"
  ensure_env

  local ext_dir="${STACK_DIR}/extensions/${ext_name}"
  if [ ! -d "$ext_dir" ]; then
    err "Extension '${ext_name}' not found in extensions/"
    exit 1
  fi

  if [ ! -f "$ext_dir/ext.yml" ]; then
    err "Extension missing ext.yml manifest"
    exit 1
  fi

  if [ "$target" = "all" ]; then
    for num in 1 2 3 4 5; do
      cmd_enable "$ext_name" "$num"
    done
    return
  fi

  local ext_state="${STACK_DIR}/.extensions.${target}"
  if grep -q "^${ext_name}$" "$ext_state" 2>/dev/null; then
    warn "Extension '${ext_name}' already enabled on Group ${target}"
    return
  fi

  echo "$ext_name" >> "$ext_state"
  info "Enabled extension '${ext_name}' on Group ${target}"

  # If the group is running, start the extension now
  if compose "$(get_group_dir "$target")" ps 2>/dev/null | grep -q "Up"; then
    info "Starting extension on running group..."
    export MESH_SERVER_IP="$(get_mesh_ip "$target")"
    docker compose -f "$ext_dir/docker-compose.ext.yml" up -d 2>/dev/null || true
  fi
}

cmd_disable() {
  local ext_name="${1:?Usage: stack.sh disable <extension> [group|all]}"
  local target="${2:-all}"

  if [ "$target" = "all" ]; then
    for num in 1 2 3 4 5; do
      cmd_disable "$ext_name" "$num"
    done
    return
  fi

  local ext_state="${STACK_DIR}/.extensions.${target}"
  if [ -f "$ext_state" ]; then
    sed -i "/^${ext_name}$/d" "$ext_state"
  fi

  local ext_dir="${STACK_DIR}/extensions/${ext_name}"
  if [ -d "$ext_dir" ] && [ -f "$ext_dir/docker-compose.ext.yml" ]; then
    docker compose -f "$ext_dir/docker-compose.ext.yml" down 2>/dev/null || true
  fi

  ok "Disabled extension '${ext_name}' from Group ${target}"
}

cmd_list() {
  echo -e "\n${BOLD}═══ Groups ═══${NC}"
  for num in 1 2 3 4 5; do
    local info_str="${STACK_GROUPS[$num]}"
    local name=$(echo "$info_str" | cut -d'|' -f1)
    local desc=$(echo "$info_str" | cut -d'|' -f2)
    local ram=$(echo "$info_str" | cut -d'|' -f3)
    echo -e "  ${GREEN}${num}${NC} — ${BOLD}${name}${NC} (${desc}) ${ram}"
  done

  echo -e "\n${BOLD}═══ Extensions ═══${NC}"
  for ext_dir in "${STACK_DIR}"/extensions/*/; do
    [ ! -f "$ext_dir/ext.yml" ] && continue
    local ext_name=$(basename "$ext_dir")
    local desc=$(grep "^description:" "$ext_dir/ext.yml" | cut -d: -f2- | xargs)
    local group=$(grep "^group:" "$ext_dir/ext.yml" | cut -d: -f2 | xargs)
    echo -e "  ${CYAN}${ext_name}${NC} — ${desc} (group: ${group})"
  done

  echo -e "\n${BOLD}═══ Services (via Consul) ═══${NC}"
  ensure_env
  local consul_addr="${REGISTRY_ADDR:-10.10.1.1:8500}"
  curl -s --max-time 3 "http://${consul_addr}/v1/catalog/services" 2>/dev/null | \
    python3 -c "import sys,json; [print(f'  {k}') for k in sorted(json.load(sys.stdin).keys())]" 2>/dev/null || \
    echo "  (Consul not reachable — start with: ./stack.sh up mesh)"
}

cmd_discover() {
  local service="${1:?Usage: stack.sh discover <service-name>}"
  ensure_env
  bash "${STACK_DIR}/mesh/scripts/discover-service.sh" "$service"
}

cmd_register() {
  ensure_env
  bash "${STACK_DIR}/mesh/scripts/register-service.sh" "$@"
}

# ── Main ───────────────────────────────────────────────────────────────────────

cmd="${1:-help}"
shift 2>/dev/null || true

case "$cmd" in
  up|combined) cmd_up "$@" ;;
  down)     cmd_down "$@" ;;
  status)   cmd_status "$@" ;;
  logs)     cmd_logs "$@" ;;
  enable)   cmd_enable "$@" ;;
  disable)  cmd_disable "$@" ;;
  list)     cmd_list ;;
  mesh)     cmd_mesh ;;
  discover) cmd_discover "$@" ;;
  register) cmd_register "$@" ;;
  help|--help|-h)
    echo -e "${BOLD}Innotel Platform Stack — Unified Orchestrator${NC}\n"
    echo "Usage: ./stack.sh <command> [args]\n"
    echo "Commands:"
    echo "  up      [group|all]   Start group(s) and extensions"
    echo "          e.g. ./stack.sh up 2           (group 2 alone)"
    echo "               ./stack.sh up 3 4         (groups 3+4 on one server)"
    echo "               ./stack.sh up all         (everything, single server)"
    echo "  down    [group|all]   Stop group(s)"
    echo "  status  [group|all]   Show running services"
    echo "  logs    [group]       Tail group logs"
    echo "  enable  <ext> [group] Enable an extension"
    echo "  disable <ext> [group] Disable an extension"
    echo "  list                  List groups, extensions, services"
    echo "  mesh                  Start mesh network only"
    echo "  discover <service>    Find a service across all groups"
    echo "  register <svc> <addr> <port> [tags]  Register a service"
    ;;
  *)
    err "Unknown command: $cmd"
    echo "Run: ./stack.sh help"
    exit 1
    ;;
esac
