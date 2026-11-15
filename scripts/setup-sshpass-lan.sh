#!/usr/bin/env bash
# setup-sshpass-lan.sh — non-interactive SSH from every Network container.
#
# Installs `sshpass` and an SSH *client* config that accepts any LAN host key
# without a prompt, so automation can run
#
#     sshpass -p '<pw>' ssh root@192.168.1.x ...
#
# from any incus container, any docker host, and the dev server.
#
# Scope and limits, deliberately stated:
#   * This is the **client** side. It makes the machine able to *connect* to any
#     local address without a prompt; it does not enable password authentication
#     on any sshd, and it does not add keys to anybody's authorized_keys.
#   * The permissive host-key rule is scoped to `192.168.1.*` only, so a host off
#     the LAN is still verified normally. `accept-new` adds a new key silently
#     but still refuses a *changed* one — a key swap is the case worth failing on.
#   * Idempotent: re-running installs nothing and rewrites the same config.
#
# `sshpass` is a tiny, dynamically-linked binary, and a container that glibc
# matches can take the dev server's copy directly (`incus file push`) in
# milliseconds instead of waiting on a package index — which on this estate is
# the difference between seconds and a quarter of an hour per container. Only a
# push that will not *run* falls back to the package manager.
#
# Usage (from the dev server or any host with sshpass):
#   INCUS_PASSWORD='…' ./setup-sshpass-lan.sh            # do it
#   INCUS_PASSWORD='…' ./setup-sshpass-lan.sh --check    # report only
set -uo pipefail

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

INCUS_HOSTS=${INCUS_HOSTS:-"192.168.1.51 192.168.1.52 192.168.1.53 192.168.1.54"}
DEV_HOSTS=${DEV_HOSTS:-"192.168.1.74"}
INCUS_PASSWORD=${INCUS_PASSWORD:-}
DEV_PASSWORD=${DEV_PASSWORD:-$INCUS_PASSWORD}
SSH_USER=${SSH_USER:-root}
SSHPASS_BIN=${SSHPASS_BIN:-/usr/bin/sshpass}

if [ -z "$INCUS_PASSWORD" ]; then
  echo "INCUS_PASSWORD is required (the root password for the incus hosts)." >&2
  exit 2
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8
          -o PreferredAuthentications=password -o PubkeyAuthentication=no)

ihost() { sshpass -p "$INCUS_PASSWORD" ssh "${SSH_OPTS[@]}" "$SSH_USER@$1" ${2:+"$2"}; }
dhost() { sshpass -p "$DEV_PASSWORD" ssh "${SSH_OPTS[@]}" "$SSH_USER@$1" ${2:+"$2"}; }
report() { printf '  %-30s %s\n' "$1" "$2"; }

# The client config, and a package-manager fallback for a container the pushed
# binary will not run in (musl, a different libc). Both are piped on stdin.
CONFIG_INSTALLER=$(cat <<'CONFIG_EOF'
mkdir -p /etc/ssh/ssh_config.d /root/.ssh
chmod 700 /root/.ssh 2>/dev/null
cat > /etc/ssh/ssh_config.d/90-innotel-lan.conf <<'CFG'
# Innotel LAN automation: reach any local address without a host-key prompt.
# Scoped to the LAN; a host off 192.168.1.0/24 is still verified normally.
Host 192.168.1.*
    StrictHostKeyChecking accept-new
    UserKnownHostsFile /root/.ssh/known_hosts
    ConnectTimeout 10
CFG
command -v sshpass >/dev/null 2>&1 && echo "OK sshpass" || echo "MISSING sshpass"
CONFIG_EOF
)

PKG_INSTALLER=$(cat <<'PKG_EOF'
set +e
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y -qq sshpass >/dev/null 2>&1
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache sshpass >/dev/null 2>&1
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y sshpass >/dev/null 2>&1
elif command -v yum >/dev/null 2>&1; then
  yum install -y sshpass >/dev/null 2>&1
fi
command -v sshpass >/dev/null 2>&1 && echo "OK sshpass" || echo "MISSING sshpass"
PKG_EOF
)

# Configure one container on a host, pushing the binary if it is absent.
# Every remote call is bounded: a stopped or wedged container must not stall the
# whole estate sweep, which is exactly what one quiet container did.
setup_container() { # $1=host $2=container
  local host="$1" ct="$2" present
  # The bound goes on the *remote* command (`timeout` is coreutils on the host);
  # `timeout … ihost` cannot work, because `ihost` is a shell function and
  # `timeout` can only exec a real program.
  present=$(ihost "$host" "timeout 40 incus exec $ct -- sh -c 'command -v sshpass >/dev/null 2>&1 && echo yes || echo no'" 2>/dev/null | tail -1)
  if [ "$present" = "" ]; then echo "unreachable"; return; fi
  if [ "$CHECK" = "1" ]; then
    [ "$present" = "yes" ] && echo "OK sshpass" || echo "MISSING sshpass"
    return
  fi
  if [ "$present" != "yes" ]; then
    ihost "$host" "timeout 60 incus file push $SSHPASS_BIN $ct/usr/bin/sshpass >/dev/null 2>&1; timeout 30 incus exec $ct -- chmod 755 /usr/bin/sshpass" >/dev/null 2>&1
    # A pushed binary that will not execute means a libc mismatch; only then pay
    # for the package manager.
    if ! ihost "$host" "timeout 30 incus exec $ct -- sshpass -V" >/dev/null 2>&1; then
      ihost "$host" "timeout 120 incus exec $ct -- bash -s" <<<"$PKG_INSTALLER" >/dev/null 2>&1
    fi
  fi
  ihost "$host" "timeout 40 incus exec $ct -- bash -s" <<<"$CONFIG_INSTALLER" 2>/dev/null | tail -1
}

echo "== incus hosts and their containers =="
for host in $INCUS_HOSTS; do
  if ! ihost "$host" true 2>/dev/null; then
    report "$host" "unreachable (check INCUS_PASSWORD)"
    continue
  fi
  # Ensure the host itself can push: it must hold the binary somewhere.
  have=$(ihost "$host" 'command -v sshpass >/dev/null 2>&1 && echo yes || echo no' 2>/dev/null | tail -1)
  if [ "$have" != "yes" ] && [ "$CHECK" = "0" ] && [ -f "$SSHPASS_BIN" ]; then
    sshpass -p "$INCUS_PASSWORD" scp "${SSH_OPTS[@]}" "$SSHPASS_BIN" "$SSH_USER@$host:/usr/bin/sshpass" >/dev/null 2>&1
    ihost "$host" 'chmod 755 /usr/bin/sshpass' >/dev/null 2>&1
  fi
  if [ "$CHECK" = "1" ]; then
    report "$host (host)" "$(ihost "$host" 'command -v sshpass >/dev/null 2>&1 && echo OK || echo MISSING' 2>/dev/null | tail -1)"
  else
    report "$host (host)" "$(ihost "$host" 'bash -s' <<<"$CONFIG_INSTALLER" 2>/dev/null | tail -1)"
  fi
  containers=$(ihost "$host" 'timeout 60 incus list --format csv -c n 2>/dev/null' 2>/dev/null | tr -d '\r')
  for ct in $containers; do
    report "$ct ($host)" "$(setup_container "$host" "$ct")"
  done
done

echo "== dev server(s) =="
for host in $DEV_HOSTS; do
  if [ "$CHECK" = "1" ]; then
    report "$host (dev)" "$(dhost "$host" 'command -v sshpass >/dev/null 2>&1 && echo OK || echo MISSING' 2>/dev/null | tail -1)"
  else
    report "$host (dev)" "$(dhost "$host" 'bash -s' <<<"$CONFIG_INSTALLER" 2>/dev/null | tail -1)"
  fi
done

echo "done."
