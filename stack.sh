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
#   list                  List all groups, extensions, components, and services
#   download [component|all] [--start] [--ref <ref>] [--dir <path>]
#                         Clone/update the component repos next to this checkout
#   verify  [component|all] [--dir <path>]
#                         Check the component checkouts before starting
#   audit   [component|all] [--org] [options]
#                         Audit attribution in git history (scripts/audit-attribution.sh)
#   mesh                  Start only the mesh network
#   discover <service>    Find a service across all groups
#   register <service> <addr> <port> [tags]  Register a service manually
#
# Group arguments accept a number (1-5), a component name (plutus, olympus, …),
# or a group alias (primary, voice, media, social, dev).
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

STACK_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${STACK_DIR}/.env"

# ── Mesh layout ───────────────────────────────────────────────────────────────
# The group dirs live BESIDE this repo, not inside it:
#
#   <root>/
#     1-primary/  2-voice/  3-media/  4-social/  5-dev/   ips/
#
# Each group dir holds the repos that run on that server (1-primary/cerulean,
# 2-voice/capstone, …) plus a `docker-compose.yml` that *points at* this repo's
# generated group compose; it declares no services of its own. This repo
# orchestrates them, and `groups/` is where the group composes are built and
# tracked — see `scripts/gen-group-compose.py`.
ROOT_DIR="$(cd "${STACK_DIR}/.." && pwd)"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ── Group definitions ─────────────────────────────────────────────────────────
declare -A STACK_GROUPS=(
  [1]="primary|Cerulean + AthenIQ + Magnate + Signara + NPM Edge|~10 GiB"
  [2]="voice|Capstone + Zeus + OmniRoute|~6 GiB"
  [3]="media|Monarch (Jellyfin + *arr) + PLUTUS|~10 GiB"
  [4]="social|Rizzaura + ONYX|~6 GiB"
  [5]="dev|Atlas + Oasis + Distro + Olympus|~8 GiB"
)

# ── Component registry ─────────────────────────────────────────────────────────
# Every stack component, the repository it comes from, and the checkout
# directory the group compose files expect. The migration moved every repo *into*
# its group dir (`<n>-<group>/<repo>`), so the checkout dir is group-relative —
# `<parent-of-stack>/<dir>` is the real path, not the pre-migration flat sibling
# (`cerulean-dns-platform`, `zeus-pbx-platform`, …) those names used to be.
# `stack.sh download` clones/updates them; `stack.sh up <component>` resolves a
# component to its hosting group; `stack.sh verify` checks the checkout is where
# the group compose builds from.
#
#   name → "<owner/repo>|<checkout dir>|<group>|<branch>|<description>"
#
# A component with an empty group is download-only (no compose services).
declare -A STACK_COMPONENTS=(
  [cerulean]="innotelinc/cerulean|1-primary/cerulean|1|main|TrustOps — Authentik SSO, Cerulean Vault, DNS + certs"
  [atheniq]="innotelinc/atheniq|1-primary/atheniq|1|main|LearningOps — Open edX LMS (tutor-managed)"
  [magnate]="innotelinc/magnate|1-primary/magnate|1|main|RevenueOps — billing and subscriptions"
  [signara]="innotelinc/signara|1-primary/signara|1|main|DocumentOps — signing and audit"
  [sign]="innotelinc/sign|1-primary/sign|1|main|DocumentOps — e-signature stopgap (OpenSign fork) at sign.innotel.us"
  [verifier]="innotelinc/verifier|1-primary/verifier|1|main|PlatformOps — conformity + attribution guard tooling"
  [npm]="innotelinc/npm|1-primary/npm|1|develop|EdgeOps — NPM edge (proxy hosts, TLS termination)"
  [capstone]="innotelinc/capstone|2-voice/capstone|2|main|AgentOps — voice AI agents over Zeus"
  [zeus]="innotelinc/zeus|2-voice/zeus|2|master|VoiceOps — PBX, VoIP, SMS"
  [monarch]="innotelinc/monarch|3-media/monarch|3|main|MediaOps — streaming, media libraries, *arr"
  [plutus]="innotelinc/plutus|3-media/plutus|3|main|VideoOps — AI shopping channel (Convex + ffmpeg)"
  [rizzaura]="innotelinc/rizzaura|4-social/rizzaura|4|main|CommunityOps — leaderboards and reputation"
  [onyx]="innotelinc/onyx|4-social/onyx|4|main|StorageOps — object storage, backups, snapshots"
  [zapit]="innotelinc/zapit|4-social/zapit||main|TransferOps — ephemeral P2P transfer (no group)"
  [atlas]="innotelinc/atlas|5-dev/atlas|5|main|CodeOps — Gitea, Convex, CI"
  [oasis]="innotelinc/oasis|5-dev/oasis|5|master|MailOps — mail, calendar, contacts"
  [distro]="innotelinc/distro|5-dev/distro|5|main|BuilderOps — in-browser AI app builder"
  [olympus]="innotelinc/olympus|5-dev/olympus|5|main|FactoryOps — issue → validated PR factory"
)

# `download all` order — bash associative arrays are unordered, so keep a list.
STACK_COMPONENT_ORDER="cerulean atheniq magnate signara sign verifier npm capstone zeus monarch plutus rizzaura onyx zapit atlas oasis distro olympus"

# ── Helpers ────────────────────────────────────────────────────────────────────
info()  { echo -e "${BLUE}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[error]${NC} $*" >&2; }

get_group_dir() {
  local num="$1"
  echo "${ROOT_DIR}/${num}-$(echo "${STACK_GROUPS[$num]}" | cut -d'|' -f1)"
}

# Resolve one CLI target to a group number: accepts 1-5, "all", "mesh", a group
# alias (primary|voice|media|social|dev), or a component name from the registry.
# Prints the group number (or the target itself for all/mesh); returns 1 if unknown.
resolve_target() {
  local t="$1" num group
  case "$t" in
    1|2|3|4|5|all|mesh) echo "$t"; return 0 ;;
  esac
  for num in 1 2 3 4 5; do
    if [ "$t" = "$(echo "${STACK_GROUPS[$num]}" | cut -d'|' -f1)" ]; then
      echo "$num"; return 0
    fi
  done
  if [ -n "${STACK_COMPONENTS[$t]:-}" ]; then
    group=$(echo "${STACK_COMPONENTS[$t]}" | cut -d'|' -f3)
    if [ -n "$group" ]; then echo "$group"; return 0; fi
  fi
  return 1
}

# Normalize a target list (component names, aliases, group numbers) to group
# numbers. Prints a space-separated, de-duplicated list in the order given.
normalize_targets() {
  local -a resolved=()
  local t g
  for t in "$@"; do
    if ! g="$(resolve_target "$t")"; then
      err "Unknown group or component: ${t}"
      return 1
    fi
    resolved+=("$g")
  done
  # shellcheck disable=SC2086  # intentional: expand only when non-empty (set -u safe)
  printf '%s\n' ${resolved[@]+"${resolved[@]}"} | awk '!seen[$0]++ {printf "%s%s", sep, $0; sep=" "}'
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
    local subnet
    subnet=$(mesh_subnet_for "$mesh_ip")
    export MESH_SUBNET="$subnet" INTERNAL_SUBNET="$subnet"
  fi
  if [ -n "$public_ip" ]; then
    export SERVER_PUBLIC_IP="$public_ip"
  fi

  # Group 1 runs the Consul server; all others are clients
  if [ "$num" = "1" ]; then
    export CONSUL_SERVER_FLAG="-server=true -bootstrap-expect=1"
    export CONSUL_SERVER_ADDR="$mesh_ip"
  else
    local consul_addr
    consul_addr=$(get_mesh_ip 1)
    export CONSUL_SERVER_FLAG="-server=false"
    export CONSUL_SERVER_ADDR="$consul_addr"
  fi

  # Registry address for service registration/discovery (mesh IP of Consul).
  # With no .env entry the mesh IP reads back empty, and "${ip}:8500" becomes
  # ":8500" — a host-less URL every consul-reg companion resolves nowhere, so
  # registrations fail silently and `discover`/Consul DNS just comes up empty.
  # Fall back to the mesh network's own name instead of exporting garbage.
  local registry_mesh_ip
  registry_mesh_ip=$(get_mesh_ip 1)
  if [ -n "$registry_mesh_ip" ]; then
    export REGISTRY_ADDR="${registry_mesh_ip}:8500"
  else
    export REGISTRY_ADDR="${REGISTRY_ADDR:-mesh-consul:8500}"
  fi
  export CONSUL="${CONSUL:-$REGISTRY_ADDR}"
}

ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    err ".env not found. Copy .env.example to .env and fill in values."
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$ENV_FILE"
}

# The group's compose.
#
# It is generated from the member repos and tracked here (`groups/<group>.yml`,
# built by `scripts/gen-group-compose.py`, which `scripts/check-group-compose-drift.py`
# keeps honest) because the hand-written copy in each group dir re-declared the
# same services and drifted from them. Prefer the generated file — it is the
# complete one, it is what the drift check compares against the repos, and its
# relative paths resolve from its own directory. A group dir's own
# `docker-compose.yml` is a pointer to it, for anyone running a group from its
# own directory; the fallback keeps that working if the generated file is absent
# (a checkout without the rest of the estate).
compose() {
  local dir="$1"; shift
  # Declared and assigned on separate lines: a `local x="$(cmd)"` masks the
  # command's exit status, which is what shellcheck's SC2155 is about, and CI
  # runs shellcheck at its default severity on every tracked script and hook —
  # the same bar (and the same shellcheck build) the member repos run.
  local generated
  generated="${STACK_DIR}/groups/$(basename "${dir}").yml"
  if [ -f "$generated" ]; then
    docker compose -f "$generated" "$@"
    return
  fi
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

  # Multiple groups: ./stack.sh up 2 3 4  (runs them together on one host).
  # Component names and group aliases resolve to their group number.
  if [ $# -eq 0 ]; then set -- all; fi
  local all_targets
  all_targets=$(normalize_targets "$@") || exit 1
  if [ -z "$all_targets" ]; then all_targets="all"; fi
  # keep positional params aligned with the normalized targets
  # shellcheck disable=SC2086  # word splitting is intended
  set -- $all_targets

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

  # Fail fast when this group's component checkouts are missing or wrong
  # (STACK_SKIP_VERIFY=1 bypasses the check).
  if [ "${STACK_SKIP_VERIFY:-0}" != "1" ]; then
    verify_group_components "$target" || exit 1
  fi

  local info_str="${STACK_GROUPS[$target]}"
  local name desc ram
  name=$(echo "$info_str" | cut -d'|' -f1)
  desc=$(echo "$info_str" | cut -d'|' -f2)
  ram=$(echo "$info_str" | cut -d'|' -f3)

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
  if [ $# -eq 0 ]; then set -- all; fi
  local all_targets
  all_targets=$(normalize_targets "$@") || exit 1
  if [ -z "$all_targets" ]; then all_targets="all"; fi
  # shellcheck disable=SC2086  # word splitting is intended
  set -- $all_targets

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
  if [ $# -gt 0 ]; then
    target=$(resolve_target "$1") || { err "Unknown group or component: $1"; exit 1; }
  fi

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
    local name desc ram
    name=$(echo "$info_str" | cut -d'|' -f1)
    desc=$(echo "$info_str" | cut -d'|' -f2)
    ram=$(echo "$info_str" | cut -d'|' -f3)

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
  local target="${1:?Usage: stack.sh logs <group|component>}"
  target=$(resolve_target "$target") || { err "Unknown group or component: $target"; exit 1; }
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
    local mesh_ip
    mesh_ip=$(get_mesh_ip "$target")
    export MESH_SERVER_IP="$mesh_ip"
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

cmd_list_components() {
  echo -e "\n${BOLD}═══ Components ═══${NC}"
  local c repo dir group branch desc marker
  for c in $STACK_COMPONENT_ORDER; do
    IFS='|' read -r repo dir group branch desc <<<"${STACK_COMPONENTS[$c]}"
    marker="download-only"
    [ -n "$group" ] && marker="group ${group}"
    echo -e "  ${GREEN}${c}${NC} — ${desc}"
    echo -e "      ${CYAN}${repo}${NC} → <parent-of-stack>/${dir} (${marker}, ${branch})"
  done
}

cmd_list() {
  echo -e "\n${BOLD}═══ Groups ═══${NC}"
  for num in 1 2 3 4 5; do
    local info_str="${STACK_GROUPS[$num]}"
    local name desc ram
    name=$(echo "$info_str" | cut -d'|' -f1)
    desc=$(echo "$info_str" | cut -d'|' -f2)
    ram=$(echo "$info_str" | cut -d'|' -f3)
    echo -e "  ${GREEN}${num}${NC} — ${BOLD}${name}${NC} (${desc}) ${ram}"
  done

  cmd_list_components

  echo -e "\n${BOLD}═══ Extensions ═══${NC}"
  for ext_dir in "${STACK_DIR}"/extensions/*/; do
    [ ! -f "$ext_dir/ext.yml" ] && continue
    local ext_name desc group
    ext_name=$(basename "$ext_dir")
    desc=$(grep "^description:" "$ext_dir/ext.yml" | cut -d: -f2- | xargs)
    group=$(grep "^group:" "$ext_dir/ext.yml" | cut -d: -f2 | xargs)
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

# ── Component verification ─────────────────────────────────────────────────────
# `verify` (and `up`, unless STACK_SKIP_VERIFY=1) checks that every component a
# group builds from is checked out, is a git checkout, and points at its repo.

component_field() { # <name> <field 1-5> → repo|dir|group|branch|description
  echo "${STACK_COMPONENTS[$1]}" | cut -d'|' -f"$2"
}

list_has() { # <word> [word...] → 0 when present
  local needle="$1"; shift
  local item
  for item in "$@"; do
    [ "$item" = "$needle" ] && return 0
  done
  return 1
}

# Expand component names and/or "all" into a validated, de-duplicated list,
# one name per line. Non-zero on an unknown component.
select_components() {
  local -a selected=()
  local t c
  for t in "$@"; do
    if [ "$t" = "all" ]; then
      for c in $STACK_COMPONENT_ORDER; do
        list_has "$c" ${selected[@]+"${selected[@]}"} || selected+=("$c")
      done
      continue
    fi
    if [ -z "${STACK_COMPONENTS[$t]:-}" ]; then
      err "Unknown component: ${t}"
      return 1
    fi
    list_has "$t" ${selected[@]+"${selected[@]}"} || selected+=("$t")
  done
  printf '%s\n' ${selected[@]+"${selected[@]}"}
}

# Verify one component checkout under <base>; $3=1 keeps the success line quiet.
#   0 = healthy · 1 = missing / not git / wrong repo (hard) · 2 = other branch (soft)
verify_component() {
  local name="$1" base="$2" quiet="${3:-0}"
  local repo dir group branch desc
  IFS='|' read -r repo dir group branch desc <<<"${STACK_COMPONENTS[$name]}"
  local target="${base}/${dir}"

  if [ ! -d "$target" ]; then
    echo -e "  ${RED}✗${NC} ${name} — missing ${target}"
    echo -e "      fix: ./stack.sh download ${name}"
    return 1
  fi
  if [ ! -d "${target}/.git" ]; then
    echo -e "  ${RED}✗${NC} ${name} — ${target} is not a git checkout"
    echo -e "      fix: move it aside, then ./stack.sh download ${name}"
    return 1
  fi

  local origin current status=0
  origin=$(git -C "$target" remote get-url origin 2>/dev/null || true)
  case "$origin" in
    *"${repo}"|*"${repo}.git")
      ;;
    "")
      echo -e "  ${RED}✗${NC} ${name} — no origin remote (expected ${repo})"
      status=1
      ;;
    *)
      echo -e "  ${RED}✗${NC} ${name} — origin ${origin} does not match ${repo}"
      status=1
      ;;
  esac

  current=$(git -C "$target" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  if [ "$current" != "$branch" ]; then
    echo -e "  ${YELLOW}!${NC} ${name} — on ${current:-detached HEAD}, registry branch is ${branch}"
    if [ "$status" -eq 0 ]; then status=2; fi
  fi

  if [ "$status" -eq 0 ] && [ "$quiet" != "1" ]; then
    echo -e "  ${GREEN}✓${NC} ${name} — ${repo} (${branch})"
  fi
  return "$status"
}

# Verify the components one group hosts. `up` uses this to fail fast: a hard
# failure aborts the start, a different branch is only a warning. Prints nothing
# (and returns 0) for a group that hosts no components.
verify_group_components() {
  local group="$1" base="${STACK_COMPONENT_BASE:-$(dirname "$STACK_DIR")}"
  local c rc count=0 hard=0 soft=0 missing=""
  for c in $STACK_COMPONENT_ORDER; do
    [ "$(component_field "$c" 3)" = "$group" ] || continue
    count=$((count+1))
    if verify_component "$c" "$base" 1; then rc=0; else rc=$?; fi
    if [ "$rc" -eq 1 ]; then
      hard=1
      missing="${missing} ${c}"
    fi
    if [ "$rc" -eq 2 ]; then soft=1; fi
  done

  [ "$count" -eq 0 ] && return 0
  if [ "$hard" -eq 1 ]; then
    err "Group ${group}: component checkout(s) missing or mismatched."
    err "    fix: ./stack.sh download${missing}"
    return 1
  fi
  if [ "$soft" -eq 1 ]; then
    warn "Some Group ${group} checkouts are on a branch other than the registry's (starting anyway)"
  fi
  return 0
}

cmd_verify_help() {
  echo "Usage: ./stack.sh verify [component...|all] [options]"
  echo ""
  echo "  Checks that each component is checked out next to this repo, is a git"
  echo "  checkout of its registry repository, and is on its registry branch."
  echo ""
  echo "Options:"
  echo "  --dir <path>       Parent directory holding the checkouts (default: $(dirname "$STACK_DIR"))"
  echo "  --list, -l         List the components"
  echo ""
  echo "Exits 1 when a checkout is missing or points at the wrong repository."
  echo "'up' runs this first; set STACK_SKIP_VERIFY=1 to skip it."
}

# ── Component verification (command) ───────────────────────────────────────────
cmd_verify() {
  local base=""
  local -a targets=()

  while [ $# -gt 0 ]; do
    case "$1" in
      --dir)     base="${2:?--dir needs a path}"; shift ;;
      --dir=*)   base="${1#--dir=}" ;;
      -l|--list) cmd_list_components; return 0 ;;
      -h|--help) cmd_verify_help; return 0 ;;
      -*)        err "Unknown option: $1"; cmd_verify_help; exit 1 ;;
      *)         targets+=("$1") ;;
    esac
    shift
  done

  if [ ${#targets[@]} -eq 0 ]; then targets=(all); fi
  # A local .env is optional here; it supplies STACK_COMPONENT_BASE when present.
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
  fi
  if [ -z "$base" ]; then
    base="${STACK_COMPONENT_BASE:-$(dirname "$STACK_DIR")}"
  fi

  local selected_list
  selected_list=$(select_components "${targets[@]}") || { cmd_list_components; exit 1; }
  local -a selected=()
  mapfile -t selected <<<"$selected_list"

  info "Verifying ${#selected[@]} component checkout(s) in ${base}"
  local c rc failed=0 warned=0
  for c in "${selected[@]}"; do
    if verify_component "$c" "$base"; then rc=0; else rc=$?; fi
    if [ "$rc" -eq 1 ]; then failed=$((failed+1)); fi
    if [ "$rc" -eq 2 ]; then warned=$((warned+1)); fi
  done

  if [ "$failed" -gt 0 ]; then
    err "${failed} of ${#selected[@]} component(s) need attention — see the fixes above"
    exit 1
  fi
  if [ "$warned" -gt 0 ]; then
    warn "${warned} of ${#selected[@]} checkout(s) are on a branch other than the registry's"
  fi
  ok "All ${#selected[@]} component checkout(s) verified"
}

# ── Attribution audit ──────────────────────────────────────────────────────────
# The guard (`.githooks/guard-lib`) is a gate: it judges the commits and file
# lines a push introduces, and the PR text. This command is the sweep that finds
# what is already *in* history — which the gate only re-examines when some later
# push makes it look further back (a branch's first push scans everything; an
# ordinary push scans only its own commits). Component names resolve to their
# checkouts exactly as `verify`/`download` do; the policy and the reporting live
# in scripts/audit-attribution.sh, so there is one implementation of each.

cmd_audit_help() {
  echo "Usage: ./stack.sh audit [component...|all] [--org] [options]"
  echo ""
  echo "  Audits attribution in git history with the policy the repos enforce in"
  echo "  their local hooks and in CI, and reports every offending commit with its"
  echo "  hash, date and reason. Reads only: fixing history is a reviewed act."
  echo ""
  echo "Options:"
  echo "  --org              every repository in the org that carries the guard"
  echo "  --dir <path>       Parent directory of the component checkouts"
  echo "                     (default: \$(dirname \"\$STACK_DIR\") or STACK_COMPONENT_BASE)"
  echo "  --since <date>     Only commits after a date, e.g. --since 2026-08-01"
  echo "  --limit <n>        At most n commits per repo, newest first (0 = all; default 500)"
  echo "  --content          Also scan tracked file contents, not just messages"
  echo "  --quiet            One line per repository"
  echo "  --json             Machine-readable report (for a dashboard or a job)"
  echo ""
  echo "  Exit: 0 clean, 1 violations, 2 usage error — usable in a scheduled job."
  echo ""
  echo "Examples:"
  echo "  ./stack.sh audit                        # this checkout"
  echo "  ./stack.sh audit all --limit 0          # every component, full history"
  echo "  ./stack.sh audit onyx distro --content"
  echo "  ./stack.sh audit --org --quiet          # the whole org, one line each"
}

cmd_audit() {
  local audit_script="${STACK_DIR}/scripts/audit-attribution.sh"
  if [ ! -f "$audit_script" ]; then
    err "audit script not found: ${audit_script}"
    exit 2
  fi

  local base="" org=0 rc=0 missing=0
  local -a targets=() pass=()

  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) cmd_audit_help; return 0 ;;
      --org)     org=1 ;;
      --dir)     base="${2:?--dir needs a path}"; shift ;;
      --dir=*)   base="${1#--dir=}" ;;
      --since)   pass+=(--since "${2:?--since needs a date}"); shift ;;
      --limit)   pass+=(--limit "${2:?--limit needs a number}"); shift ;;
      --since=*|--limit=*|--content|--quiet|--json) pass+=("$1") ;;
      -*)        err "Unknown option: $1"; cmd_audit_help; exit 1 ;;
      *)         targets+=("$1") ;;
    esac
    shift
  done

  # A local .env is optional; it can supply STACK_COMPONENT_BASE, as for verify.
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
  fi
  [ -n "$base" ] || base="${STACK_COMPONENT_BASE:-$(dirname "$STACK_DIR")}"

  if [ "$org" -eq 1 ]; then
    info "Attribution audit — every repo in the org that carries the guard"
    bash "$audit_script" ${pass[@]+"${pass[@]}"} --org || rc=$?
    return "$rc"
  fi

  # No argument means this checkout, the same as running the script here.
  if [ ${#targets[@]} -eq 0 ]; then
    bash "$audit_script" ${pass[@]+"${pass[@]}"} "$STACK_DIR" || rc=$?
    return "$rc"
  fi

  local selected_list
  selected_list=$(select_components "${targets[@]}") || { cmd_list_components; exit 1; }

  local -a dirs=() c target
  while read -r c; do
    [ -n "$c" ] || continue
    target="${base}/$(component_field "$c" 2)"
    if [ ! -d "${target}/.git" ]; then
      warn "${c} — no git checkout at ${target} (fix: ./stack.sh download ${c})"
      missing=$((missing + 1))
      continue
    fi
    dirs+=("$target")
  done <<<"$selected_list"

  if [ ${#dirs[@]} -eq 0 ]; then
    err "no component checkouts to audit under ${base}"
    exit 1
  fi

  info "Attribution audit — ${#dirs[@]} component checkout(s) in ${base}"
  bash "$audit_script" ${pass[@]+"${pass[@]}"} "${dirs[@]}" || rc=$?
  if [ "$missing" -gt 0 ]; then
    warn "${missing} selected component(s) were not checked out and were skipped"
  fi
  return "$rc"
}

cmd_download_help() {
  echo "Usage: ./stack.sh download <component...|all> [options]"
  echo ""
  echo "  Clones (or updates) the stack's component repositories as siblings of"
  echo "  this checkout — the locations the group compose files build from."
  echo ""
  echo "Options:"
  echo "  --start, -s        Start each selected component's group afterwards"
  echo "  --ref <ref>        Branch, tag, or commit to check out (default: per-component)"
  echo "  --dir <path>       Parent directory to download into (default: $(dirname "$STACK_DIR"))"
  echo "  --list, -l         List the components"
  echo ""
  echo "Examples:"
  echo "  ./stack.sh download all                # every component"
  echo "  ./stack.sh download plutus olympus     # only the new additions"
  echo "  ./stack.sh download all --start        # download, then bring the stack up"
  echo "  ./stack.sh download cerulean --ref v0.3.0"
}

# ── Component download ─────────────────────────────────────────────────────────
# Clone the component repos next to this checkout, or update the ones already
# present, then optionally start the groups that host them.
cmd_download() {
  local do_start=0 ref="" base=""
  local -a targets=()

  while [ $# -gt 0 ]; do
    case "$1" in
      -s|--start) do_start=1 ;;
      --ref)      ref="${2:?--ref needs a branch, tag, or commit}"; shift ;;
      --ref=*)    ref="${1#--ref=}" ;;
      --dir)      base="${2:?--dir needs a path}"; shift ;;
      --dir=*)    base="${1#--dir=}" ;;
      -l|--list)  cmd_list_components; return 0 ;;
      -h|--help)  cmd_download_help; return 0 ;;
      -*)         err "Unknown option: $1"; cmd_download_help; exit 1 ;;
      *)          targets+=("$1") ;;
    esac
    shift
  done

  if [ ${#targets[@]} -eq 0 ]; then
    err "Nothing to download — name one or more components, or 'all'."
    cmd_download_help
    exit 1
  fi

  # A local .env is optional for downloading (no configuration is needed to
  # clone), but when it exists it supplies STACK_COMPONENT_BASE / GITHUB_BASE.
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
  fi
  if [ -z "$base" ]; then
    base="${STACK_COMPONENT_BASE:-$(dirname "$STACK_DIR")}"
  fi
  # Let --start verify the same directory we just downloaded into (unless the
  # .env names another one, which is the operator's configured location).
  export STACK_COMPONENT_BASE="$base"

  # Expand "all" and validate names, preserving order and dropping duplicates.
  local selected_list
  selected_list=$(select_components "${targets[@]}") || { cmd_list_components; exit 1; }
  local -a selected=()
  mapfile -t selected <<<"$selected_list"

  command -v git >/dev/null 2>&1 || { err "git is required to download components"; exit 1; }
  [ -d "$base" ] || mkdir -p "$base"

  local repo dir group branch desc use_ref target failed=0
  info "Downloading ${#selected[@]} component(s) into ${base}"
  for c in "${selected[@]}"; do
    IFS='|' read -r repo dir group branch desc <<<"${STACK_COMPONENTS[$c]}"
    use_ref="${ref:-${branch:-main}}"
    target="${base}/${dir}"

    if [ -d "${target}/.git" ]; then
      info "  ${c}: updating ${dir} (${use_ref})"
      git -C "$target" fetch --quiet --prune --tags origin || {
        warn "  ${c}: could not fetch — leaving ${target} as-is"
        continue
      }
      if ! git -C "$target" diff --quiet || ! git -C "$target" diff --cached --quiet; then
        warn "  ${c}: local changes in ${target} — leaving the checkout alone"
        continue
      fi
      git -C "$target" checkout --quiet "$use_ref" 2>/dev/null \
        || git -C "$target" checkout --quiet -B "$use_ref" "origin/${use_ref}"
      git -C "$target" pull --quiet --ff-only origin "$use_ref" \
        || warn "  ${c}: ${use_ref} is not a fast-forward — review ${target}"
    elif [ -e "$target" ]; then
      warn "  ${c}: ${target} exists but is not a git checkout — skipping"
    else
      info "  ${c}: cloning ${repo} → ${target}"
      git clone --quiet --branch "$use_ref" "${GITHUB_BASE:-https://github.com}/${repo}.git" "$target" || {
        warn "  ${c}: clone failed (${repo}) — continuing with the rest"
        failed=1
        continue
      }
    fi
  done

  if [ "$failed" -eq 1 ]; then
    err "One or more components could not be downloaded — fix the above and re-run."
    exit 1
  fi
  ok "Download complete"

  if [ "$do_start" -eq 1 ]; then
    ensure_env
    local -a groups=()
    for c in "${selected[@]}"; do
      group=$(component_field "$c" 3)
      [ -z "$group" ] && continue
      list_has "$group" ${groups[@]+"${groups[@]}"} || groups+=("$group")
    done

    if [ ${#groups[@]} -eq 0 ]; then
      warn "None of the selected components belong to a group — nothing to start"
      return 0
    fi

    local -a ordered=()
    mapfile -t ordered < <(printf '%s\n' "${groups[@]}" | sort -n)
    info "Starting group(s): ${ordered[*]}"
    for group in "${ordered[@]}"; do
      cmd_up "$group"
    done
  fi
}

cmd_migrate() {
  local sub="${1:?Usage: stack.sh migrate pack <group> --stop --out DIR | stack.sh migrate restore --in BUNDLE [--force]}"
  shift
  exec python3 "${STACK_DIR}/scripts/migrate-stack.py" "$sub" "$@"
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
  download|fetch) cmd_download "$@" ;;
  verify)   cmd_verify "$@" ;;
  audit)    cmd_audit "$@" ;;
  mesh)     cmd_mesh ;;
  discover) cmd_discover "$@" ;;
  register) cmd_register "$@" ;;
  migrate)  cmd_migrate "$@" ;;
  help|--help|-h)
    echo -e "${BOLD}Innotel Platform Stack — Unified Orchestrator${NC}\n"
    echo -e "Usage: ./stack.sh <command> [args]\n"
    echo "Commands:"
    echo "  up      [group|all]   Start group(s) and extensions"
    echo "          e.g. ./stack.sh up 2           (group 2 alone)"
    echo "               ./stack.sh up 3 4         (groups 3+4 on one server)"
    echo "               ./stack.sh up plutus      (a component's group)"
    echo "               ./stack.sh up all         (everything, single server)"
    echo "  down    [group|all]   Stop group(s)"
    echo "  status  [group|all]   Show running services"
    echo "  logs    [group]       Tail group logs"
    echo "  enable  <ext> [group] Enable an extension"
    echo "  disable <ext> [group] Disable an extension"
    echo "  list                  List groups, components, extensions, services"
    echo "  download <component...|all> [--start] [--ref <ref>] [--dir <path>]"
    echo "                        Clone/update component repos next to this checkout"
    echo "          e.g. ./stack.sh download all --start"
    echo "               ./stack.sh download plutus olympus"
    echo "  verify  [component...|all]  Check component checkouts before starting"
    echo "                        e.g. ./stack.sh verify all"
    echo "  audit   [component...|all] [--org]  Audit attribution in git history"
    echo "                        e.g. ./stack.sh audit all --limit 0     (full history)"
    echo "                             ./stack.sh audit --org            (whole org)"
    echo "  mesh                  Start mesh network only"
    echo "  discover <service>    Find a service across all groups"
    echo "  register <svc> <addr> <port> [tags]  Register a service"
    echo "  migrate pack <group> --stop --out DIR   Bundle a stack: volumes, envs, vault secrets, images"
    echo "  migrate restore --in BUNDLE [--force]   Rebuild that stack here from the bundle"
    ;;
  *)
    err "Unknown command: $cmd"
    echo "Run: ./stack.sh help"
    exit 1
    ;;
esac
