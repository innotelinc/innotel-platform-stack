#!/usr/bin/env python3
"""Publish Coolify's addresses into Cerulean DNS instead of Cloudflare.

Coolify's managed-DNS feature speaks only to Cloudflare. The estate's DNS lives
in Cerulean (Technitium), so the addresses Coolify would have published there
are published here instead: every hostname Coolify has configured for an
application, an application preview or a service application becomes an A
record in the Cerulean zone that owns it, pointing at the public address of the
server the resource runs on.

Records this script creates are remembered in STATE_FILE and are the only ones
it ever deletes; a record it did not create is reported, never overwritten.

Credentials come from CERULEAN_URL + CERULEAN_TOKEN in the environment, or from
the same keys in ~/.cerulean-coolify.

Usage:
  coolify-cerulean-dns.py            # dry run: print what would change
  coolify-cerulean-dns.py --apply    # make the changes
"""
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TTL = 300
STATE_FILE = os.environ.get("COOLIFY_CERULEAN_STATE", "/var/lib/coolify-cerulean-dns/state.json")
CERULEAN_ENV = os.path.expanduser("~/.cerulean-coolify")
ALIASES_FILE = os.environ.get("COOLIFY_CERULEAN_ALIASES", "/etc/innotel/coolify-cerulean-dns.aliases")
DB_CONTAINER = os.environ.get("COOLIFY_DB_CONTAINER", "coolify-db")
DB_USER = os.environ.get("COOLIFY_DB_USER", "coolify")
DB_NAME = os.environ.get("COOLIFY_DB_NAME", "coolify")

# Names that are not real DNS: Coolify's magic wildcard services and localhost.
SKIP_SUFFIXES = (".sslip.io", ".nip.io", ".localhost", ".local", ".internal.test")


def load_cerulean_config():
    values = {}
    if os.path.exists(CERULEAN_ENV):
        with open(CERULEAN_ENV) as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
    url = os.environ.get("CERULEAN_URL") or values.get("CERULEAN_URL")
    token = os.environ.get("CERULEAN_TOKEN") or values.get("CERULEAN_TOKEN")
    if not url or not token:
        raise SystemExit(f"missing CERULEAN_URL/CERULEAN_TOKEN (env or {CERULEAN_ENV})")
    return url.rstrip("/"), token


def cerulean(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    last = None
    # Cerulean's larger zones can take a moment to serialise; a dropped connection is
    # worth retrying, a bad answer is not.
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read().decode()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode()[:300]
            raise RuntimeError(f"{method} {path}: HTTP {error.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = error
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{method} {path}: {last}")


def coolify_json(sql):
    """Read Coolify's own database: one read-only query, one JSON value."""
    result = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", sql],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"coolify query failed: {result.stderr.strip()[:300]}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


def coolify_resources():
    """(hostname, server_id, what) for every domain Coolify has configured."""
    queries = {
        "instance": """
            select coalesce(json_agg(json_build_object('fqdn', fqdn, 'server', 0)), '[]')
            from instance_settings
            where coalesce(fqdn, '') <> ''
        """,
        "application": """
            select coalesce(json_agg(json_build_object('fqdn', a.fqdn,
                   'server', coalesce(sd.server_id, sw.server_id, 0))), '[]')
            from applications a
            left join standalone_dockers sd on sd.id = a.destination_id
            left join swarm_dockers sw on sw.id = a.destination_id
            where a.deleted_at is null and coalesce(a.fqdn, '') <> ''
        """,
        "preview": """
            select coalesce(json_agg(json_build_object('fqdn', p.fqdn,
                   'server', coalesce(sd.server_id, sw.server_id, 0))), '[]')
            from application_previews p
            join applications a on a.id = p.application_id
            left join standalone_dockers sd on sd.id = a.destination_id
            left join swarm_dockers sw on sw.id = a.destination_id
            where p.deleted_at is null and coalesce(p.fqdn, '') <> ''
        """,
        "service": """
            select coalesce(json_agg(json_build_object('fqdn', sa.fqdn, 'server', s.server_id)), '[]')
            from service_applications sa
            join services s on s.id = sa.service_id
            where sa.deleted_at is null and coalesce(sa.fqdn, '') <> ''
        """,
    }
    rows = []
    for kind, sql in queries.items():
        for row in coolify_json(sql) or []:
            for host in hostnames(row.get("fqdn")):
                rows.append((host, int(row.get("server") or 0), kind))
    return rows


def hostnames(fqdn_field):
    """Coolify keeps a comma-separated list of URLs in the fqdn column."""
    hosts = []
    for entry in str(fqdn_field or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "://" not in entry:
            entry = "http://" + entry
        host = urllib.parse.urlsplit(entry).hostname
        if host:
            hosts.append(host.lower().rstrip("."))
    return hosts


def public_addresses():
    """server_id -> IPv4 address a DNS record may point at."""
    settings = coolify_json(
        "select json_build_object('public_ipv4', public_ipv4, 'public_ipv6', public_ipv6) from instance_settings limit 1"
    ) or {}
    # The localhost server's ip is host.docker.internal, so its records point at the instance's public address.
    addresses = {0: settings.get("public_ipv4") if is_public(settings.get("public_ipv4")) else None}
    for server in coolify_json("select coalesce(json_agg(json_build_object('id', id, 'ip', ip)), '[]') from servers") or []:
        server_id = int(server["id"])
        if is_public(server.get("ip")):
            addresses[server_id] = server["ip"]
        elif server_id != 0:
            addresses[server_id] = None
    return {key: value for key, value in addresses.items() if value}


def is_public(address):
    try:
        ip = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global


def zone_for(host, zones):
    """The most specific Cerulean zone that owns the hostname."""
    matches = [zone for zone in zones if host == zone or host.endswith("." + zone)]
    return max(matches, key=len) if matches else None


def record_name(host, zone):
    return "@" if host == zone else host[: -len(zone) - 1]


def normalize(value):
    """Compare record content the way DNS does: case-insensitively, without the trailing dot."""
    return str(value or "").strip().rstrip(".").lower()


def aliases():
    """hostname -> CNAME target from ALIASES_FILE (one `alias -> target` per line).

    A Coolify domain that is really another name (a `www`, a short vanity host)
    should follow that name rather than duplicate its address, so the address
    changes in one place.
    """
    mapping = {}
    try:
        with open(ALIASES_FILE) as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if "->" not in line:
                    continue
                name, target = (part.strip().lower().rstrip(".") for part in line.split("->", 1))
                if name and target:
                    mapping[name] = target
    except FileNotFoundError:
        pass
    return mapping


def load_state():
    try:
        with open(STATE_FILE) as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return {"records": []}


def save_state(records):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w") as handle:
        json.dump({"records": sorted(records, key=lambda r: (r["zone"], r["name"], r["value"]))}, handle, indent=2)
    os.replace(tmp, STATE_FILE)


def expected_records(notify=None):
    """(zone, hostname) -> the record Coolify's configuration calls for.

    `notify` receives one line per hostname that is deliberately not published
    (a magic sslip.io name, a zone Cerulean does not serve, a server without a
    public IPv4, two servers claiming one name). The health check shares this
    with the publisher so both agree on what "should exist".
    """
    base, token = load_cerulean_config()
    zones = sorted({d["name"].lower() for d in cerulean("GET", f"{base}/api/service/domains", token) or []})
    addresses = public_addresses()
    alias_map = aliases()
    wanted = {}
    for host, server, kind in coolify_resources():
        if host.startswith("*") or any(host.endswith(suffix) for suffix in SKIP_SUFFIXES):
            continue
        zone = zone_for(host, zones)
        if not zone:
            if notify:
                notify(f"skip   {host} ({kind}): no Cerulean zone owns it")
            continue
        alias = alias_map.get(host)
        address = addresses.get(server)
        if alias:
            want = {"zone": zone, "name": record_name(host, zone), "value": alias, "type": "CNAME",
                    "kind": kind, "server": server}
        elif address:
            want = {"zone": zone, "name": record_name(host, zone), "value": address, "type": "A",
                    "kind": kind, "server": server}
        else:
            if notify:
                notify(f"skip   {host} ({kind}): server {server} has no public IPv4")
            continue
        existing = wanted.get((zone, host))
        if existing and normalize(existing["value"]) != normalize(want["value"]):
            if notify:
                notify(f"skip   {host} ({kind}): {want['type']} {want['value']} is already claimed by "
                       f"{existing['type']} {existing['value']} from server {existing['server']}")
            continue
        wanted[(zone, host)] = want
    return wanted


def main():
    apply = "--apply" in sys.argv
    base, token = load_cerulean_config()
    wanted = expected_records(notify=print)
    state = load_state()
    owned = {(r["zone"], r["name"], r.get("type", "A"), normalize(r["value"])) for r in state["records"]}
    zone_records = {}

    def records_in(zone):
        if zone not in zone_records:
            zone_records[zone] = cerulean("GET", f"{base}/api/service/dns/records?zone={urllib.parse.quote(zone)}", token) or []
        return zone_records[zone]

    def forget(zone, host, record):
        owned.discard((zone, host, record["type"], normalize(record["value"])))
        state["records"] = [
            r for r in state["records"]
            if not (r["zone"] == zone and r["name"].lower() == host
                    and r.get("type", "A") == record["type"]
                    and normalize(r["value"]) == normalize(record["value"]))
        ]

    def drop(zone, host, record):
        cerulean("DELETE", f"{base}/api/service/dns/records", token,
                 {"zone": zone, "type": record["type"], "name": record["name"], "value": record["value"]})
        forget(zone, host, record)

    created = kept = 0
    for (zone, host), want in sorted(wanted.items()):
        at_host = [r for r in records_in(zone) if r["name"].lower() == host]
        exact = [r for r in at_host
                 if r["type"] == want["type"] and normalize(r["value"]) == normalize(want["value"])]
        owned_here = [r for r in at_host if (zone, host, r["type"], normalize(r["value"])) in owned]
        if exact:
            # The wanted record exists; drop only owned leftovers (a switch from A to CNAME leaves one).
            for record in [r for r in owned_here if r not in exact]:
                print(f"-      {host} {record['type']} {record['value']} ({want['kind']}, zone {zone}): superseded")
                if apply:
                    drop(zone, host, record)
            if apply:
                zone_records.pop(zone, None)
            kept += 1
            continue
        conflicts = [r for r in at_host if r["type"] in ("A", "CNAME")
                     and (zone, host, r["type"], normalize(r["value"])) not in owned]
        if conflicts:
            shown = ", ".join(record["type"] + " " + record["value"] for record in conflicts)
            print(f"hold   {host} {want['type']} {want['value']} ({want['kind']}): {host} already has "
                  f"{shown} that this script did not create")
            continue
        print(f"{'move ' if owned_here else '+    '} {host} {want['type']} {want['value']} ({want['kind']}, zone {zone})")
        if not apply:
            continue
        for record in owned_here:
            drop(zone, host, record)
        cerulean("POST", f"{base}/api/service/dns/records", token,
                 {"zone": zone, "type": want["type"], "name": host, "value": want["value"], "ttl": TTL})
        zone_records.pop(zone, None)
        state["records"].append({"zone": zone, "name": host, "type": want["type"], "value": want["value"]})
        created += 1

    removed = 0
    desired = {(zone, host): (want["type"], normalize(want["value"])) for (zone, host), want in wanted.items()}
    for record in list(state["records"]):
        key = (record["zone"], record["name"].lower())
        if desired.get(key) == (record.get("type", "A"), normalize(record["value"])):
            continue
        print(f"-      {record['name']} {record.get('type', 'A')} {record['value']} (zone {record['zone']}): no longer configured in Coolify")
        if not apply:
            continue
        try:
            cerulean("DELETE", f"{base}/api/service/dns/records", token,
                     {"zone": record["zone"], "type": record.get("type", "A"),
                      "name": record["name"], "value": record["value"]})
        except RuntimeError as error:
            print(f"       could not remove: {error}")
            continue
        state["records"].remove(record)
        removed += 1

    if apply:
        save_state(state["records"])
    print(f"\n{'applied' if apply else 'dry run'}: {len(wanted)} configured, {created} created, "
          f"{removed} removed, {kept} already correct")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f"error: {error}")
