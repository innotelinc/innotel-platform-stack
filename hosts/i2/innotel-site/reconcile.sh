#!/usr/bin/env bash
# reconcile.sh — keep the innotel.us site container defined, running, and on the
# Docker network NPM addresses it by.
#
# NPM host 88 forwards to `innotel-site:80`, a name only Docker's DNS on
# `proxy_default` can resolve. The container therefore cannot be left to whatever
# last touched it: a container started by hand, created from an old definition,
# stopped, or pruned while stopped all leave the apex pointing at nothing. This
# reconciles the running state to the compose definition and is safe to run at
# any time, so nothing has to be done by hand when the state drifts.
set -uo pipefail

NAME=innotel-site
NET=proxy_default
COMPOSE_DIR=/usr/src/innotel-deploy
COMPOSE_FILE=$COMPOSE_DIR/docker-compose.yml
CONTENT_DIR=/usr/src/innotel-site

log() { logger -t innotel-site-reconcile -- "$*" 2>/dev/null || true; printf '%s\n' "$*"; }
fail() { log "FAILED: $*"; exit 1; }

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" "$@"
  else
    docker-compose -f "$COMPOSE_FILE" "$@"
  fi
}

networks() {
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$NAME" 2>/dev/null | tr ' ' '\n'
}

[ -f "$COMPOSE_FILE" ] || fail "no compose definition at $COMPOSE_FILE"
[ -f "$CONTENT_DIR/index.html" ] || fail "no site content at $CONTENT_DIR/index.html"

# Empty when the container does not exist at all.
state=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null)
project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$NAME" 2>/dev/null)

# A container without the compose label was not created from the definition, so
# its network is whatever that command happened to choose. Replace it.
if [ -n "$state" ] && [ -z "$project" ]; then
  log "container exists outside the compose definition; replacing"
  docker rm -f "$NAME" >/dev/null 2>&1 || fail "could not remove $NAME"
  state=""
fi

if [ -z "$state" ]; then
  log "container missing; creating from $COMPOSE_FILE"
  compose up -d >/dev/null 2>&1 || fail "compose up failed"
elif [ "$state" != "running" ]; then
  log "container is $state; starting"
  compose up -d >/dev/null 2>&1 || fail "compose up failed"
fi

# Compose labels prove provenance, not placement: a container created from an
# earlier definition can be running and still off the network. `up -d` will not
# move it, so placement is asserted rather than assumed.
if ! networks | grep -qx "$NET"; then
  log "container is not on $NET; recreating"
  compose up -d --force-recreate >/dev/null 2>&1 || fail "compose recreate failed"
fi

final=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null)
[ "$final" = "running" ] || fail "container ended $final"
networks | grep -qx "$NET" || fail "container is running but not on $NET"
log "ok: $NAME running on $NET"
