#!/usr/bin/env bash
# artex-planner-watch.sh - detect a wedged ARTEX task and recover just that task.
#
# A wedged task keeps status created/running while emitting no further tool
# activity. Recovery is two-tier so that one stuck task does not cost every
# other task its progress:
#
#   1. pause then resume the stalled task through the control API. Pausing
#      cancels that task's in-flight planner/worker context; resuming starts a
#      fresh planning round. Other tasks keep running untouched.
#   2. only if the task still records no new activity after RESUME_WAIT does
#      the script restart the app container.
#
# Safety: per-task cooldown (default 30 min), diagnostics captured before any
# action, DRY_RUN=1 for observation only, and the API token is never logged.
set -uo pipefail

APP_PREFIX="${APP_PREFIX:-ugnnlaygpfkq8w9g9qcd13qb}"
DB_CONTAINER="${DB_CONTAINER:-ihouhdfdabpiwqzlaoyqjt3b}"
API="${API:-http://127.0.0.1:8790}"
ENV_FILE="${ENV_FILE:-/root/artex.env}"
STALL_MIN="${STALL_MIN:-12}"
COOLDOWN_MIN="${COOLDOWN_MIN:-30}"
RESUME_WAIT="${RESUME_WAIT:-90}"
STATE_DIR="${STATE_DIR:-/var/lib/artex-planner-watch}"
CAPTURE_DIR="${CAPTURE_DIR:-/var/log/artex-planner-watch}"
LOG="${LOG:-/var/log/artex-planner-watch.log}"
DRY_RUN="${DRY_RUN:-0}"
TOKEN=""

mkdir -p "$STATE_DIR" "$CAPTURE_DIR"
ts() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }
log() { printf '%s %s\n' "$(ts)" "$*" >>"$LOG"; }
psql_q() { docker exec "$DB_CONTAINER" psql -U postgres -d postgres -At -F'|' -c "$1" 2>/dev/null; }

login() {
  [ -r "$ENV_FILE" ] || return 1
  # shellcheck disable=SC1090
  . "$ENV_FILE" 2>/dev/null || return 1
  [ -n "${ARTEX_ADMIN_PASSWORD:-}" ] || return 1
  TOKEN=$(curl -sf -X POST "$API/api/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"ARTEX\",\"password\":\"$ARTEX_ADMIN_PASSWORD\"}" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
  [ -n "$TOKEN" ]
}

# control <task id> <pause|resume> -> "ok" or "error:<reason>"
control() {
  curl -sf -X POST "$API/api/tasks/control/batch" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"task_ids\":[\"$1\"],\"action\":\"$2\"}" 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d = json.load(sys.stdin)
    i = (d.get("items") or [{}])[0]
    print("ok" if i.get("ok") else "error:" + (i.get("error") or "unknown"))
except Exception as e:
    print("error:unreadable response " + str(e))' 2>/dev/null
}

last_activity_id() { psql_q "select coalesce(max(id),0) from activity where exploration_id=$1;"; }

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
  stamp="$STATE_DIR/recovered.$id"
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
    log "DRY_RUN would pause/resume task=$id idle=${idle}s capture=$cap"
    continue
  fi

  recovered=0
  if login; then
    before=$(last_activity_id "$expl")
    pause_result=$(control "$id" pause)
    case "$pause_result" in
      ok)
        # Let the pause commit and the cancelled context unwind before resuming.
        for _ in $(seq 1 15); do
          [ "$(psql_q "select status||':'||paused from tasks where id=$id;")" = "paused:true" ] && break
          sleep 1
        done
        sleep 3
        resume_result=$(control "$id" resume)
        log "task=$id idle=${idle}s pause=ok resume=$resume_result"
        if [ "$resume_result" = "ok" ]; then
          deadline=$(( $(date -u +%s) + RESUME_WAIT ))
          while [ "$(date -u +%s)" -lt "$deadline" ]; do
            sleep 10
            if [ "$(last_activity_id "$expl")" -gt "${before:-0}" ]; then
              recovered=1
              break
            fi
          done
        fi
        ;;
      *)
        log "task=$id idle=${idle}s pause=$pause_result (no live runtime to pause)"
        ;;
    esac
  else
    log "task=$id idle=${idle}s login unavailable; control API path skipped"
  fi

  if [ "$recovered" = "1" ]; then
    ts >"$stamp"
    log "task=$id recovered by pause/resume idle=${idle}s capture=$cap"
    continue
  fi

  docker restart "$app" >/dev/null 2>&1
  rc=$?
  ts >"$stamp"
  log "task=$id still idle -> restarted container=$app rc=$rc capture=$cap"
  break
done <<<"$rows"
