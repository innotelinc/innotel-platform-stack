#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# Discover a service from the Consul registry
# ══════════════════════════════════════════════════════════════════════════════
# Usage:
#   ./discover-service.sh <service-name>
#
# Returns: address:port (suitable for curl, docker links, etc.)
#
# Examples:
#   ./discover-service.sh cerulean    → 10.10.1.1:3003
#   ./discover-service.sh authentik   → 10.10.1.1:9000
#   ./discover-service.sh omniroute   → 10.10.2.1:20128
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SERVICE="${1:?Usage: discover-service.sh <service-name>}"
CONSUL="${REGISTRY_ADDR:-10.10.1.1:8500}"

RESULT=$(curl -s "http://${CONSUL}/v1/health/service/${SERVICE}?passing=true" | \
  python3 -c "
import sys, json
services = json.load(sys.stdin)
if services:
    svc = services[0]['Service']
    print(f\"{svc['Address']}:{svc['Port']}\")
else:
    print('NOT_FOUND', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)

if [ -n "$RESULT" ]; then
  echo "$RESULT"
else
  echo "[mesh] Service '${SERVICE}' not found in registry" >&2
  exit 1
fi
