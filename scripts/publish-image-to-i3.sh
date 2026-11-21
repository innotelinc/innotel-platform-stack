#!/usr/bin/env bash
# Publish an image that was built on i2 into the registry:2 that runs on i3, and print the
# tag to put in Coolify's `docker_registry_image_tag`.
#
# i3 cannot finish a Dockerfile build (BuildKit's session dies while exporting layers), so an
# app on the `i3-local` target has to arrive as a prebuilt image: build on i2, push here, and
# set `build_pack='dockerimage'`. The registry listens on i3's loopback, and i2 is not allowed
# to reach i3 at all, so the push travels the way it always has — a reverse tunnel opened FROM
# i3, which leaves both ends talking to 127.0.0.1:5000. Docker already accepts that address as
# an insecure registry, so no `daemon.json` edit and no daemon restart is needed anywhere.
#
# Run this ON i3 (it holds the dedicated push key authorized on i2's root).
#
#   ./publish-image-to-i3.sh opencompany                      # tags with a UTC timestamp
#   ./publish-image-to-i3.sh opencompany 4dae24e6e1528e3b     # or an explicit tag
#   IMAGE=opencompany:local ./publish-image-to-i3.sh opencompany
#
# Afterwards: update `applications.docker_registry_image_name/tag` for the app and redeploy.
set -euo pipefail

REPO=${1:?usage: publish-image-to-i3.sh <repo-name> [tag]}
TAG=${2:-$(date -u +%Y%m%dT%H%M%SZ)}
IMAGE=${IMAGE:-${REPO}:latest}

I2=${I2_HOST:-root@192.168.1.52}
REGISTRY_HOST=127.0.0.1
REGISTRY_PORT=${REGISTRY_PORT:-5000}
TARGET="$REGISTRY_HOST:$REGISTRY_PORT/$REPO:$TAG"
SOCK=$(mktemp -u /tmp/publish-i3-XXXXXX.sock)
CTL="ssh -o ControlMaster=yes -o ControlPath=$SOCK -o ControlPersist=600"

cleanup() {
  # Closing the multiplexed master tears down the -O forward it carries.
  ssh -o ControlPath="$SOCK" -O cancel -R "$REGISTRY_PORT:$REGISTRY_HOST:$REGISTRY_PORT" "$I2" 2>/dev/null || true
  ssh -o ControlPath="$SOCK" -O exit "$I2" 2>/dev/null || true
  rm -f "$SOCK"
}
trap cleanup EXIT

# Refuse early if the registry is not answering, rather than failing after a long push.
if ! curl -fsS -m 5 "http://$REGISTRY_HOST:$REGISTRY_PORT/v2/" >/dev/null; then
  echo "error: no registry answering on $REGISTRY_HOST:$REGISTRY_PORT (is registry:2 up on i3?)" >&2
  exit 1
fi

# Keep one authenticated connection alive and hang a reverse tunnel on it: i2's
# 127.0.0.1:$REGISTRY_PORT then lands on this host's registry.
$CTL -f -N -R "$REGISTRY_PORT:$REGISTRY_HOST:$REGISTRY_PORT" "$I2"

echo "pushing $IMAGE -> $TARGET (via reverse tunnel to i2)"
ssh -o ControlPath="$SOCK" "$I2" \
  "docker image inspect '$IMAGE' >/dev/null 2>&1 || { echo 'error: no image $IMAGE on i2' >&2; exit 1; };
   docker tag '$IMAGE' '$TARGET' && docker push '$TARGET'" >/dev/null

# Prove the registry really holds it before anyone points an app at the tag.
if ! curl -fsS -m 10 "http://$REGISTRY_HOST:$REGISTRY_PORT/v2/$REPO/tags/list" | grep -q "\"$TAG\""; then
  echo "error: $TARGET is not listed by the registry after the push" >&2
  exit 1
fi

echo "published: $TARGET"
echo "set applications.docker_registry_image_name='$REGISTRY_HOST:$REGISTRY_PORT/$REPO', docker_registry_image_tag='$TAG'"
