#!/bin/bash
# Install the local discovery document for Stalwart's OIDC directory, prove it,
# and undo everything if the mail host does not come back healthy.
#
# Run as root inside the `mail` container:
#   bash stalwart-oidc-discovery-setup.sh install
#   bash stalwart-oidc-discovery-setup.sh rollback
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
STAMP=$(date +%Y%m%dT%H%M%SZ)
ACTION=${1:-install}

probe() {
  curl -s -o /dev/null -w '%{http_code}' -m 5 \
    -H 'Authorization: Bearer local-discovery-probe' http://127.0.0.1:8080/jmap/session 2>/dev/null
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

if [ "$ACTION" = "rollback" ]; then rollback; exit 0; fi

set -e
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

install -m 0755 /root/stalwart-oidc-redirect.sh /usr/local/bin/stalwart-oidc-redirect.sh
install -m 0755 /root/stalwart-oidc-discovery-proxy.py /usr/local/bin/stalwart-oidc-discovery-proxy.py
install -m 0644 /root/stalwart-oidc-proxy.service "$UNIT_DIR/stalwart-oidc-proxy.service"
install -m 0644 /root/stalwart-oidc-redirect.service "$UNIT_DIR/stalwart-oidc-redirect.service"

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
