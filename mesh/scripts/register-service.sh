#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# Register a service with the Consul registry
# ══════════════════════════════════════════════════════════════════════════════
# Usage:
#   ./register-service.sh <name> <address> <port> [tags...]
#
# Example:
#   ./register-service.sh cerulean 10.10.1.1 3003 auth primary
#   ./register-service.sh lms 10.10.1.1 18080 education primary
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SERVICE_NAME="${1:?Usage: register-service.sh <name> <address> <port> [tags...]}"
SERVICE_ADDR="${2:?Missing address}"
SERVICE_PORT="${3:?Missing port}"
shift 3
TAGS="${*:-$SERVICE_NAME}"

CONSUL="${REGISTRY_ADDR:-10.10.1.1:8500}"

# Register via HTTP API
curl -s -X PUT "http://${CONSUL}/v1/agent/service/register" \
  -d "{
    \"Name\": \"${SERVICE_NAME}\",
    \"Address\": \"${SERVICE_ADDR}\",
    \"Port\": ${SERVICE_PORT},
    \"Tags\": [\"${TAGS// /\", \"}\"],
    \"Check\": {
      \"TCP\": \"${SERVICE_ADDR}:${SERVICE_PORT}\",
      \"Interval\": \"15s\",
      \"Timeout\": \"5s\",
      \"DeregisterCriticalServiceAfter\": \"10m\"
    }
  }" >/dev/null

echo "[mesh] Registered ${SERVICE_NAME} at ${SERVICE_ADDR}:${SERVICE_PORT} (tags: ${TAGS})"
