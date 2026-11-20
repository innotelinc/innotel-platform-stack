#!/usr/bin/env bash
# Bring up a second, deliberately slim Open edX stack beside the live one, so
# content and users can be migrated without downtime.
#
#   ./atheniq-parallel-stack.sh --plan                    # print, change nothing
#   ./atheniq-parallel-stack.sh --apply                   # create + restore
#   ./atheniq-parallel-stack.sh --verify
#   ./atheniq-parallel-stack.sh --destroy                 # remove the second stack
#
# Run on the incus host (i1). Memory is the whole story here; see
# platform-stack/docs/atheniq-cutover-and-migration-2026-11-18.md §5.
set -euo pipefail

NAME=atheniq-next
IMAGE=images:ubuntu/24.04
SNAPSHOT=${SNAPSHOT:-/home/tutor/migration/snapshot-20261118-1946}
LIMIT=3584MiB          # slim: no Meilisearch, one worker, small gunicorn pool
CPUS=2
MODE=${1:---plan}

need_kb=$(( (3584 + 1024) * 1024 ))   # stack + a real safety margin

say() { printf '\n== %s\n' "$*"; }

headroom() {
  local avail
  avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  echo "host available: $((avail/1024)) MiB; this stack wants $((need_kb/1024)) MiB (limit + margin)"
  if [ "$avail" -lt "$need_kb" ]; then
    echo "NOT ENOUGH MEMORY — the live stack would start swapping." >&2
    return 1
  fi
}

case "$MODE" in
  --plan)
    cat <<PLAN
Plan (changes nothing):

  1. incus launch $IMAGE $NAME        then limits: memory=$LIMIT cpu=$CPUS
  2. install docker + the Tutor CLI version the live stack runs
  3. new Tutor root /home/tutor/tutor-next with its OWN project name and ports
     (TUTOR_PROJECT_NAME=$NAME) so it cannot touch the live containers
  4. slim profile: no Meilisearch (point search at LMS index off), one CMS,
     one lms-worker with low concurrency, gunicorn 2 workers
  5. restore $SNAPSHOT into it and verify 120 courses / 5 enrolments / 7 users
  6. point a test hostname at it; keep the live stack untouched

Rollback is `--destroy`: the live stack never moved.
PLAN
    headroom || true
    ;;
  --apply)
    headroom
    if incus info "$NAME" >/dev/null 2>&1; then
      echo "$NAME already exists; use --destroy first." >&2; exit 1
    fi
    say "launch $NAME"
    incus launch "$IMAGE" "$NAME" -c "limits.memory=$LIMIT" -c "limits.cpu=$CPUS"
    say "waiting for network"
    for i in $(seq 1 30); do
      incus exec "$NAME" -- getent hosts archive.ubuntu.com >/dev/null 2>&1 && break
      sleep 2
    done
    say "next: install docker + tutor inside $NAME, then restore $SNAPSHOT"
    echo "  incus exec $NAME -- bash -lc 'apt-get update && apt-get install -y docker.io'"
    echo "  then follow §5 of the runbook with TUTOR_ROOT=/home/tutor/tutor-next"
    ;;
  --verify)
    incus exec "$NAME" -- bash -lc 'free -m | head -2'
    incus config get "$NAME" limits.memory
    ;;
  --destroy)
    say "removing $NAME (the live stack is not touched)"
    incus delete "$NAME" --force
    ;;
  *) echo "Unknown mode: $MODE" >&2; exit 2 ;;
esac
