#!/usr/bin/env bash
# artex-planner-watch.sh - detect a wedged ARTEX planner and recover it.
#
# A wedged planner keeps the task in status created/running past its deadline
# window while emitting no further tool activity. The proven remedy is a
# restart of the app container, which resumes the task immediately.
#
# Safety: only one restart per invocation, per-task cooldown (default 30 min),
# diagnostics captured before the restart, and DRY_RUN=1 for observation only.
set -uo pipefail

APP_PREFIX="${APP_PREFIX:-ugnnlaygpfkq8w9g9qcd13qb}"
DB_CONTAINER="${DB_CONTAINER:-ihouhdfdabpiwqzlaoyqjt3b}"
STALL_MIN="${STALL_MIN:-12}"
COOLDOWN_MIN="${COOLDOWN_MIN:-30}"
STATE_DIR="${STATE_DIR:-/var/lib/artex-planner-watch}"
CAPTURE_DIR="${CAPTURE_DIR:-/var/log/artex-planner-watch}"
LOG="${LOG:-/var/log/artex-planner-watch.log}"
DRY_RUN="${DRY_RUN:-0}"

mkdir -p "$STATE_DIR" "$CAPTURE_DIR"
ts() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }
log() { printf '%s %s\n' "$(ts)" "$*" >>"$LOG"; }
psql_q() { docker exec "$DB_CONTAINER" psql -U postgres -d postgres -At -F'|' -c "$1" 2>/dev/null; }

app=$(docker ps --format '{{.Names}}' | grep "^${APP_PREFIX}" | head -1)
if [ -z "$app" ]; then
  log "app container ${APP_PREFIX}* not running; skip"
  exit 0
fi

# Idle time is measured from the last tool call/result, falling back to the
# first run attempt, then to creation for a task that never started. Planner
# heartbeats do not count as progress, and queued tasks are not stalled.
sql="
select t.id, t.status, t.exploration_id,
       coalesce(extract(epoch from (now() - coalesce(last_tool.seen, t.first_run_at, t.created_at)))::int, -1)
from tasks t
left join lateral (
  select max(a.created_at) as seen
  from activity a
  where a.exploration_id = t.exploration_id
    and a.kind in ('tool_use','tool_result')
) last_tool on true
where t.deleted_at is null
  and t.archived_at is null
  and t.status in ('created','running')
  and t.paused = false
  and (t.first_run_at is not null or t.queued = false)
  and now() < coalesce(t.deadline_at, now() + interval '1 day')
  and coalesce(extract(epoch from (now() - coalesce(last_tool.seen, t.first_run_at, t.created_at))), 1e9) > ${STALL_MIN} * 60
order by t.id;"

rows=$(psql_q "$sql")
if [ -z "$rows" ]; then
  log "healthy container=$app no stalled tasks"
  exit 0
fi

while IFS='|' read -r id status expl idle; do
  [ -z "${id:-}" ] && continue
  stamp="$STATE_DIR/restarted.$id"
  if [ -f "$stamp" ]; then
    last=$(date -u -d "$(cat "$stamp")" +%s 2>/dev/null || echo 0)
    if [ $(( $(date -u +%s) - last )) -lt $(( COOLDOWN_MIN * 60 )) ]; then
      log "task=$id stalled idle=${idle}s but cooldown active since $(cat "$stamp")"
      continue
    fi
  fi

  cap="$CAPTURE_DIR/$(date -u +%Y%m%dT%H%M%SZ)-task${id}-${status}.log"
  {
    echo "== detected $(ts) task=$id status=$status idle=${idle}s container=$app =="
    echo "-- task row --"
    psql_q "select id,status,first_run_at,deadline_at,timeout_seconds,paused,end_reason from tasks where id=$id;"
    echo "-- last 8 activity --"
    psql_q "select created_at,worker,kind,tool,left(coalesce(summary,''),160) from activity where exploration_id=$expl order by id desc limit 8;"
    echo "-- container state --"
    docker inspect -f 'status={{.State.Status}} restarts={{.RestartCount}} started={{.State.StartedAt}}' "$app"
    echo "-- last 120 log lines --"
    docker logs --tail 120 "$app" 2>&1
  } >"$cap" 2>&1

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would restart task=$id idle=${idle}s capture=$cap"
    continue
  fi

  docker restart "$app" >/dev/null 2>&1
  rc=$?
  ts >"$stamp"
  log "task=$id stalled idle=${idle}s -> restarted container=$app rc=$rc capture=$cap"
  break
done <<<"$rows"
