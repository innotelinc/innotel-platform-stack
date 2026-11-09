#!/usr/bin/env python3
"""Alert when a domain Coolify serves has no matching Cerulean record.

The check compares what Coolify is configured for (the same computation the
publisher uses, so the two cannot disagree) against the authoritative Cerulean
zone, then asks Cerulean's own resolver whether each name actually answers with
the expected address or follows the expected CNAME: a record can sit in the zone
and still not resolve (a same-name A beside the CNAME, a zone that never
reloaded). It deliberately does not query public resolvers — propagation beyond
Cerulean is Cerulean's business, and alerting on it here would be about something
this host cannot fix. Set COOLIFY_CERULEAN_RESOLVE=0 to skip resolution checks,
or CERULEAN_DNS_SERVER to name the resolver (default: the CERULEAN_URL host).

Exit status is 0 when every domain matches, 1 when any does not ("a reconcile
is missing"), 2 when the check itself could not run. One mail per run is sent
to CERULEAN_DNS_ALERT_TO through CERULEAN_DNS_SMTP_HOST when drift is found;
with the address unset the failure is only in the journal and the failed unit,
which is also how `systemctl --failed` notices it.
"""
import hashlib
import importlib.util
import json
import os
import smtplib
import subprocess
import sys
import time
import urllib.parse
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
SMTP_HOST = os.environ.get("CERULEAN_DNS_SMTP_HOST", "192.168.1.15")
SMTP_PORT = int(os.environ.get("CERULEAN_DNS_SMTP_PORT", "25"))
MAIL_FROM = os.environ.get("CERULEAN_DNS_MAIL_FROM", "coolify-dns@innotel.us")
ALERT_STATE = os.environ.get("COOLIFY_CERULEAN_HEALTH_STATE", "/var/lib/coolify-cerulean-dns/health.json")
#: One broken domain must not mail every 15 minutes; the same drift repeats after this.
REPEAT_HOURS = float(os.environ.get("CERULEAN_DNS_ALERT_REPEAT_HOURS", "12"))
#: The resolver that answers from Cerulean's zones (default: the CERULEAN_URL host).
DNS_SERVER = os.environ.get("CERULEAN_DNS_SERVER", "").strip()
RESOLVE = os.environ.get("COOLIFY_CERULEAN_RESOLVE", "1").strip().lower() not in ("0", "no", "false")


def publisher():
    spec = importlib.util.spec_from_file_location("coolify_cerulean_dns", os.path.join(HERE, "coolify-cerulean-dns.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alert(subject, body):
    to = os.environ.get("CERULEAN_DNS_ALERT_TO", "").strip()
    if not to:
        print("alert: CERULEAN_DNS_ALERT_TO is unset; the failure is only in this log", file=sys.stderr)
        return
    message = EmailMessage()
    message["From"] = MAIL_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as relay:
            relay.send_message(message)
        print(f"alert: mailed to {to} via {SMTP_HOST}")
    except Exception as error:  # a broken alert channel must not hide the findings above
        print(f"alert: could not mail {to} via {SMTP_HOST}: {error}", file=sys.stderr)


def dig(server, name, rtype):
    """The answers Cerulean's resolver gives for one name, without a resolver library."""
    result = subprocess.run(
        ["dig", "+short", "+time=3", "+tries=2", f"@{server}", name, rtype],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200] or f"dig exited {result.returncode}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip() and not line.startswith(";")]


def resolution_problems(pub, wanted, base):
    """Names whose zone record is right but which Cerulean's resolver does not answer."""
    if not RESOLVE:
        print("note: COOLIFY_CERULEAN_RESOLVE=0, resolution check skipped")
        return []
    server = DNS_SERVER or urllib.parse.urlsplit(base).hostname or ""
    if not server:
        print("note: no resolver to ask (set CERULEAN_DNS_SERVER), resolution check skipped")
        return []

    problems = []
    for (zone, host), want in sorted(wanted.items()):
        try:
            if want["type"] == "CNAME":
                follows = {pub.normalize(answer) for answer in dig(server, host, "CNAME")}
                if pub.normalize(want["value"]) not in follows:
                    shown = ", ".join(sorted(follows)) or "no answer"
                    problems.append(f"{host}: Cerulean has CNAME {want['value']}, but {server} answers "
                                    f"{shown} for {host} ({want['kind']})")
                    continue
                if not dig(server, host, "A"):
                    problems.append(f"{host}: follows {want['value']}, which {server} cannot resolve to an address "
                                    f"({want['kind']})")
            else:
                answers = {pub.normalize(answer) for answer in dig(server, host, "A")}
                if pub.normalize(want["value"]) not in answers:
                    shown = ", ".join(sorted(answers)) or "no answer"
                    problems.append(f"{host}: Cerulean has A {want['value']}, but {server} answers {shown} for "
                                    f"{host} ({want['kind']})")
        except FileNotFoundError:
            print("note: dig is not installed, resolution check skipped")
            return problems
        except (RuntimeError, subprocess.SubprocessError) as error:
            problems.append(f"{host}: could not ask {server} for {host}: {error}")
    return problems


def last_alert():
    try:
        with open(ALERT_STATE) as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return {}


def remember_alert(fingerprint):
    os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
    with open(ALERT_STATE, "w") as handle:
        json.dump({"fingerprint": fingerprint, "alerted_at": time.time()}, handle)


def clear_alert():
    try:
        os.remove(ALERT_STATE)
    except FileNotFoundError:
        pass


def main():
    pub = publisher()
    notes = []
    wanted = pub.expected_records(notify=notes.append)
    base, token = pub.load_cerulean_config()

    zones = {}
    problems = []
    matched = {}
    for (zone, host), want in sorted(wanted.items()):
        if zone not in zones:
            zones[zone] = pub.cerulean(
                "GET", f"{base}/api/service/dns/records?zone={urllib.parse.quote(zone)}", token
            ) or []
        same = [record for record in zones[zone]
                if record["type"] == want["type"] and record["name"].lower() == host]
        if not same:
            problems.append(f"{host}: no {want['type']} record in Cerulean zone {zone} "
                            f"(expected {want['value']}, {want['kind']})")
        elif not any(pub.normalize(record["value"]) == pub.normalize(want["value"]) for record in same):
            addresses = ", ".join(record["value"] for record in same)
            problems.append(f"{host}: Cerulean has {addresses}, but Coolify's configuration needs "
                            f"{want['value']} ({want['kind']})")
        else:
            matched[(zone, host)] = want

    problems.extend(resolution_problems(pub, matched, base))

    for note in notes:
        print(note)

    if not wanted:
        print("ok: no Coolify domains resolve into a Cerulean zone yet")
        return 0
    if not problems:
        if last_alert():
            print("recovered: the drift reported earlier is gone")
            clear_alert()
        print(f"ok: {len(wanted)} Coolify domain(s) publish to Cerulean")
        return 0

    for problem in problems:
        print(f"drift: {problem}")

    fingerprint = hashlib.sha256("\n".join(sorted(problems)).encode()).hexdigest()
    previous = last_alert()
    age_hours = (time.time() - float(previous.get("alerted_at") or 0)) / 3600 if previous else REPEAT_HOURS
    if previous.get("fingerprint") == fingerprint and age_hours < REPEAT_HOURS:
        print(f"alert: the same drift was mailed {age_hours:.1f}h ago; repeating after {REPEAT_HOURS:g}h "
              f"(the check still fails)")
        return 1

    host = os.uname().nodename
    alert(
        f"Coolify DNS drift on {host}: {len(problems)} domain(s) missing from Cerulean",
        "These Coolify domains have no matching Cerulean record:\n\n"
        + "\n".join(f"  - {problem}" for problem in problems)
        + "\n\nThe publisher runs on every deployment; run it by hand to see why:\n"
        + "  python3 /root/coolify-cerulean-dns.py        # dry run\n"
        + "  python3 /root/coolify-cerulean-dns-status.py # what is where\n"
        + "  journalctl -u coolify-cerulean-dns-watch.service -n 50\n",
    )
    remember_alert(fingerprint)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"check failed: {error}", file=sys.stderr)
        sys.exit(2)
