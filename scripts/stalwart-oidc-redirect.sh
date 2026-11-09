#!/bin/sh
# Point Stalwart's discovery fetch at the local cache (2026-11-09).
#
# Installed at /usr/local/bin/stalwart-oidc-redirect.sh in the `mail` container
# and driven by stalwart-oidc-proxy.service. Scope is deliberately narrow:
#   * this process's own connections are exempt (`-m owner`), so its refresh
#     still reaches the real IdP;
#   * 127.0.0.1:443 covers Stalwart's private hosts file (only Stalwart uses it);
#   * the IdP's own addresses cover the case where Stalwart resolves the name
#     itself instead of reading /etc/hosts.
# Everything else in the container is untouched.
set -u
UPSTREAM=auth.cerulean.innotel.us
PORT=8443
ACTION=${1:-install}

add() { iptables -t nat -C OUTPUT "$@" 2>/dev/null || iptables -t nat -A OUTPUT "$@"; }
drop() { while iptables -t nat -C OUTPUT "$@" 2>/dev/null; do iptables -t nat -D OUTPUT "$@" 2>/dev/null; done; }

addresses() { getent ahostsv4 "$UPSTREAM" 2>/dev/null | awk '{print $1}' | sort -u; }

case "$ACTION" in
  install)
    add -m owner --uid-owner oidcproxy -j RETURN
    add -d 127.0.0.1 -p tcp --dport 443 -j DNAT --to-destination "127.0.0.1:$PORT"
    for ip in $(addresses); do
      add -d "$ip" -p tcp --dport 443 -j DNAT --to-destination "127.0.0.1:$PORT"
    done
    ;;
  remove)
    for ip in $(addresses); do
      drop -d "$ip" -p tcp --dport 443 -j DNAT --to-destination "127.0.0.1:$PORT"
    done
    drop -d 127.0.0.1 -p tcp --dport 443 -j DNAT --to-destination "127.0.0.1:$PORT"
    drop -m owner --uid-owner oidcproxy -j RETURN
    ;;
  status)
    iptables -t nat -S OUTPUT | grep -E 'oidcproxy|8443' || echo "(no rules)"
    ;;
  *) echo "usage: $0 install|remove|status" >&2; exit 2 ;;
esac
