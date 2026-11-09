#!/usr/bin/env bash
# Keep the edge's custom nginx snippets reviewed rather than typed on the host.
#
# NPM loads /data/nginx/custom/*.conf through hooks baked into its nginx.conf:
# root_top (main), events, http_top and http. Everything in them changes how the
# whole edge behaves, and until now they existed only on the proxy host - a
# rebuilt proxy silently lost the connection ceiling, the header buffers and the
# retry peer that keep the panel and the webmail alive.
#
#   scripts/sync-proxy-nginx-custom.sh            # report drift, change nothing
#   scripts/sync-proxy-nginx-custom.sh --apply    # write it, test, reload
#
# Run it on the proxy host: that is where the live tree and the container are.
# The checked-in copy is authoritative; edit the repo file, then apply.
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SRC_DIR="$REPO_DIR/proxy/nginx/custom"
LIVE_DIR=${LIVE_DIR:-/usr/src/proxy/data/nginx/custom}
CONTAINER=${CONTAINER:-cerulean-npm}
SNIPPETS=(root_top.conf events.conf http_top.conf http.conf)

apply=0
case "${1:-}" in
  "") ;;
  --apply) apply=1 ;;
  -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

drift=0
for name in "${SNIPPETS[@]}"; do
  want="$SRC_DIR/$name"
  have="$LIVE_DIR/$name"
  [[ -f "$want" ]] || { echo "missing in repo: $want" >&2; exit 2; }
  if [[ -f "$have" ]] && cmp -s "$want" "$have"; then
    echo "same    $name"
  else
    drift=1
    echo "DIFFERS $name"
    if [[ -f "$have" ]]; then
      diff -u "$have" "$want" || true
    else
      echo "  (not on the host)"
    fi
  fi
done

if (( drift == 0 )); then
  echo "custom snippets: ${#SNIPPETS[@]} in step with $LIVE_DIR"
  exit 0
fi

if (( apply == 0 )); then
  echo "drift only — re-run with --apply to install" >&2
  exit 1
fi

install -d -m 0755 "$LIVE_DIR"
for name in "${SNIPPETS[@]}"; do
  install -m 0644 "$SRC_DIR/$name" "$LIVE_DIR/$name"
done

# Test before reloading: a bad snippet fails here, with the old config still
# serving, instead of taking the edge down on the next reload.
if ! docker exec "$CONTAINER" nginx -t; then
  echo "nginx -t refused the new snippets; the running config was not reloaded" >&2
  exit 1
fi
docker exec "$CONTAINER" nginx -s reload
echo "installed ${#SNIPPETS[@]} snippet(s) and reloaded $CONTAINER"
