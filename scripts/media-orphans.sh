#!/bin/sh
# media-orphans.sh - name anything in the library that no *arr claims.
#
# Runs INSIDE the monarch container, where the library is /data/media and the
# *arr APIs listen on 127.0.0.1. From i1:
#
#   incus exec monarch -- sh -s < scripts/media-orphans.sh
#
# Four libraries, four owners, and no two of them use the same API shape:
#
#   tv      Sonarr    /api/v3/series
#   movies  Radarr    /api/v3/movie
#   music   Lidarr    /api/v1/artist    (Lidarr never left API v1)
#   xxx     Whisparr  /api/v3/series    (v2 is Sonarr-based, not Radarr-based:
#                                        it answers series/episode and returns
#                                        404 for /api/v3/movie)
#
# A folder counts as claimed only if one of those lists holds that exact path.
# An unclaimed folder that holds media is an orphan (exit 1). An unclaimed folder
# holding only .nfo/.srt/posters is debris, because a purge leaves those behind
# and no *arr claims them. Exit 2 whenever an API cannot be read: a failed read is
# never "the library is clean".
set -u

ROOT=${MEDIA_ROOT:-/data/media}
SONARR=${SONARR_URL:-http://127.0.0.1:8989}
RADARR=${RADARR_URL:-http://127.0.0.1:7878}
LIDARR=${LIDARR_URL:-http://127.0.0.1:8686}
WHISPARR=${WHISPARR_URL:-http://127.0.0.1:6969}
SONARR_CONF=${SONARR_CONF:-/docker/appdata/sonarr/config.xml}
RADARR_CONF=${RADARR_CONF:-/docker/appdata/radarr/config.xml}
LIDARR_CONF=${LIDARR_CONF:-/docker/appdata/lidarr/config.xml}
WHISPARR_CONF=${WHISPARR_CONF:-/docker/appdata/whisparr/config.xml}

command -v curl >/dev/null 2>&1 || { echo "CANNOT RUN: curl is missing"; exit 2; }
[ -d "$ROOT" ] || { echo "CANNOT RUN: $ROOT is not a directory"; exit 2; }

TMP=$(mktemp -d 2>/dev/null) || { echo "CANNOT RUN: no writable temporary directory"; exit 2; }
CLAIMED="$TMP/claimed"
: > "$CLAIMED"
trap 'rm -rf "$TMP"' EXIT INT TERM

apikey() {
  [ -r "$1" ] || return 0
  sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' "$1" | head -1
}

# collect NAME URL CONFIG OUT -> appends that app's claimed paths to $CLAIMED.
# Nonzero when the app cannot be read; the caller turns that into exit 2.
collect() {
  _name=$1
  _url=$2
  _conf=$3
  _out=$4
  _before=$(wc -l < "$CLAIMED")
  _key=$(apikey "$_conf")
  if [ -z "$_key" ]; then
    echo "CANNOT RUN: no ApiKey readable in $_conf" >&2
    return 2
  fi
  if ! curl -fsS --max-time 25 -H "X-Api-Key: $_key" "$_url" -o "$_out" 2>/dev/null; then
    echo "CANNOT RUN: $_name API did not answer at $_url" >&2
    return 2
  fi
  case "$(head -c 1 "$_out")" in
    '[') : ;;
    *)   echo "CANNOT RUN: $_name answered with something that is not a JSON array" >&2
         return 2 ;;
  esac
  # The *arr APIs pretty-print their JSON, so allow whitespace after the colon.
  grep -o '"path"[[:space:]]*:[[:space:]]*"[^"]*"' "$_out" 2>/dev/null \
    | sed 's/^"path"[[:space:]]*:[[:space:]]*"//; s/"$//' >> "$CLAIMED"
  _after=$(wc -l < "$CLAIMED")
  echo "$_name: $((_after - _before)) path(s) claimed"
}

collect sonarr   "$SONARR/api/v3/series"   "$SONARR_CONF"   "$TMP/sonarr.json"   || exit 2
collect radarr   "$RADARR/api/v3/movie"    "$RADARR_CONF"   "$TMP/radarr.json"   || exit 2
collect lidarr   "$LIDARR/api/v1/artist"   "$LIDARR_CONF"   "$TMP/lidarr.json"   || exit 2
collect whisparr "$WHISPARR/api/v3/series" "$WHISPARR_CONF" "$TMP/whisparr.json" || exit 2

claimed_count=$(wc -l < "$CLAIMED")
echo "claimed paths: $claimed_count (sonarr series + radarr movies + lidarr artists + whisparr series)"

if [ "$claimed_count" -lt 1 ]; then
  echo "CANNOT RUN: not one path was returned by any API"
  exit 2
fi

orphans=0
debris=0
scanned=0

for section in tv movies music xxx; do
  dir="$ROOT/$section"
  if [ ! -d "$dir" ]; then
    echo "note: $dir does not exist"
    continue
  fi
  for entry in "$dir"/* "$dir"/.[!.]*; do
    [ -e "$entry" ] || continue
    scanned=$((scanned + 1))
    if grep -Fxq "$entry" "$CLAIMED"; then
      continue
    fi
    # An unclaimed entry only matters if it holds media. Leftover .nfo/.srt/.jpg
    # sidecars from deleted episodes are debris: they cost kilobytes and the *arr
    # apps deliberately do not claim them, so they must not keep the timer red.
    if [ -d "$entry" ]; then
      media=$(find "$entry" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' \
        -o -iname '*.m4v' -o -iname '*.mov' -o -iname '*.wmv' -o -iname '*.webm' \
        -o -iname '*.mpg' -o -iname '*.mpeg' -o -iname '*.ts' -o -iname '*.m2ts' \
        -o -iname '*.mp3' -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.opus' \
        \) -print -quit 2>/dev/null)
    else
      case "$entry" in
        *.mkv|*.mp4|*.avi|*.m4v|*.mov|*.wmv|*.webm|*.mpg|*.mpeg|*.ts|*.m2ts|*.mp3|*.flac|*.m4a|*.opus)
          media=$entry ;;
        *) media="" ;;
      esac
    fi
    if [ -z "$media" ]; then
      debris=$((debris + 1))
      echo "debris (no media, unclaimed): $entry"
      continue
    fi
    orphans=$((orphans + 1))
    size=$(du -sh "$entry" 2>/dev/null | cut -f1)
    files=$(find "$entry" -type f 2>/dev/null | wc -l)
    echo "ORPHAN ${size:-?} ($files files): $entry"
  done
done

echo
echo "scanned: $scanned entries under $ROOT/{tv,movies,music,xxx}"
if [ "$orphans" -gt 0 ]; then
  echo "result: $orphans unclaimed item(s) holding media; $debris leftover item(s) with no media"
  exit 1
fi
echo "result: all $scanned entries holding media are claimed by sonarr, radarr, lidarr or whisparr; $debris leftover item(s) with no media"
exit 0
