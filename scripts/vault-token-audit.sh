#!/bin/sh
# vault-token-audit.sh - report every product token's remaining life, and renew or
# replace the ones inside the warning window.
#
# Runs INSIDE the cerulean-vault container: only there do the instance root token
# (/vault/init/init.json, mode 600) and every product token exist together. From i2:
#
#   incus exec proxy -- docker exec -i cerulean-vault sh -s -- < scripts/vault-token-audit.sh
#   incus exec proxy -- docker exec -i cerulean-vault sh -s -- --warn-days 30 --replace \
#     < scripts/vault-token-audit.sh
#
# Restrict with --only NAME; the window defaults to 14 days.
#
# Exit codes: 0 = nothing inside the window, 1 = something is lapsing, invalid, or an
# action failed, 2 = the audit could not run at all. A failed read is never "ok".
set -u

WARN_DAYS=14
MODE=none
ONLY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --renew)
      [ "$MODE" = replace ] && { echo "CANNOT RUN: --renew and --replace are mutually exclusive" >&2; exit 2; }
      MODE=renew ;;
    --replace)
      [ "$MODE" = renew ] && { echo "CANNOT RUN: --renew and --replace are mutually exclusive" >&2; exit 2; }
      MODE=replace ;;
    --warn-days)
      [ $# -ge 2 ] || { echo "CANNOT RUN: --warn-days needs a value" >&2; exit 2; }
      WARN_DAYS=$2; shift ;;
    --only)
      [ $# -ge 2 ] || { echo "CANNOT RUN: --only needs a token name" >&2; exit 2; }
      ONLY=$2; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "CANNOT RUN: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

case "$WARN_DAYS" in ''|*[!0-9]*) echo "CANNOT RUN: --warn-days must be a number" >&2; exit 2 ;; esac

export VAULT_ADDR=${VAULT_ADDR:-http://127.0.0.1:8200}
command -v vault >/dev/null 2>&1 || { echo "CANNOT RUN: no vault CLI in this container" >&2; exit 2; }

ROOT=$(sed -n 's/.*"root_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /vault/init/init.json 2>/dev/null | head -1)
[ -n "$ROOT" ] || { echo "CANNOT RUN: no readable root token in /vault/init/init.json" >&2; exit 2; }
vault status >/dev/null 2>&1 || { echo "CANNOT RUN: Vault unreachable or sealed at $VAULT_ADDR" >&2; exit 2; }

VAULT_TOKEN=$ROOT
export VAULT_TOKEN
umask 077

# One field out of a Vault JSON reply, without jq. Vault pretty-prints its JSON, so
# collapse the whitespace first and match the compact form.
compact() { printf '%s' "$1" | tr -d ' \n\r\t'; }
jnum() { compact "$1" | grep -o "\"$2\":[0-9]*" | head -1 | cut -d: -f2; }
jstr() { compact "$1" | grep -o "\"$2\":\"[^\"]*\"" | head -1 | cut -d'"' -f4; }

lapsing=0
failed=0
seen=0

printf '%-10s %-6s %-10s %-26s %-6s %s\n' NAME DAYS TTL EXPIRES RENEW ACTION

for f in /vault/token/*.token; do
  [ -r "$f" ] || continue
  n=$(basename "$f" .token)
  if [ -n "$ONLY" ] && [ "$n" != "$ONLY" ]; then continue; fi
  seen=$((seen + 1))

  t=$(tr -d '\r\n' < "$f")
  if [ -z "$t" ]; then
    printf '%-10s EMPTY TOKEN FILE\n' "$n"
    failed=1
    continue
  fi

  if ! js=$(vault token lookup -format=json "$t" 2>&1); then
    printf '%-10s INVALID OR UNREADABLE: %s\n' "$n" "$(printf '%s' "$js" | head -1)"
    failed=1
    continue
  fi

  ttl=$(jnum "$js" ttl)
  exp=$(jstr "$js" expire_time)
  ren=$(compact "$js" | grep -o '"renewable":[a-z]*' | head -1 | cut -d: -f2)

  if [ -z "$ttl" ]; then
    printf '%-10s could not parse ttl from the lookup reply\n' "$n"
    failed=1
    continue
  fi

  days=$((ttl / 86400))
  action=ok

  if [ "$ttl" -le 0 ]; then
    days="-"
    action="no expiry"
  elif [ "$days" -lt "$WARN_DAYS" ]; then
    case "$MODE" in
      renew)
        if [ "$ren" = "true" ]; then
          if rj=$(vault token renew -format=json "$t" 2>&1) && printf '%s' "$rj" | grep -q '"client_token"'; then
            newttl=$(jnum "$rj" ttl)
            days=$((newttl / 86400))
            action="renewed to ${days}d"
          else
            action="RENEW FAILED"
            lapsing=1
          fi
        else
          # A token at its maximum TTL has nothing to renew to; it has to be replaced.
          action="LAPSING (not renewable: use --replace)"
          lapsing=1
        fi
        ;;
      replace)
        cttl=$(jnum "$js" creation_ttl)
        case "$cttl" in ''|*[!0-9]*|0) cttl=2764800 ;; esac
        pol=$(compact "$js" | grep -o '"policies":\[[^]]*\]' | head -1 |
          sed 's/.*\[//; s/\]//; s/"//g' |
          tr ',' '\n' | grep -v '^default$' | tr '\n' ',' | sed 's/,$//')
        if [ -z "$pol" ]; then
          action="REPLACE FAILED (no policies to copy)"
          lapsing=1
        elif newt=$(vault token create -policy="$pol" -display-name="$n" -ttl="${cttl}s" -renewable -field=token 2>/dev/null) &&
             [ -n "$newt" ]; then
          if printf '%s' "$newt" > "$f.new" && chmod 600 "$f.new" && mv "$f.new" "$f"; then
            js2=$(vault token lookup -format=json "$newt" 2>/dev/null)
            newttl=$(jnum "$js2" ttl)
            days=$((newttl / 86400))
            action="replaced (${pol}; old token left to expire)"
            printf '%-10s %-6s %-10s %-26s %-6s %s\n' "$n" "$days" "$newttl" "$(jstr "$js2" expire_time)" "$(compact "$js2" | grep -o '"renewable":[a-z]*' | head -1 | cut -d: -f2)" "$action"
            continue
          else
            action="REPLACE FAILED (could not write $f)"
            lapsing=1
          fi
        else
          action="REPLACE FAILED (create refused)"
          lapsing=1
        fi
        ;;
      *)
        action="LAPSING"
        lapsing=1
        ;;
    esac
  fi

  printf '%-10s %-6s %-10s %-26s %-6s %s\n' "$n" "$days" "$ttl" "$exp" "$ren" "$action"
done

echo
if [ "$seen" -eq 0 ]; then
  echo "result: no token files matched"
  exit 2
fi
if [ "$failed" -eq 1 ]; then
  echo "result: at least one token is invalid or unreadable"
  exit 1
fi
if [ "$lapsing" -eq 1 ]; then
  echo "result: token(s) inside the ${WARN_DAYS}-day window need attention"
  exit 1
fi
echo "result: no token inside the ${WARN_DAYS}-day window"
exit 0
