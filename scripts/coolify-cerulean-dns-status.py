#!/usr/bin/env python3
"""One view of every Coolify domain and the Cerulean record behind it.

For troubleshooting without reading the publisher's logic: each hostname Coolify
is configured for, the record it should have, what Cerulean actually holds, and
whether those agree. `--json` is the same data for a script.

    ./coolify-cerulean-dns-status.py
    ./coolify-cerulean-dns-status.py --detail   # + which Coolify resource, when it last changed
    ./coolify-cerulean-dns-status.py --json
"""
import importlib.util
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))


def publisher():
    spec = importlib.util.spec_from_file_location("coolify_cerulean_dns", os.path.join(HERE, "coolify-cerulean-dns.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Where each kind of Coolify domain comes from, with the resource it belongs to and
#: the row's `updated_at` — the closest thing Coolify keeps to "when this domain last
#: changed". Only queried for --detail; the plain view stays a single fast pass.
DETAIL_QUERIES = {
    "instance": """
        select coalesce(json_agg(json_build_object('fqdn', fqdn, 'resource', 'instance',
               'updated_at', updated_at)), '[]')
        from instance_settings
        where coalesce(fqdn, '') <> ''
    """,
    "application": """
        select coalesce(json_agg(json_build_object('fqdn', a.fqdn, 'resource', a.name,
               'updated_at', a.updated_at)), '[]')
        from applications a
        where a.deleted_at is null and coalesce(a.fqdn, '') <> ''
    """,
    "preview": """
        select coalesce(json_agg(json_build_object('fqdn', p.fqdn, 'resource', a.name || ' (preview)',
               'updated_at', p.updated_at)), '[]')
        from application_previews p
        join applications a on a.id = p.application_id
        where p.deleted_at is null and coalesce(p.fqdn, '') <> ''
    """,
    "service": """
        select coalesce(json_agg(json_build_object('fqdn', sa.fqdn, 'resource', s.name,
               'updated_at', sa.updated_at)), '[]')
        from service_applications sa
        join services s on s.id = sa.service_id
        where sa.deleted_at is null and coalesce(sa.fqdn, '') <> ''
    """,
}


def resource_index(pub):
    """hostname -> (Coolify resource name, when that row last changed)."""
    index = {}
    for sql in DETAIL_QUERIES.values():
        for row in pub.coolify_json(sql) or []:
            for host in pub.hostnames(row.get("fqdn")):
                index.setdefault(host, (row.get("resource") or "-", row.get("updated_at")))
    return index


def when(value):
    """`2026-11-09T03:30:09.000000Z` -> `2026-11-09 03:30`; a missing stamp is just `-`."""
    text = str(value or "").rstrip("Z")
    if "T" in text:
        text = text.replace("T", " ", 1)
    return text[:16] or "-"


def main():
    as_json = "--json" in sys.argv
    detail = "--detail" in sys.argv
    pub = publisher()
    notes = []
    wanted = pub.expected_records(notify=notes.append)
    base, token = pub.load_cerulean_config()

    details = resource_index(pub) if detail else {}
    zone_records = {}
    rows = []
    for (zone, host), want in sorted(wanted.items()):
        if zone not in zone_records:
            zone_records[zone] = pub.cerulean(
                "GET", f"{base}/api/service/dns/records?zone={urllib.parse.quote(zone)}", token
            ) or []
        same = [r for r in zone_records[zone] if r["type"] == want["type"] and r["name"].lower() == host]
        if not same:
            state = "missing"
        elif any(pub.normalize(r["value"]) == pub.normalize(want["value"]) for r in same):
            state = "ok"
        else:
            state = "mismatch"
        row = {
            "host": host,
            "kind": want["kind"],
            "server": want["server"],
            "zone": zone,
            "expected": f"{want['type']} {want['value']}",
            "cerulean": ", ".join(f"{r['type']} {r['value']}" for r in same) or "-",
            "state": state,
        }
        if detail:
            resource, updated = details.get(host, ("-", None))
            row["resource"] = resource
            row["changed"] = when(updated)
        rows.append(row)

    if as_json:
        print(json.dumps({"domains": rows, "notes": notes}, indent=2))
        return 0

    if rows and detail:
        header = (f"{'HOST':34} {'RESOURCE':24} {'CHANGED':16} {'SRV':>3}  {'ZONE':16} "
                  f"{'EXPECTED':26} {'CERULEAN':26} STATE")
        print(header)
        print("-" * len(header))
        for row in rows:
            print(f"{row['host']:34} {row['resource'][:24]:24} {row['changed']:16} {row['server']:>3}  "
                  f"{row['zone']:16} {row['expected']:26} {row['cerulean']:26} {row['state']}")
    elif rows:
        header = f"{'HOST':34} {'RESOURCE':11} {'SRV':>3}  {'ZONE':16} {'EXPECTED':26} {'CERULEAN':26} STATE"
        print(header)
        print("-" * len(header))
        for row in rows:
            print(f"{row['host']:34} {row['kind']:11} {row['server']:>3}  {row['zone']:16} "
                  f"{row['expected']:26} {row['cerulean']:26} {row['state']}")
    else:
        print("no Coolify domains resolve into a Cerulean zone")
    for note in notes:
        print(note)
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    print("\n" + (", ".join(f"{count} {state}" for state, count in sorted(counts.items())) or "nothing to publish"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
