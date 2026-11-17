#!/bin/sh
# Materialise the Mailjet credentials from cerulean-vault into a runtime env file.
#
# Reads the AppRole credentials from /etc/oasis/vault-mailjet.env, logs in to the
# cerulean-vault on the edge, and atomically rewrites /run/mailjet/mailjet.env.
# The secret itself is only ever held in memory and in that 0600 runtime file;
# nothing is written to stdout or to the journal.
set -eu

ENV_FILE=/etc/oasis/vault-mailjet.env
OUT=/run/mailjet/mailjet.env

[ -r "$ENV_FILE" ] || { echo "vault-mailjet-env: missing $ENV_FILE" >&2; exit 1; }
set -a
. "$ENV_FILE"
set +a
: "${VAULT_ADDR:?VAULT_ADDR missing}"
: "${VAULT_APPROLE_ROLE_ID:?VAULT_APPROLE_ROLE_ID missing}"
: "${VAULT_APPROLE_SECRET_ID:?VAULT_APPROLE_SECRET_ID missing}"

login=$(curl -fsS --max-time 10 -X POST \
  --data "{\"role_id\":\"$VAULT_APPROLE_ROLE_ID\",\"secret_id\":\"$VAULT_APPROLE_SECRET_ID\"}" \
  "$VAULT_ADDR/v1/auth/approle/login") || {
  echo "vault-mailjet-env: approle login failed" >&2
  exit 1
}
token=$(printf '%s' "$login" | python3 -c 'import json,sys;print(json.load(sys.stdin)["auth"]["client_token"])') || {
  echo "vault-mailjet-env: no client token in login response" >&2
  exit 1
}

tmp=$(mktemp /run/mailjet/.secret.XXXXXX 2>/dev/null) || {
  mkdir -p /run/mailjet
  tmp=$(mktemp /run/mailjet/.secret.XXXXXX)
}
trap 'rm -f "$tmp"' EXIT INT TERM

curl -fsS --max-time 10 -H "X-Vault-Token: $token" \
  "$VAULT_ADDR/v1/cerulean/data/mailjet" -o "$tmp" || {
  echo "vault-mailjet-env: secret read failed" >&2
  exit 1
}

python3 - "$tmp" "$OUT" <<'PY' || exit 1
import json, os, sys

raw = json.load(open(sys.argv[1]))["data"]["data"]
names = {
    "api_key": "MAILJET_API_KEY",
    "secret_key": "MAILJET_SECRET_KEY",
    "inbound_user": "MAILJET_INBOUND_USER",
    "inbound_password": "MAILJET_INBOUND_PASSWORD",
}
missing = [k for k in names if not raw.get(k)]
if missing:
    sys.stderr.write("vault-mailjet-env: missing fields %s\n" % ",".join(missing))
    sys.exit(1)

out = sys.argv[2] + ".tmp"
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    for key, var in names.items():
        f.write("%s=%s\n" % (var, raw[key]))
os.replace(out, sys.argv[2])
PY

rm -f "$tmp"
trap - EXIT INT TERM
chmod 600 "$OUT"
