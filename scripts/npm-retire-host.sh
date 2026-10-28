#!/bin/sh
# List or disable Nginx Proxy Manager proxy hosts by domain, straight through the
# npm-db MySQL container. Run inside the `proxy` container:
#   incus exec proxy -- sh /root/npm-retire-host.sh list   '%zeus%'
#   incus exec proxy -- sh /root/npm-retire-host.sh disable '%zeus%'
# Disabling sets proxy_host.enabled=0 and restarts NPM so it regenerates the
# nginx configs; the row stays, so `enable` puts it back.
#
# Three things that cost real time on 2026-10-27:
#   * Run it on the host that actually owns the live proxy. `proxy` is STOPPED on
#     i4 (a leftover copy) and RUNNING on i2; editing the i4 one changes nothing.
#   * Inside the `proxy` container, /data is a stale decoy. The tree nginx reads
#     is /usr/src/proxy/data/nginx (bind-mounted to /data inside the NPM container).
#   * `docker restart` is not optional HERE, but for a different reason than it used
#     to be. This writes the database directly and NPM regenerates
#     /data/nginx/*.conf only on boot — a plain `nginx -s reload` re-reads config
#     files, not the DB. (Separately: the SIGHUP reload itself used to be impossible
#     — nginx started with nofile=1024 against ~570 open fds, so every reload died
#     with "[emerg] open() ... failed (24: Too many open files)" and left the old
#     config serving. Fixed 2026-10-27 by `ulimits: nofile 65536:65536` on
#     cerulean-npm in /usr/src/proxy/docker-compose.cerulean.yml, so API-driven host
#     changes now apply in place instead of needing a container restart.)
set -e

ACTION=${1:-list}
PATTERN=${2:-}

[ -n "$PATTERN" ] || { echo "usage: $0 list|disable|enable <sql-like-pattern>" >&2; exit 1; }

SQL="select id, domain_names, enabled from npm.proxy_host where domain_names like '$PATTERN';"

case "$ACTION" in
  list)
    docker exec cerulean-npm-db sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -e \"$SQL\""
    ;;
  disable)
    docker exec cerulean-npm-db sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -e \"update npm.proxy_host set enabled=0 where domain_names like '$PATTERN'; select id, domain_names, enabled from npm.proxy_host where domain_names like '$PATTERN';\""
    echo "restarting NPM to regenerate configs"
    docker restart cerulean-npm >/dev/null
    sleep 8
    ;;
  enable)
    docker exec cerulean-npm-db sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -e \"update npm.proxy_host set enabled=1 where domain_names like '$PATTERN'; select id, domain_names, enabled from npm.proxy_host where domain_names like '$PATTERN';\""
    echo "restarting NPM to regenerate configs"
    docker restart cerulean-npm >/dev/null
    sleep 8
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 1
    ;;
esac
