#!/usr/bin/env bash
# Move AthenIQ's LMS onto the image built from innotelinc/openedx-platform.
#
# Read the companion doc first:
#   platform-stack/docs/atheniq-cutover-and-migration-2026-11-18.md
#
# Safety model:
#   * no flag            → prints the plan and the exact commands, changes nothing
#   * --snapshot         → writes a restorable snapshot (safe to run any time)
#   * --apply            → does the cutover, after taking its own snapshot
#   * --restore <dir>    → puts a snapshot back
#
# The Tutor root is NOT the default one. Running `tutor` as root without
# TUTOR_ROOT targets an empty second root and silently changes nothing that the
# containers read; see the doc.
set -euo pipefail

TUTOR_ROOT_DIR=/home/tutor/.local/share/tutor
TUTOR=/home/tutor/tutor-venv/bin/tutor
MIGRATION=/home/tutor/migration
MODE=${1:---plan}
IMAGE=${ATHENIQ_IMAGE:-}

export TUTOR_ROOT="$TUTOR_ROOT_DIR"

say() { printf '\n== %s\n' "$*"; }

plan() {
  cat <<'PLAN'
Plan (nothing has been changed):

  1. snapshot      MySQL dump + Mongo archive + media size, into
                   /home/tutor/migration/snapshot-<stamp>/
  2. pin image     DOCKER_IMAGE_OPENEDX=<ghcr image> in the Tutor config
  3. pull          tutor images pull   (no rebuild on this box)
  4. restart       tutor local restart lms cms lms-worker cms-worker
  5. verify        /heartbeat, /catalog/, and the course_list_search size cap
  6. rollback      re-pin the previous DOCKER_IMAGE_OPENEDX and restart

Why step 2 exists: `tutor config save` regenerates env/ from the config, so the
image must be a config key, never a hand-edited compose file.
PLAN
}

snapshot() {
  local stamp out
  stamp=$(date +%Y%m%d-%H%M%S)
  out="$MIGRATION/snapshot-$stamp"
  mkdir -p "$out"
  say "snapshot → $out"
  docker exec tutor_local-mysql-1 sh -c \
    'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers --events --databases openedx' \
    > "$out/mysql-openedx.sql"
  docker exec tutor_local-mongodb-1 sh -c 'mongodump --archive --gzip' \
    > "$out/mongo-openedx.archive.gz"
  docker exec tutor_local-lms-1 du -sh /openedx/media > "$out/media-size.txt" 2>/dev/null || true
  printf '%s\n' "$out"
}

verify() {
  say "verify"
  local base=https://atheniq.courses
  for path in /heartbeat /catalog/; do
    printf '%-12s %s\n' "$path" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$base$path")"
  done
  docker exec tutor_local-lms-1 ./manage.py lms shell -c "
from django.conf import settings
from django.test import Client
print('SEARCH_MAX_PAGE_SIZE =', getattr(settings, 'SEARCH_MAX_PAGE_SIZE', 'UNSET'))
c = Client(HTTP_HOST='atheniq.courses')
r = c.post('/search/unstable/v0/course_list_search/', data={'page_size':'120','page_index':'0','enable_course_sorting_by_start_date':'true'})
print('catalog search =', r.status_code, 'results =', len(r.json().get('results', [])))
" 2>&1 | grep -E 'SEARCH_MAX_PAGE_SIZE|catalog search'
}

case "$MODE" in
  --plan) plan ;;
  --snapshot) snapshot ;;
  --verify) verify ;;
  --apply)
    [ -n "$IMAGE" ] || { echo "Set ATHENIQ_IMAGE to the ghcr.io image to deploy." >&2; exit 1; }
    snapshot
    previous=$($TUTOR config printvalue DOCKER_IMAGE_OPENEDX)
    say "was: $previous"
    say "pinning $IMAGE"
    $TUTOR config save --set "DOCKER_IMAGE_OPENEDX=$IMAGE"
    $TUTOR images pull
    $TUTOR local restart lms cms lms-worker cms-worker
    verify
    say "rollback if needed:"
    echo "  $TUTOR config save --set 'DOCKER_IMAGE_OPENEDX=$previous' && $TUTOR images pull && $TUTOR local restart lms cms lms-worker cms-worker"
    ;;
  --restore)
    dir=${2:-}
    [ -n "$dir" ] && [ -d "$dir" ] || { echo "Usage: $0 --restore <snapshot-dir>" >&2; exit 1; }
    say "restoring $dir — this overwrites the LMS database"
    docker exec -i tutor_local-mysql-1 sh -c \
      'mysql -uroot -p"$MYSQL_ROOT_PASSWORD"' < "$dir/mysql-openedx.sql"
    docker exec -i tutor_local-mongodb-1 sh -c 'mongorestore --archive --gzip --drop' \
      < "$dir/mongo-openedx.archive.gz"
    $TUTOR local restart lms cms lms-worker cms-worker
    verify
    ;;
  *) echo "Unknown mode: $MODE (try --plan)" >&2; exit 2 ;;
esac
