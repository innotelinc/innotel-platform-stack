#!/bin/bash
#
# rotate-regcache-pat.sh — install a new Docker Hub token into the `regcache`
# pull-through registry, and take it out of the container's environment.
#
# `regcache` (192.168.1.79, on i1) runs one Docker container, `registry:2`,
# proxying registry-1.docker.io. The Hub credential has always been a
# REGISTRY_PROXY_PASSWORD baked into the container's Config.Env: visible to
# anything that can run `docker inspect` there, and carried forward by every
# restart. This script is the whole rotation, in one run:
#
#   1. proves the new token actually authenticates against the Hub
#   2. stores it in Vault (optional, see --vault-token-file)
#   3. rewrites the container's environment into a 0600 file, password included
#   4. recreates `registry` from the spec it just read, using --env-file
#   5. verifies: env no longer carries the secret, file is 0600, cache serves 200
#
# The old container's environment is kept in a 0600 file until those checks
# pass; any failure puts the old container back exactly as it was. A rotation
# that cannot be verified is not left half-applied.
#
# Usage (as root, on i1):
#   bash platform-stack/scripts/rotate-regcache-pat.sh --pat-file /root/new-hub-pat
#   bash platform-stack/scripts/rotate-regcache-pat.sh --pat-file /root/new-hub-pat \
#        --vault-token-file /root/vault.token
#
# Options:
#   --pat-file PATH        file holding the new token (required; a trailing
#                          newline is stripped, nothing else is accepted)
#   --vault-token-file P   Vault token allowed to write the path; without it the
#                          Vault step is skipped and said so out loud
#   --vault-path PATH      default cerulean/regcache
#   --vault-addr URL       default https://127.0.0.1:8200
#   --username NAME        Hub account, default innotel
#   --container NAME       Docker container, default registry
#   --incus NAME           Incus container holding it, default regcache
#   --skip-hub-check       install without proving the token first (offline use)
#   --dry-run              print the plan and change nothing
#
# Exit: 0 rotated and verified, 1 failed (and rolled back), 2 could not run.
#
# Why Vault is optional rather than required: on 2026-11-13 every one of the
# fifteen product tokens on the estate returned `deny` for creating
# secret/data/cerulean/regcache, so that half cannot be done by this script
# alone. Run with --vault-token-file once a token with write on that path
# exists; until then the token is still rotated here, which is the security
# half that matters.

set -uo pipefail

PAT_FILE=""
VAULT_TOKEN_FILE=""
VAULT_PATH="cerulean/regcache"
VAULT_ADDR_DEFAULT="https://127.0.0.1:8200"
VAULT_ADDR_ARG=""
USERNAME="innotel"
CONTAINER="registry"
INCUS="regcache"
SKIP_HUB_CHECK=0
DRY_RUN=0

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf '[%s] FATAL: %s\n' "$(date '+%F %T')" "$*" >&2; exit 1; }
cannot() { printf '[%s] CANNOT RUN: %s\n' "$(date '+%F %T')" "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --pat-file)           [ $# -ge 2 ] || cannot "--pat-file needs a path"; PAT_FILE="$2"; shift 2 ;;
    --vault-token-file)   [ $# -ge 2 ] || cannot "--vault-token-file needs a path"; VAULT_TOKEN_FILE="$2"; shift 2 ;;
    --vault-path)         [ $# -ge 2 ] || cannot "--vault-path needs a value"; VAULT_PATH="$2"; shift 2 ;;
    --vault-addr)         [ $# -ge 2 ] || cannot "--vault-addr needs a URL"; VAULT_ADDR_ARG="$2"; shift 2 ;;
    --username)           [ $# -ge 2 ] || cannot "--username needs a value"; USERNAME="$2"; shift 2 ;;
    --container)          [ $# -ge 2 ] || cannot "--container needs a name"; CONTAINER="$2"; shift 2 ;;
    --incus)              [ $# -ge 2 ] || cannot "--incus needs a name"; INCUS="$2"; shift 2 ;;
    --skip-hub-check)     SKIP_HUB_CHECK=1; shift ;;
    --dry-run)            DRY_RUN=1; shift ;;
    -h|--help)            sed -n '2,52p' "$0"; exit 0 ;;
    *)                    echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# ── Preconditions ─────────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || cannot "run as root on i1, the host that holds $INCUS"
command -v incus >/dev/null 2>&1 || cannot "missing required command: incus"
[ -n "$PAT_FILE" ] || cannot "--pat-file is required (the new Docker Hub token)"
[ -r "$PAT_FILE" ] || cannot "cannot read $PAT_FILE"
command -v curl >/dev/null 2>&1 || cannot "missing required command: curl"

# The token is read once, newline-stripped, and never echoed or passed on a
# command line where `ps` would show it.
NEW_PAT="$(tr -d '\r\n' < "$PAT_FILE")"
[ -n "$NEW_PAT" ] || cannot "$PAT_FILE is empty"
case "$NEW_PAT" in
  *[[:space:]]*) cannot "$PAT_FILE holds whitespace -- one token per file, nothing else" ;;
esac
# sed is used to substitute the token later, so a token that sed would mangle is
# refused here rather than written into a broken environment file.
case "$NEW_PAT" in
  *'|'*|*'&'*|*'\'*) cannot "$PAT_FILE holds |, & or a backslash, which this script cannot substitute safely" ;;
esac

VCMD="timeout 60 incus exec $INCUS -- docker"
$VCMD inspect "$CONTAINER" >/dev/null 2>&1 \
  || cannot "container $CONTAINER not found in $INCUS -- nothing was changed"

# ── 1. Does the token actually work? ──────────────────────────────────────────
if [ "$SKIP_HUB_CHECK" -eq 0 ]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 25 \
    -u "$USERNAME:$NEW_PAT" https://registry-1.docker.io/v2/ 2>/dev/null)"
  case "$code" in
    200) log "hub check: the token authenticates as $USERNAME" ;;
    401) die "hub check: the Hub rejected this token (401) -- nothing was changed" ;;
    *)   die "hub check: could not confirm the token (HTTP ${code:-none}) -- nothing was changed; --skip-hub-check overrides" ;;
  esac
else
  log "hub check: skipped on request"
fi

# ── 2. Vault, when a token that may write the path is supplied ────────────────
if [ -n "$VAULT_TOKEN_FILE" ]; then
  [ -r "$VAULT_TOKEN_FILE" ] || cannot "cannot read $VAULT_TOKEN_FILE"
  command -v vault >/dev/null 2>&1 || cannot "no vault CLI here; store the token on the host that has one, then re-run with --vault-token-file"
  VAULT_ADDR="${VAULT_ADDR_ARG:-$VAULT_ADDR_DEFAULT}"
  export VAULT_ADDR
  VAULT_TOKEN="$(tr -d '\r\n' < "$VAULT_TOKEN_FILE")"
  export VAULT_TOKEN
  caps="$(vault token capabilities "secret/data/$VAULT_PATH" 2>/dev/null | tr -d '\n')"
  case " $caps " in
    *" create "*|*" update "*) log "vault: token may write $VAULT_PATH (caps:$caps)" ;;
    *) cannot "this Vault token cannot write secret/data/$VAULT_PATH (caps: ${caps:-none}) -- nothing was changed" ;;
  esac
  if [ "$DRY_RUN" -eq 1 ]; then
    log "dry run: would write username/password to $VAULT_PATH"
  else
    vault kv put "$VAULT_PATH" username="$USERNAME" password="$NEW_PAT" >/dev/null \
      || die "vault write to $VAULT_PATH failed -- nothing was changed"
    log "vault: stored $VAULT_PATH"
  fi
else
  log "vault: skipped (no --vault-token-file); the token is rotated but not recorded in Vault"
fi

# ── 3. Read the running spec, so the rebuild is not a guess ───────────────────
IMG="$($VCMD inspect -f '{{.Config.Image}}' "$CONTAINER" | tr -d '\r')"
RESTART="$($VCMD inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" | tr -d '\r')"
NETMODE="$($VCMD inspect -f '{{.HostConfig.NetworkMode}}' "$CONTAINER" | tr -d '\r')"
PORTS="$($VCMD inspect -f '{{range $p, $c := .HostConfig.PortBindings}}{{$p}}={{range $c}}{{.HostPort}}{{end}} {{end}}' "$CONTAINER" | tr -d '\r')"
BINDS="$($VCMD inspect -f '{{range .HostConfig.Binds}}{{.}} {{end}}' "$CONTAINER" | tr -d '\r')"
ENV_LINES="$($VCMD inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" | tr -d '\r')"

[ -n "$IMG" ] || cannot "could not read $CONTAINER's image -- nothing was changed"
case "$PORTS" in *' '*|'') : ;; esac

log "current: image=$IMG restart=${RESTART:-none} net=${NETMODE:-default}"
log "ports: ${PORTS:-none}"

build_args() {
  # Rebuild the docker run arguments from the captured spec. Binds and ports
  # are validated rather than trusted: a bind containing a comma cannot be
  # re-expressed as a -v argument safely, so this refuses instead of guessing.
  local envfile="$1"
  ARGS=(run -d --name "$CONTAINER")
  [ -n "$RESTART" ] && ARGS+=(--restart "$RESTART")
  [ -n "$NETMODE" ] && [ "$NETMODE" != "default" ] && ARGS+=(--network "$NETMODE")
  for b in $BINDS; do
    case "$b" in
      *,*) cannot "bind '$b' contains a comma; recreate it by hand" ;;
      *)   ARGS+=(-v "$b") ;;
    esac
  done
  for p in $PORTS; do
    cport="${p%%=*}"; hport="${p#*=}"
    cport="${cport%/tcp}"
    [ -n "$hport" ] && ARGS+=(-p "${hport}:${cport}")
  done
  ARGS+=(--env-file "$envfile" "$IMG")
}

ENV_FILE=/root/registry.env
OLD_ENV_FILE=/root/registry.env.pre-rotation

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--- would run ---"
  echo "docker run -d --name $CONTAINER --restart ${RESTART:-no} -p <captured> -v <captured> --env-file $ENV_FILE $IMG"
  echo "--- env that would be written (password replaced) ---"
  printf '%s\n' "$ENV_LINES" | sed 's/^\(REGISTRY_PROXY_PASSWORD=\).*/\1<new token>/'
  exit 0
fi

# ── 4. Prepare rollback, then swap ────────────────────────────────────────────
# The old environment is written first: if the rebuild or its checks fail, the
# old container comes back byte-identical to how it was found.
printf '%s\n' "$ENV_LINES" > /tmp/registry.env.old || die "cannot stage the old environment"
timeout 60 incus file push /tmp/registry.env.old "$INCUS$OLD_ENV_FILE" >/dev/null 2>&1 \
  || die "cannot stage the old environment inside $INCUS"
timeout 40 incus exec "$INCUS" -- sh -c "chmod 600 $OLD_ENV_FILE" || die "cannot chmod the staged environment"
rm -f /tmp/registry.env.old

ROLLED_BACK=0
rollback() {
  [ "$ROLLED_BACK" -eq 1 ] && return
  ROLLED_BACK=1
  log "rolling back to the previous container"
  $VCMD rm -f "$CONTAINER" >/dev/null 2>&1 || true
  ARGS=(); build_args "$OLD_ENV_FILE"
  $VCMD "${ARGS[@]}" >/dev/null 2>&1 \
    && log "old container is back" \
    || log "WARNING: rollback failed -- recreate $CONTAINER from $OLD_ENV_FILE by hand"
}

trap 'if [ $? -ne 0 ] && [ "$ROLLED_BACK" -eq 0 ]; then rollback; fi' EXIT

# New environment: every existing variable, with only the password replaced.
printf '%s\n' "$ENV_LINES" \
  | sed "s|^REGISTRY_PROXY_PASSWORD=.*|REGISTRY_PROXY_PASSWORD=${NEW_PAT}|" \
  > /tmp/registry.env.new
grep -qxF "REGISTRY_PROXY_PASSWORD=${NEW_PAT}" /tmp/registry.env.new \
  || die "could not substitute the password into the environment"
timeout 60 incus file push /tmp/registry.env.new "$INCUS$ENV_FILE" >/dev/null 2>&1 \
  || die "cannot write $ENV_FILE inside $INCUS"
rm -f /tmp/registry.env.new
timeout 40 incus exec "$INCUS" -- sh -c "chmod 600 $ENV_FILE" || die "cannot chmod $ENV_FILE"

# The rebuild arguments are built and validated before anything is stopped, so
# a spec this script cannot express leaves the running cache untouched.
ARGS=(); build_args "$ENV_FILE"

log "recreating $CONTAINER with the environment in $ENV_FILE (0600)"
$VCMD rm -f "$CONTAINER" >/dev/null 2>&1 || die "could not stop the old container"
$VCMD "${ARGS[@]}" >/dev/null || die "docker run failed"

# ── 5. Verify, or put it back ─────────────────────────────────────────────────
sleep 3
env_hits="$($VCMD inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" 2>/dev/null \
  | grep -c '^REGISTRY_PROXY_PASSWORD=' || true)"
[ "${env_hits:-1}" -eq 0 ] || die "the new container still carries the password in its environment"

mode="$(timeout 30 incus exec "$INCUS" -- stat -c %a "$ENV_FILE" 2>/dev/null | tr -d '\r')"
case "$mode" in
  600)  log "verified: $ENV_FILE is mode 600" ;;
  '')   log "warning: could not read the mode of $ENV_FILE (no stat in $INCUS)" ;;
  *)    die "environment file is mode $mode, expected 600" ;;
esac

v2="$(timeout 30 incus exec "$INCUS" -- sh -c 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/v2/' 2>/dev/null | tr -d '\r')"
[ "$v2" = "200" ] || die "cache answered $v2 on /v2/, expected 200"

pull="$(timeout 60 incus exec "$INCUS" -- sh -c 'curl -s -o /dev/null -w "%{http_code}" -H "Accept: application/vnd.docker.distribution.manifest.v2+json" http://127.0.0.1:5000/v2/library/alpine/manifests/latest' 2>/dev/null | tr -d '\r')"
case "$pull" in
  200) log "verified: cache serves /v2/ 200 and resolves alpine through the Hub" ;;
  401) die "the cache cannot authenticate upstream (401) -- was the token scoped for pulls?" ;;
  *)   die "pull-through check answered ${pull:-nothing}, expected 200" ;;
esac

ROLLED_BACK=1   # success: the trap must not undo it
log "rotated. revoke the old token in the Hub UI now; it is still valid until you do"
log "the new token lives in $ENV_FILE (0600) on $INCUS, and in Vault at $VAULT_PATH${VAULT_TOKEN_FILE:+}"
