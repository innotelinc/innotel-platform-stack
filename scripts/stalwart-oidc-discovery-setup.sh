#!/bin/bash
# Install the local discovery document for Stalwart's OIDC directory, prove it,
# and undo everything if the mail host does not come back healthy.
#
# Run as root inside the `mail` container, with the pieces beside it (a copy of
# this file's directory, or the repo's scripts/ + systemd/):
#   bash stalwart-oidc-discovery-setup.sh install
#   bash stalwart-oidc-discovery-setup.sh preflight   # read-only, changes nothing
#   bash stalwart-oidc-discovery-setup.sh rollback
#
# It installs from its own directory, not from /root: the 2026-10-25 /root cleanup
# deleted a script a unit exec'd and left that unit failing at exec for three weeks
# (docs/vault-replica-restore.md). Preflight below refuses to start half-armed.
#
# The pieces are deliberately small and each one is reversible:
#   /etc/stalwart-oidc/            cache, trust anchor, private hosts file
#   stalwart-oidc-proxy.service    serves the document, forwards the rest
#   stalwart.service.d/hosts.conf  gives Stalwart alone the local resolution
#   SSL_CERT_FILE in stalwart.env  trusts our anchor for that fetch
#   iptables OUTPUT DNAT           sends :443 for that name to the proxy
set -u
ETC=/etc/stalwart-oidc
UNIT_DIR=/etc/systemd/system
ACTION=${1:-install}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Where each piece we install may live: beside this script, or in the repo's
# systemd/ directory one level up from scripts/.
src() {
  for p in "$HERE/$1" "$HERE/../systemd/$1"; do
    [ -f "$p" ] && { printf '%s\n' "$p"; return 0; }
  done
  return 1
}

probe() {
  curl -s -o /dev/null -w '%{http_code}' -m 5 \
    -H 'Authorization: Bearer local-discovery-probe' http://127.0.0.1:8080/jmap/session 2>/dev/null
}

# Read-only go/no-go for the ordered change in docs/mail-discovery-canonical.md.
#
# Every line below is a fact step 1 of that runbook asks a person to read by hand, printed
# as one verdict — because the failure this whole change can cause is identical at every
# half-done step (a start-up fetch cached as failed, every bearer token 500, anonymous still
# 200), and it is cheapest to see before anything is touched. Nothing here writes, restarts
# or refreshes. It refuses to run outside the mail container: every path is that
# container's, so a verdict from anywhere else would be about nothing.
preflight() {
  # A directory is not proof of a container. The first version of this check tested only
  # `$ETC`, and the machine it was written on has one: a stale copy, the cerulean issuer,
  # an anchor to 2036, no units and nothing listening on 8080. It answered NOT OK, which
  # is a verdict about a system that is not the mail container — the one thing this verb
  # must never do. The file the install path edits and the unit it restarts are both
  # required, so a host that merely carries the directory is told so.
  if [ ! -d "$ETC" ] || [ ! -f /etc/stalwart/stalwart.env ]; then
    echo "CANNOT RUN: $ETC and /etc/stalwart/stalwart.env are not both here - this is not the mail container"
    exit 2
  fi
  if ! systemctl cat stalwart.service >/dev/null 2>&1; then
    echo "CANNOT RUN: this host does not run stalwart.service"
    exit 2
  fi
  rc=0
  # The verdict is per line, and it is written down rather than implied, because the same
  # output is read by a machine as well as a person: `check-stalwart-oidc-preflight.py`
  # streams this script into the container and turns every FAIL line into a finding. A
  # form where pass and fail print identically can only be judged by the exit code, which
  # says that something is wrong and never which of the eleven things.
  say() { printf '  %-4s %-22s  %s\n' ok "$1" "$2"; }
  need() { printf '  %-4s %-22s  %s\n' FAIL "$1" "$2"; rc=1; }

  echo "units"
  for unit in stalwart stalwart-oidc-proxy stalwart-oidc-redirect; do
    state=$(systemctl is-active "$unit" 2>/dev/null || true)
    if [ "$state" = "active" ]; then say "$unit" active; else need "$unit" "${state:-unknown}"; fi
  done

  echo "the copy"
  if [ -s "$ETC/cache/discovery.json" ]; then
    issuer=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["issuer"])' \
      "$ETC/cache/discovery.json" 2>/dev/null || true)
    if [ -n "$issuer" ]; then say "issuer served" "$issuer"; else need "issuer served" unreadable; fi
    mtime=$(stat -c %Y "$ETC/cache/discovery.json" 2>/dev/null || echo 0)
    age=$(( $(date +%s) - mtime ))
    if [ "$age" -le 1800 ]; then say "copy age" "${age}s"; else need "copy age" "${age}s, past two refreshes"; fi
  else
    need "copy" missing
  fi

  echo "the pieces"
  upstream=$(sed -n 's/^UPSTREAM=//p' /usr/local/bin/stalwart-oidc-discovery-proxy.py 2>/dev/null | head -1)
  : "${upstream:=auth.cerulean.innotel.us}"
  san=$(openssl x509 -in "$ETC/tls/server.pem" -noout -ext subjectAltName 2>/dev/null | tr -d '\n' || true)
  case "$san" in
    *"DNS:$upstream"*) say "leaf covers" "$upstream" ;;
    *) need "leaf covers" "$upstream: ${san:-unreadable}" ;;
  esac
  expires=$(openssl x509 -in "$ETC/tls/ca.pem" -noout -enddate 2>/dev/null | cut -d= -f2)
  say "anchor expires" "${expires:-unreadable}"
  rules=$(/usr/local/bin/stalwart-oidc-redirect.sh status 2>/dev/null | grep -c 8443 || true)
  if [ "${rules:-0}" -ge 2 ]; then say "dnat rules" "$rules"; else need "dnat rules" "${rules:-0}"; fi

  echo "the fetch and the server"
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
    "https://$upstream/application/o/stalwart-webui/.well-known/openid-configuration" 2>/dev/null)
  if [ "$code" = "200" ]; then say "provider from here" 200; else need "provider from here" "${code:-no answer}"; fi
  if su -s /bin/sh stalwart -c "test -r $ETC/ca-bundle.pem" 2>/dev/null; then
    say "bundle readable" stalwart
  else
    need "bundle readable" "not by stalwart"
  fi
  if su -s /bin/sh oidcproxy -c "test -r $ETC/cache/discovery.json" 2>/dev/null; then
    say "copy readable" oidcproxy
  else
    need "copy readable" "not by oidcproxy"
  fi
  bearer=$(probe)
  if [ "$bearer" = "401" ]; then say "bearer probe" 401; else need "bearer probe" "${bearer:-no answer}"; fi

  if [ "$rc" -eq 0 ]; then
    echo "PREFLIGHT OK: every piece the switch depends on is here and working"
  else
    echo "PREFLIGHT NOT OK: fix the lines above before changing anything"
  fi
  return "$rc"
}

rollback() {
  echo "ROLLBACK: restoring the previous state"
  systemctl disable --now stalwart-oidc-proxy >/dev/null 2>&1
  systemctl disable --now stalwart-oidc-redirect >/dev/null 2>&1
  /usr/local/bin/stalwart-oidc-redirect.sh remove >/dev/null 2>&1
  rm -f "$UNIT_DIR/stalwart-oidc-proxy.service" "$UNIT_DIR/stalwart-oidc-redirect.service"
  rm -f "$UNIT_DIR/stalwart.service.d/hosts.conf"
  if [ -f /etc/stalwart/stalwart.env.pre-oidc-cache ]; then
    cp -a /etc/stalwart/stalwart.env.pre-oidc-cache /etc/stalwart/stalwart.env
  fi
  systemctl daemon-reload
  systemctl restart stalwart
  sleep 8
  echo "after rollback: bearer probe HTTP $(probe) (401 is healthy)"
}

if [ "$ACTION" = "preflight" ]; then preflight; exit $?; fi
if [ "$ACTION" = "rollback" ]; then rollback; exit 0; fi

set -e

# Install only, and before anything is changed: the pieces have to be here, or
# the run would get halfway and leave the container with a proxy unit and no
# script to serve. Rollback above deliberately does not need them.
PIECES="stalwart-oidc-redirect.sh stalwart-oidc-discovery-proxy.py stalwart-oidc-proxy.service stalwart-oidc-redirect.service"
missing=""
for piece in $PIECES; do
  src "$piece" >/dev/null || missing="$missing $piece"
done
if [ -n "$missing" ]; then
  echo "CANNOT RUN: missing$missing"
  echo "looked in $HERE and $(cd "$HERE/.." 2>/dev/null && pwd)/systemd"
  echo "copy the repo's scripts/ and systemd/ together, or run from a checkout"
  exit 1
fi
id oidcproxy >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin oidcproxy
install -d -m 0755 -o root -g root "$ETC"
install -d -m 0750 -o root -g oidcproxy "$ETC/tls"
install -d -m 0750 -o oidcproxy -g oidcproxy "$ETC/cache"

if [ ! -f "$ETC/tls/ca.pem" ]; then
  echo "generating a local trust anchor for auth.cerulean.innotel.us"
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
    -keyout "$ETC/tls/ca.key" -out "$ETC/tls/ca.pem" \
    -subj "/CN=Stalwart OIDC local discovery CA" \
    -addext "basicConstraints=critical,CA:TRUE" >/dev/null 2>&1
  openssl req -newkey rsa:2048 -nodes -keyout "$ETC/tls/server.key" \
    -out "$ETC/tls/server.csr" -subj "/CN=auth.cerulean.innotel.us" >/dev/null 2>&1
  printf 'subjectAltName=DNS:auth.cerulean.innotel.us\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n' > "$ETC/tls/server.ext"
  openssl x509 -req -in "$ETC/tls/server.csr" -CA "$ETC/tls/ca.pem" -CAkey "$ETC/tls/ca.key" \
    -CAcreateserial -out "$ETC/tls/server.pem" -days 3650 -extfile "$ETC/tls/server.ext" >/dev/null 2>&1
  rm -f "$ETC/tls/server.csr" "$ETC/tls/server.ext"
fi
chown root:oidcproxy "$ETC/tls"/*.pem "$ETC/tls"/*.key
chmod 0640 "$ETC/tls/server.key" "$ETC/tls/ca.key"
chmod 0644 "$ETC/tls/server.pem" "$ETC/tls/ca.pem"
cat /etc/ssl/certs/ca-certificates.crt "$ETC/tls/ca.pem" > "$ETC/ca-bundle.pem"
chmod 0644 "$ETC/ca-bundle.pem"

# One cached copy, so the very first start has something to serve.
if [ ! -s "$ETC/cache/discovery.json" ]; then
  curl -s -m 15 -o "$ETC/cache/discovery.json" \
    https://auth.cerulean.innotel.us/application/o/stalwart-webui/.well-known/openid-configuration
  chown oidcproxy:oidcproxy "$ETC/cache/discovery.json"
  chmod 0644 "$ETC/cache/discovery.json"
fi
python3 -c 'import json,sys;d=json.load(open("/etc/stalwart-oidc/cache/discovery.json"));assert d["issuer"] and d["jwks_uri"]' \
  || { echo "cached document is not usable"; exit 1; }
chown oidcproxy:oidcproxy "$ETC/cache/discovery.json"
  chmod 0644 "$ETC/cache/discovery.json"

grep -q 'auth.cerulean.innotel.us' "$ETC/hosts" 2>/dev/null || {
  cp /etc/hosts "$ETC/hosts"
  echo '127.0.0.1 auth.cerulean.innotel.us' >> "$ETC/hosts"
}
chmod 0644 "$ETC/hosts"

install -m 0755 "$(src stalwart-oidc-redirect.sh)" /usr/local/bin/stalwart-oidc-redirect.sh
install -m 0755 "$(src stalwart-oidc-discovery-proxy.py)" /usr/local/bin/stalwart-oidc-discovery-proxy.py
install -m 0644 "$(src stalwart-oidc-proxy.service)" "$UNIT_DIR/stalwart-oidc-proxy.service"
install -m 0644 "$(src stalwart-oidc-redirect.service)" "$UNIT_DIR/stalwart-oidc-redirect.service"

install -d "$UNIT_DIR/stalwart.service.d"
cat > "$UNIT_DIR/stalwart.service.d/hosts.conf" <<'EOF'
# Give Stalwart alone a hosts file that points its IdP at the local discovery
# cache (2026-11-09). Other processes in the container keep the real one.
[Service]
BindReadOnlyPaths=/etc/stalwart-oidc/hosts:/etc/hosts
EOF

[ -f /etc/stalwart/stalwart.env.pre-oidc-cache ] || cp -a /etc/stalwart/stalwart.env /etc/stalwart/stalwart.env.pre-oidc-cache
grep -q '^SSL_CERT_FILE=' /etc/stalwart/stalwart.env \
  || echo "SSL_CERT_FILE=$ETC/ca-bundle.pem" >> /etc/stalwart/stalwart.env

systemctl daemon-reload
systemctl enable --now stalwart-oidc-proxy
systemctl enable --now stalwart-oidc-redirect
sleep 2
systemctl restart stalwart
sleep 10

echo "--- bearer probe: HTTP $(probe) (401 is healthy)"
echo "--- journal since start:"
journalctl -u stalwart --since "-90s" --no-pager 2>/dev/null | grep -iE 'directory|discovery|issuer' | tail -6
if [ "$(probe)" != "401" ]; then
  journalctl -u stalwart --since "-120s" --no-pager | tail -20
  rollback
  exit 1
fi
echo "INSTALL OK"
