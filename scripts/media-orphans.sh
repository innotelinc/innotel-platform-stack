#!/bin/sh
# media-orphans.sh - name anything in the library that no *arr claims.
#
# Runs INSIDE the monarch container, where the library is /data/media and the
# *arr APIs listen on 127.0.0.1 (Sonarr 8989, Radarr 7878). From i1:
#
#   incus exec monarch -- sh -s < scripts/media-orphans.sh
#
# A folder counts as claimed only if Sonarr's series list or Radarr's movie list
# contains that exact path; a folder nothing claims but which holds files is an
# orphan (exit 1). An unclaimed empty folder is reported as debris, because it
# costs no space and is usually what a purge leaves behind. Exit 2 whenever an
# API cannot be read: a failed read is never "the library is clean".
set -u

ROOT=${MEDIA_ROOT:-/data/media}
SONARR=${SONARR_URL:-http://127.0.0.1:8989}
RADARR=${RADARR_URL:-http://127.0.0.1:7878}
SONARR_CONF=${SONARR_CONF:-/docker/appdata/sonarr/config.xml}
RADARR_CONF=${RADARR_CONF:-/docker/appdata/radarr/config.xml}

command -v curl >/dev/null 2>&1 || { echo "CANNOT RUN: curl is missing"; exit 2; }
[ -d "$ROOT" ] || { echo "CANNOT RUN: $ROOT is not a directory"; exit 2; }

apikey() {
  [ -r "$1" ] || return 0
  sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' "$1" | head -1
}

# fetch NAME URL CONFIG -> array JSON on stdout; nonzero when it cannot be read
fetch() {
  _name=$1
  _url=$2
  _conf=$3
  _key=$(apikey "$_conf")
  if [ -z "$_key" ]; then
    echo "CANNOT RUN: no ApiKey readable in $_conf" >&2
    return 2
  fi
  _out=$(curl -fsS --max-time 25 -H "X-Api-Key: $_key" "$_url" 2>/dev/null) || {
    echo "CANNOT RUN: $_name API did not answer at $_url" >&2
    return 2
  }
  case "$_out" in
    \[*) printf '%s' "$_out" ;;
    *) echo "CANNOT RUN: $_name API returned something that is not a JSON array" >&2; return 2 ;;
  esac
}

series=$(fetch sonarr "$SONARR/api/v3/series" "$SONARR_CONF") || exit 2
movies=$(fetch radarr "$RADARR/api/v3/movie" "$RADARR_CONF") || exit 2

claimed=$(
  # The *arr APIs pretty-print their JSON, so allow whitespace after the colon.
  printf '%s\n%s\n' "$series" "$movies" |
    grep -o '"path"[[:space:]]*:[[:space:]]*"[^"]*"' |
    sed 's/^"path"[[:space:]]*:[[:space:]]*"//; s/"$//'
)
claimed_count=$(printf '%s\n' "$claimed" | grep -c . || true)
echo "claimed paths: $claimed_count (sonarr series + radarr movies)"

if [ "$claimed_count" -lt 1 ]; then
  echo "CANNOT RUN: not one path was returned by either API"
  exit 2
fi

orphans=0
debris=0
scanned=0

for section in tv movies; do
  dir="$ROOT/$section"
  if [ ! -d "$dir" ]; then
    echo "note: $dir does not exist"
    continue
  fi
  for entry in "$dir"/* "$dir"/.[!.]*; do
    [ -e "$entry" ] || continue
    scanned=$((scanned + 1))
    if printf '%s\n' "$claimed" | grep -Fxq "$entry"; then
      continue
    fi
    # An unclaimed entry only matters if it holds media. Leftover .nfo/.srt/.jpg
    # sidecars from deleted episodes are debris: they cost kilobytes and Sonarr
    # deliberately does not claim them, so they must not keep the timer red.
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
echo "scanned: $scanned entries under $ROOT/{tv,movies}"
if [ "$orphans" -gt 0 ]; then
  echo "result: $orphans unclaimed item(s) holding media; $debris leftover item(s) with no media"
  exit 1
fi
echo "result: all $scanned entries holding media are claimed by Sonarr or Radarr; $debris leftover item(s) with no media"
exit 0
