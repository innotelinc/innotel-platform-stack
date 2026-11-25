#!/bin/bash
# Install a `docker-default` AppArmor profile for a Docker daemon running inside an
# Incus guest, in complain, so a container may signal its own processes again.
#
# WHY THIS EXISTS
# ---------------
# Under nested Incus a container's AppArmor peer label is namespaced
# (`docker-default//&unconfined`), while the stock `docker-default` profile allows
# `signal` only to a peer named exactly `docker-default`. Docker's generated profile
# is therefore too narrow for its own containers, and the denial is not cosmetic:
#
#   FATAL:  could not signal for checkpoint: Permission denied
#   pg_ctl: could not send stop signal (PID: 53): Permission denied
#
#   apparmor="DENIED" operation="signal" namespace="root//incus-signara_<var-lib-incus>"
#   profile="docker-default" pid=… comm="postgres" requested_mask="send"
#   signal=urg peer="docker-default//&unconfined"
#
# and, worst of all, an image whose entrypoint drops privileges with a Go helper
# (`gosu`) loops forever at ~100% CPU before `exec`, so PID 1 stays the helper, the
# logs stay empty, the container stays `running`, and `restart: unless-stopped`
# never fires. Measured on `signara` (2026-11-15 → 2026-11-18): the database was
# healthy the whole time and the API crash-looped on `P1001` behind a wedge that no
# health-based monitor could attribute.
#
# The profile is **named `docker-default`** on purpose: the selector Docker hands to
# AppArmor is the profile *name*, so the stacked peer label only matches if this file
# carries the name the daemon's containers label themselves with. That is also why
# `monarch` (i2) works — it runs a profile of this exact shape.
#
# Complain, not enforce: the rules are audited and never blocked, so the effect is
# equal to allowing them while every denial still lands in the kernel log. Enforcing
# a hand-written container profile is how a host loses a service to a rule nobody
# tested.
#
# USAGE
#   ./install-docker-apparmor-profile.sh --check                 # report, change nothing
#   ./install-docker-apparmor-profile.sh --apply                 # install + reload + select
#   ./install-docker-apparmor-profile.sh --apply --name docker-default-atheniq
#
# Run it **inside the guest that runs the daemon**:
#
#   incus exec <guest> -- bash -s -- --check < scripts/install-docker-apparmor-profile.sh
#
# `--apply` restarts Docker to make the daemon read the new `apparmor-profile` key.
# `live-restore: true` keeps running containers up across that restart; without it,
# expect containers to stop and come back. Either way the containers that were
# already wedged keep the label they were created with and need one recreate.
#
# The value written is the profile's **path**; Docker loads that file and applies the
# profile named inside it. A bare name there makes dockerd refuse to start.
#
# Not every daemon can be configured this way. Docker 29.1.3 (atheniq) has no
# `--apparmor-profile` at all and rejects the key with "the following directives don't
# match any configuration option". The script therefore asks
# `dockerd --validate --config-file=` BEFORE restarting, and rolls the config back and
# leaves Docker running if the daemon would not accept it — the first version of this
# script restarted first and took all 21 containers on atheniq down with it.
#
# Exit: 0 already correct / repaired, 1 drift or a failed repair, 2 bad usage.
set -uo pipefail

NAME="docker-default"
MODE=check
DAEMON=/etc/docker/daemon.json
PROFILES=/sys/kernel/security/apparmor/profiles

usage() { sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) MODE=apply ;;
    --check) MODE=check ;;
    --name) NAME="${2:-}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "install-docker-apparmor-profile: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$NAME" in
  "" | *[!A-Za-z0-9._-]*)
    echo "install-docker-apparmor-profile: --name must be a profile name (letters, digits, . _ -)" >&2
    exit 2
    ;;
esac

PROFILE=/etc/apparmor.d/$NAME
LOADED=$(grep "^$NAME " "$PROFILES" 2>/dev/null | head -1)
WANT_LOADED="$NAME (complain)"

profile_body() {
  cat <<PROFILE_TEXT
#include <tunables/global>

# Replaces docker-default for this Docker daemon (daemon.json: apparmor-profile).
#
# Why: under nested incus the container's peer label is namespaced
# (\`docker-default//&unconfined\`), while the stock docker-default allows a signal
# only to a peer named exactly \`docker-default\`. So a container could not signal
# its own processes — a postgres that cannot checkpoint, and an entrypoint whose
# Go privilege drop retries forever instead of exec'ing the server.
#
# complain means the rules are audited, never enforced: the outcome is the same as
# allowing them, but a denial still appears in the audit log. This is the posture
# monarch and signara run.
profile $NAME flags=(complain,attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  capability,
  network,
  mount,
  remount,
  umount,
  pivot_root,
  ptrace,
  signal,
  dbus,
  unix,
  file,
}
PROFILE_TEXT
}

# The value is the profile **file** (Docker loads the profile from it), not the profile
# name. Measured 2026-11-18: a name here makes dockerd refuse to start, and the failure
# takes every container on the host with it. `--security-opt apparmor=<name>` is where a
# name belongs; this key is a path.
daemon_selects() {
  python3 - "$DAEMON" "$PROFILE" <<'PY' 2>/dev/null
import json, sys
config, profile = sys.argv[1], sys.argv[2]
try:
    current = json.load(open(config))
except Exception:
    sys.exit(1)
sys.exit(0 if current.get("apparmor-profile") == profile else 1)
PY
}

restore_daemon() { # put back the newest backup; never leave a daemon without a config
  local backup
  # Newest by mtime, and a path with a space stays one: `ls | head -1` splits on whitespace,
  # which SC2012 is right about.
  backup=$(find "$(dirname "$DAEMON")" -maxdepth 1 -name "$(basename "$DAEMON").bak-*" \
             -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)
  if [ -n "$backup" ]; then
    cp -p "$backup" "$DAEMON" && echo "restored $DAEMON from $backup"
  else
    rm -f "$DAEMON" && echo "removed $DAEMON (there was no backup)"
  fi
}

drift=""
[ -f "$PROFILE" ] || drift="$drift profile-missing"
if [ -f "$PROFILE" ] && ! cmp -s "$PROFILE" <(profile_body); then
  drift="$drift profile-content"
fi
daemon_selects || drift="$drift daemon-config"
[ "$LOADED" = "$WANT_LOADED" ] || drift="$drift not-loaded-in-complain"

if [ -z "$drift" ]; then
  echo "ok: $NAME is installed, selected and loaded in complain"
  exit 0
fi

echo "drift:$drift"
if [ "$MODE" = check ]; then
  echo "check only; run with --apply to repair"
  exit 1
fi

# ── the profile ─────────────────────────────────────────────────────────────
if ! profile_body > "$PROFILE.tmp"; then
  echo "could not write $PROFILE.tmp" >&2
  exit 1
fi
# Back up a differing file before replacing it: an operator may have tightened it.
if [ -f "$PROFILE" ] && ! cmp -s "$PROFILE" "$PROFILE.tmp"; then
  cp -p "$PROFILE" "$PROFILE.bak-$(date +%s)"
fi
install -m 0644 "$PROFILE.tmp" "$PROFILE" || { rm -f "$PROFILE.tmp"; echo "install failed" >&2; exit 1; }
rm -f "$PROFILE.tmp"

apparmor_parser -r "$PROFILE" 2>/dev/null || { echo "apparmor_parser refused $PROFILE" >&2; exit 1; }

# ── the daemon's selection of it ────────────────────────────────────────────
if ! daemon_selects; then
  cp -p "$DAEMON" "$DAEMON.bak-$(date +%s)" 2>/dev/null || true
  python3 - "$DAEMON" "$PROFILE" <<'PY' || { echo "could not update $DAEMON" >&2; exit 1; }
import json, sys
path, profile = sys.argv[1], sys.argv[2]
try:
    current = json.load(open(path))
except Exception:
    current = {}
current["apparmor-profile"] = profile
with open(path, "w") as handle:
    json.dump(current, handle, indent=2)
    handle.write("\n")
PY

  # Ask the daemon whether it understands the file *before* bouncing it. Docker 29.1.3
  # (atheniq, 2026-11-18) answers `the following directives don't match any
  # configuration option: apparmor-profile` — a restart there stops every container on
  # the host until somebody notices, which is exactly what this validate step prevents.
  if command -v dockerd >/dev/null 2>&1 &&
     ! dockerd --validate --config-file="$DAEMON" >/tmp/dockerd-validate.log 2>&1; then
    echo "this daemon does not accept apparmor-profile: $(grep -o 'directives.*' /tmp/dockerd-validate.log | head -1)"
    echo "leaving $DAEMON unchanged (the profile stays installed for per-container use)"
    restore_daemon
    echo "the label can still be applied per container: docker run --security-opt apparmor=$NAME …"
    exit 1
  fi

  running_before=$(docker ps -q 2>/dev/null | wc -l)
  # Restarting dockerd without live-restore stops every container on the host, and
  # on 2026-11-22 one such bounce also left a container's port publish unregistered
  # (gateway.innotel.us answered 502). Refuse unless live-restore is genuinely on.
  if [ "${FORCE_DOCKER_RESTART:-0}" != 1 ] &&
     [ "$(docker info --format '{{.LiveRestoreEnabled}}' 2>/dev/null)" != true ]; then
    echo "refusing to restart docker: live-restore is off, so this bounce would stop all $running_before running containers." >&2
    echo "add \"live-restore\": true to $DAEMON and restart once deliberately, or re-run with FORCE_DOCKER_RESTART=1." >&2
    restore_daemon
    exit 1
  fi
  echo "added apparmor-profile=$PROFILE to $DAEMON; restarting docker ($running_before containers running)"
  if ! systemctl restart docker; then
    echo "docker did not restart; rolling $DAEMON back" >&2
    restore_daemon
    systemctl start docker
    exit 1
  fi
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
  running_after=$(docker ps -q 2>/dev/null | wc -l)
  if [ "$running_after" -lt "$running_before" ]; then
    echo "warning: containers running $running_before → $running_after after the restart; check docker ps -a" >&2
    exit 1
  fi
fi

# ── verify, never assume ────────────────────────────────────────────────────
LOADED=$(grep "^$NAME " "$PROFILES" 2>/dev/null | head -1)
if [ "$LOADED" != "$WANT_LOADED" ]; then
  echo "still not loaded in complain: '${LOADED:-none}'" >&2
  exit 1
fi

echo "ok: $NAME installed, selected and loaded in complain"
echo "note: containers created before this change keep their old label — recreate them once"
