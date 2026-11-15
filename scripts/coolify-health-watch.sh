#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# coolify-health-watch — restart Coolify's web process when it dies in place.
#
# On 2026-11-15 the `coolify` container stayed Up for seven hours while its web
# process on 8080 was gone: `deploy.innotel.us` answered 502 from the edge and
# Laravel's scheduler kept logging jobs, so `docker ps` looked healthy. Docker
# had run the healthcheck 5,038 times and marked the container *unhealthy* —
# Docker never restarts a container for being unhealthy, only for exiting, so
# nothing acted on it. This is the actor.
#
# Cheap and idempotent: one `docker inspect`, and a port probe only when the
# healthcheck is missing. It restarts, waits for the port, and says so in the
# journal; a container it had to start twice in a row is worth reading about.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

CONTAINER="${COOLIFY_CONTAINER:-coolify}"
PORT="${COOLIFY_PORT:-8000}"
STATE="${COOLIFY_STATE:-/run/coolify-health-watch.state}"
say() { logger -t coolify-health-watch -- "$*"; echo "$*"; }

info="$(docker inspect "$CONTAINER" --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null)"
if [ -z "$info" ]; then say "no container named $CONTAINER — nothing to watch"; exit 0; fi
read -r state health <<<"$info"

if [ "$state" != "running" ]; then
  say "container is '$state' — starting it"
  docker start "$CONTAINER" >/dev/null 2>&1
  echo start >>"$STATE" 2>/dev/null
  exit 0
fi

# The healthcheck is the authority when the image has one: it probes the port
# from inside, which is where the 2026-11-15 failure was visible.
if [ "$health" = "unhealthy" ]; then
  say "container is unhealthy — restarting it"
  docker restart "$CONTAINER" >/dev/null 2>&1
  echo restart >>"$STATE" 2>/dev/null
  exit 0
fi

if [ "$health" = "none" ] && [ "$CONTAINER" = "coolify" ]; then
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:${PORT}/" 2>/dev/null || echo 000)"
  case "$code" in
    2*|3*) : ;;
    *)
      # One failed probe is noise (a restart in progress looks the same); two
      # in a row is the dead-web-process shape.
      if [ "$(cat "$STATE" 2>/dev/null)" = "probe-failed" ]; then
        say "web port answered '$code' twice — restarting the container"
        docker restart "$CONTAINER" >/dev/null 2>&1
        echo restart >>"$STATE" 2>/dev/null
      else
        say "web port answered '$code' — will restart if it repeats"
        echo probe-failed >"$STATE" 2>/dev/null
      fi
      exit 0 ;;
  esac
fi

: >"$STATE" 2>/dev/null
exit 0
