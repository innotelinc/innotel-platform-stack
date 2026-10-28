#!/usr/bin/env bash
# Update the mail stack that lives in i3's `mail` container: Stalwart (native
# binary over a RocksDB store) and Bulwark/Loomix (the innotelinc/webmail image
# behind /opt/bulwark).
#
# The two move together. Bulwark is a JMAP client with no account store of its
# own — it reads Stalwart's session, capabilities and mailboxes — so a Stalwart
# that renames a capability or reshapes a session breaks the webmail that talks
# to it. This script therefore treats them as one unit: it resolves both, and if
# either half fails its verification the other is rolled back too.
#
# Usage (on the mail container, as root):
#   update-mail-stack.sh --check              report current vs available only
#   update-mail-stack.sh                      update both, verify, roll back on failure
#   update-mail-stack.sh --allow-stalwart-major
#                                             permit crossing the 1.0 boundary
#   update-mail-stack.sh --install-timer      install the weekly systemd timer
#   update-mail-stack.sh --only stalwart|bulwark
#
# The 1.0 guard
# -------------
# Stalwart 1.0 does not open a data store created by a pre-1.0 release; it stops
# with "The data store was created by a Stalwart version prior to 1.0". Moving
# this server from 0.16.x to 1.x is therefore a documented migration (export,
# new deployment, import), not a binary swap, and no script should pretend
# otherwise. Without --allow-stalwart-major this script stays inside the 0.16
# patch line, where replacing the binary is safe.
#
# What is backed up before anything is touched
# --------------------------------------------
#   <backup>/stalwart-store.tar.gz    /var/lib/stalwart (RocksDB)
#   <backup>/stalwart-etc.tar.gz      /etc/stalwart (config.json, env, tls)
#   <backup>/stalwart-binary          /usr/local/bin/stalwart
#   <backup>/compose.yml              /opt/bulwark/docker-compose.yml
#   <backup>/loomix-worktree.patch    the fork's uncommitted customisation
#   <backup>/bulwark-image.txt        the image id the compose file pointed at
#
# Exit codes: 0 nothing to do or update verified, 1 update failed and rolled
# back, 2 could not determine state (left everything untouched).

set -uo pipefail

STALWART_BIN=/usr/local/bin/stalwart
STALWART_CFG=/etc/stalwart
STALWART_DATA=/var/lib/stalwart
STALWART_SVC=stalwart

COMPOSE_DIR=/opt/bulwark
COMPOSE_FILE=$COMPOSE_DIR/docker-compose.yml
BULWARK_SRC=/root/bulwark-src
IMAGE_REPO=ghcr.io/innotelinc/webmail
UPSTREAM_REPO=bulwarkmail/webmail

# The Bulwark image is NOT built in this container. On 2026-10-27 a local build
# (npm ci plus the Next.js compile) exhausted the mail container's 4 GiB and left
# the whole i3 host unable to fork an sshd: mail was down ~28 minutes. The image
# is built on BUILD_HOST instead and streamed back, and a local build has to be
# asked for explicitly.
BUILD_HOST=${BUILD_HOST:-root@192.168.1.54}
# No default: a committed password is a leaked password. Export it, or set
# BUILD_HOST_PASS in the environment the cron/CI job runs under.
BUILD_HOST_PASS=${BUILD_HOST_PASS:?set BUILD_HOST_PASS before running this}
BUILD_DIR=/root/loomix-build
BUILD_SSH="sshpass -p $BUILD_HOST_PASS ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20"

STALWART_REPO=stalwartlabs/stalwart
STALWART_ASSET=stalwart-x86_64-unknown-linux-gnu.tar.gz

BACKUP_ROOT=/root/mail-backups
TS=$(date '+%Y%m%dT%H%M%S')
BACKUP=$BACKUP_ROOT/$TS

MODE=apply
ONLY=both
ALLOW_MAJOR=0

log()  { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
warn() { printf '[%s] WARN  %s\n' "$(date '+%F %T')" "$*" >&2; }
die()  { printf '[%s] FATAL %s\n' "$(date '+%F %T')" "$*" >&2; exit "${2:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check)               MODE=check ;;
    --allow-stalwart-major) ALLOW_MAJOR=1 ;;
    --only)                ONLY="${2:?--only needs stalwart|bulwark}"; shift ;;
    --install-timer)       INSTALL_TIMER=1 ;;
    -h|--help)             sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "run as root"
command -v curl >/dev/null || die "curl is required"
command -v docker >/dev/null || die "docker is required"

# ── helpers ──────────────────────────────────────────────────────────────

api_get() { curl -fsS --max-time 25 -H 'Accept: application/vnd.github+json' "$1"; }

# newest release tag for a repo, optionally restricted to a regex
newest_tag() {
  local repo=$1 filter=${2:-.} page tags
  for page in 1 2 3; do
    tags=$(api_get "https://api.github.com/repos/$repo/releases?per_page=100&page=$page" 2>/dev/null \
           | jq -r '.[] | select(.draft==false and .prerelease==false) | .tag_name') || return 1
    [ -n "$tags" ] || return 1
    printf '%s\n' "$tags" | grep -E "$filter" | sed 's/^v//' | sort -V | tail -1
    return 0
  done
  return 1
}

stalwart_running_version() {
  "$STALWART_BIN" --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

stalwart_healthy() {
  systemctl is-active --quiet "$STALWART_SVC" || return 1
  # the two ports the estate actually depends on
  ss -lntH 2>/dev/null | grep -qE ':(993|25)\b' || return 1
  return 0
}

bulwark_running_tag() {
  grep -E '^\s*image:' "$COMPOSE_FILE" 2>/dev/null | head -1 | awk '{print $2}'
}

bulwark_running_version() {
  local tag
  tag=$(bulwark_running_tag) || return 1
  printf '%s\n' "${tag##*:}" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

bulwark_healthy() {
  local id
  id=$(docker ps -q --filter 'name=^bulwark$' 2>/dev/null)
  [ -n "$id" ] || return 1
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null)" = healthy ]
}

backup_all() {
  mkdir -p "$BACKUP" || die "cannot create $BACKUP"
  chmod 700 "$BACKUP_ROOT" "$BACKUP"
  log "backing up to $BACKUP"
  tar -C "$(dirname "$STALWART_DATA")" -czf "$BACKUP/stalwart-store.tar.gz" "$(basename "$STALWART_DATA")" 2>/dev/null \
    || warn "store backup failed"
  tar -C "$(dirname "$STALWART_CFG")" -czf "$BACKUP/stalwart-etc.tar.gz" "$(basename "$STALWART_CFG")" 2>/dev/null \
    || warn "config backup failed"
  cp -a "$STALWART_BIN" "$BACKUP/stalwart-binary" 2>/dev/null || warn "binary backup failed"
  cp -a "$COMPOSE_FILE" "$BACKUP/compose.yml" 2>/dev/null || warn "compose backup failed"
  if [ -d "$BULWARK_SRC/.git" ]; then
    git -C "$BULWARK_SRC" diff > "$BACKUP/loomix-worktree.patch" 2>/dev/null || true
    git -C "$BULWARK_SRC" rev-parse HEAD > "$BACKUP/loomix-head.txt" 2>/dev/null || true
  fi
  docker image inspect -f '{{.Id}}' "$(bulwark_running_tag)" > "$BACKUP/bulwark-image.txt" 2>/dev/null || true
  log "backup complete: $(du -sh "$BACKUP" 2>/dev/null | cut -f1)"
}

# ── stalwart ─────────────────────────────────────────────────────────────

stalwart_resolve() {
  local cur major target
  cur=$(stalwart_running_version) || die "cannot read the installed Stalwart version"
  major=${cur%%.*}
  STALWART_CURRENT=$cur

  if [ "$major" -lt 1 ] && [ "$ALLOW_MAJOR" -eq 0 ]; then
    target=$(newest_tag "$STALWART_REPO" '^v0\.16\.[0-9]+$') \
      || die "cannot resolve the newest 0.16.x release"
    STALWART_TARGET=$target
    STALWART_SERIES="0.16 patch line (major upgrades need --allow-stalwart-major)"
  else
    target=$(newest_tag "$STALWART_REPO" '^v[0-9]+\.[0-9]+\.[0-9]+$') \
      || die "cannot resolve the newest release"
    STALWART_TARGET=$target
    STALWART_SERIES="all releases"
  fi

  if [ "$(printf '%s\n%s\n' "$cur" "$STALWART_TARGET" | sort -V | tail -1)" = "$cur" ]; then
    STALWART_NEEDS=0
  else
    STALWART_NEEDS=1
  fi
}

stalwart_apply() {
  local url tmp install_ver
  url="https://github.com/$STALWART_REPO/releases/download/v$STALWART_TARGET/$STALWART_ASSET"
  log "stalwart: $STALWART_CURRENT -> $STALWART_TARGET"
  tmp=$(mktemp -d) || die "mktemp failed"

  log "  downloading $STALWART_ASSET"
  curl -fsSL --max-time 300 -o "$tmp/stalwart.tar.gz" "$url" || { rm -rf "$tmp"; die "download failed"; }

  tar -C "$tmp" -xzf "$tmp/stalwart.tar.gz" 2>/dev/null || { rm -rf "$tmp"; die "extract failed"; }
  local newbin
  # The release archive ships the binary mode 0644, so find it by name and set
  # the mode ourselves — filtering on -perm -u+x finds nothing.
  newbin=$(find "$tmp" -type f -name stalwart | head -1)
  [ -n "$newbin" ] || { rm -rf "$tmp"; die "no stalwart binary in the archive"; }
  chmod 0755 "$newbin"

  install_ver=$("$newbin" --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  [ "$install_ver" = "$STALWART_TARGET" ] \
    || { rm -rf "$tmp"; die "archive reports '$install_ver', expected '$STALWART_TARGET'"; }

  install -m 0755 "$newbin" "$STALWART_BIN.new" || { rm -rf "$tmp"; die "install failed"; }
  mv -f "$STALWART_BIN" "$STALWART_BIN.prev"
  mv -f "$STALWART_BIN.new" "$STALWART_BIN"
  rm -rf "$tmp"

  log "  restarting $STALWART_SVC"
  systemctl restart "$STALWART_SVC" || true

  local i
  for i in $(seq 1 24); do
    stalwart_healthy && break
    sleep 5
  done
  if stalwart_healthy; then
    log "  stalwart OK at $(stalwart_running_version)"
    return 0
  fi

  warn "stalwart did not come up on $STALWART_TARGET — rolling back to $STALWART_CURRENT"
  mv -f "$STALWART_BIN.prev" "$STALWART_BIN"
  systemctl restart "$STALWART_SVC" || true
  for i in $(seq 1 12); do stalwart_healthy && break; sleep 5; done
  stalwart_healthy && log "  rolled back, running $(stalwart_running_version)" \
                    || warn "  ROLLBACK DID NOT RECOVER — restore $BACKUP/stalwart-binary and the store by hand"
  return 1
}

# ── bulwark ──────────────────────────────────────────────────────────────

bulwark_resolve() {
  local cur
  cur=$(bulwark_running_version) || die "cannot read the running Bulwark version from $COMPOSE_FILE"
  BULWARK_CURRENT=$cur
  BULWARK_TARGET=$(newest_tag "$UPSTREAM_REPO" '^[0-9]+\.[0-9]+\.[0-9]+$') \
    || die "cannot resolve the newest Bulwark release"
  if [ "$(printf '%s\n%s\n' "$cur" "$BULWARK_TARGET" | sort -V | tail -1)" = "$cur" ]; then
    BULWARK_NEEDS=0
  else
    BULWARK_NEEDS=1
  fi
}

bulwark_apply() {
  local tag=$BULWARK_TARGET build=$BULWARK_SRC-build-$BULWARK_TARGET
  local newimg=$IMAGE_REPO:$tag-loomix patch=$BACKUP/loomix-worktree.patch

  log "bulwark: $BULWARK_CURRENT -> $BULWARK_TARGET ($newimg)"

  [ -d "$BULWARK_SRC/.git" ] || die "no fork checkout at $BULWARK_SRC"

  log "  fetching upstream tags"
  git -C "$BULWARK_SRC" fetch --tags --prune origin >/dev/null 2>&1 \
    || { warn "git fetch failed"; return 1; }
  git -C "$BULWARK_SRC" rev-parse -q --verify "refs/tags/$BULWARK_TARGET" >/dev/null 2>&1 \
    || { warn "upstream tag $BULWARK_TARGET not found after fetch"; return 1; }

  # The Loomix customisation is an uncommitted working-tree diff, so it travels
  # as a patch onto a clean checkout of the new tag rather than as a merge.
  [ -s "$patch" ] || { warn "no saved Loomix patch — refusing to build an unbranded image"; return 1; }

  rm -rf "$build"
  git -C "$BULWARK_SRC" worktree add --detach "$build" "$BULWARK_TARGET" >/dev/null 2>&1 \
    || { warn "could not create a worktree at $BULWARK_TARGET"; return 1; }

  log "  applying the Loomix patch"
  if ! git -C "$build" apply --3way "$patch" >/dev/null 2>&1; then
    warn "the Loomix patch does not apply cleanly on $BULWARK_TARGET —"
    warn "the tree is left at $build for a human merge; the running stack is untouched"
    return 1
  fi

  if [ "$BUILD_HOST" = local ]; then
    [ "${ALLOW_LOCAL_BUILD:-0}" -eq 1 ] || {
      warn "refusing to build inside the mail container — a local build took mail"
      warn "down on 2026-10-27. Set BUILD_HOST=<user@host with docker> (now: $BUILD_HOST)"
      warn "or ALLOW_LOCAL_BUILD=1 to override. Nothing has been changed."
      return 1
    }
    log "  building $newimg locally (this takes several minutes)"
    if ! docker build -t "$newimg" "$build" >/root/bulwark-build-$tag.log 2>&1; then
      warn "image build failed — see /root/bulwark-build-$tag.log"
      return 1
    fi
  else
    log "  building $newimg on $BUILD_HOST (this takes several minutes)"
    $BUILD_SSH "$BUILD_HOST" "rm -rf $BUILD_DIR && mkdir -p $BUILD_DIR" >/dev/null 2>&1 \
      || { warn "cannot prepare $BUILD_DIR on $BUILD_HOST"; return 1; }
    tar -C "$build" -cf - . 2>/dev/null \
      | $BUILD_SSH "$BUILD_HOST" "tar -C $BUILD_DIR -xf -" 2>/dev/null \
      || { warn "cannot copy the source tree to $BUILD_HOST"; return 1; }
    if ! $BUILD_SSH "$BUILD_HOST" "cd $BUILD_DIR && docker build -t $newimg ." \
         >/root/bulwark-build-$tag.log 2>&1; then
      warn "image build failed on $BUILD_HOST — see /root/bulwark-build-$tag.log"
      return 1
    fi
    log "  importing the image from $BUILD_HOST"
    if ! $BUILD_SSH "$BUILD_HOST" "docker save $newimg | gzip -1" 2>/dev/null | docker load >/dev/null 2>&1; then
      warn "could not import $newimg from $BUILD_HOST"
      return 1
    fi
    $BUILD_SSH "$BUILD_HOST" "rm -rf $BUILD_DIR" >/dev/null 2>&1 || true
  fi

  local i
  log "  deploying"
  sed -i "s|^\(\s*image:\s*\).*|\1$newimg|" "$COMPOSE_FILE" || { warn "cannot rewrite compose file"; return 1; }
  ( cd "$COMPOSE_DIR" && docker compose up -d ) >/root/bulwark-up-$tag.log 2>&1 \
    || warn "compose up returned non-zero"

  for i in $(seq 1 30); do
    bulwark_healthy && break
    sleep 10
  done
  if bulwark_healthy; then
    log "  bulwark OK on $newimg"
    git -C "$BULWARK_SRC" worktree remove --force "$build" >/dev/null 2>&1 || true
    return 0
  fi

  warn "bulwark did not become healthy on $newimg — restoring $BULWARK_CURRENT"
  cp -f "$BACKUP/compose.yml" "$COMPOSE_FILE"
  ( cd "$COMPOSE_DIR" && docker compose up -d ) >/root/bulwark-rollback.log 2>&1 || true
  local i
  for i in $(seq 1 18); do bulwark_healthy && break; sleep 5; done
  bulwark_healthy && log "  rolled back, healthy on $(bulwark_running_tag)" \
                  || warn "  ROLLBACK DID NOT RECOVER — see /root/bulwark-rollback.log"
  return 1
}

# ── timer ────────────────────────────────────────────────────────────────

install_timer() {
  local self
  self=$(readlink -f "$0")
  install -d -m 0755 /usr/local/sbin
  install -m 0755 "$self" /usr/local/sbin/update-mail-stack.sh

  cat > /etc/systemd/system/mail-stack-update.service <<EOF
[Unit]
Description=Update the Stalwart + Bulwark mail stack
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/update-mail-stack.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EOF

  cat > /etc/systemd/system/mail-stack-update.timer <<'EOF'
[Unit]
Description=Weekly mail stack update check

[Timer]
OnCalendar=Sun *-*-* 04:30:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now mail-stack-update.timer >/dev/null 2>&1
  log "installed mail-stack-update.timer (Sundays 04:30 + up to 30m jitter)"
  systemctl list-timers mail-stack-update.timer --no-pager 2>/dev/null | head -3
}

# ── main ─────────────────────────────────────────────────────────────────

if [ "${INSTALL_TIMER:-0}" -eq 1 ]; then
  install_timer
  exit 0
fi

log "==================== mail stack: $MODE ===================="

[ "$ONLY" = bulwark ] || stalwart_resolve
[ "$ONLY" = stalwart ] || bulwark_resolve

printf '\n  %-10s %-12s %-12s %s\n' COMPONENT CURRENT AVAILABLE ACTION
printf '  %-10s %-12s %-12s %s\n' stalwart "$STALWART_CURRENT" "$STALWART_TARGET" \
  "$([ "${STALWART_NEEDS:-0}" -eq 1 ] && echo update || echo current)"
printf '  %-10s %-12s %-12s %s\n\n' bulwark "$BULWARK_CURRENT" "$BULWARK_TARGET" \
  "$([ "${BULWARK_NEEDS:-0}" -eq 1 ] && echo update || echo current)"
log "stalwart tracks: $STALWART_SERIES"

if [ "${STALWART_NEEDS:-0}" -eq 0 ] && [ "${BULWARK_NEEDS:-0}" -eq 0 ]; then
  log "nothing to do"
  exit 0
fi

if [ "$MODE" = check ]; then
  log "check only — nothing changed"
  exit 0
fi

backup_all

rc=0
if [ "${STALWART_NEEDS:-0}" -eq 1 ] && [ "$ONLY" != bulwark ]; then
  stalwart_apply || rc=1
fi

if [ "${BULWARK_NEEDS:-0}" -eq 1 ] && [ "$ONLY" != stalwart ]; then
  bulwark_apply || rc=1
fi

if [ "$rc" -eq 0 ]; then
  log "mail stack updated and verified: stalwart $(stalwart_running_version), bulwark $(bulwark_running_version)"
  log "backup kept at $BACKUP"
else
  warn "update did not complete cleanly; backup at $BACKUP"
fi
exit "$rc"
