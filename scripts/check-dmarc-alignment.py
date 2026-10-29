#!/usr/bin/env python3
"""Read the DMARC aggregate reports and say which sending streams do not align.

WHY THIS EXISTS
---------------
`check-mail-auth.py` proves the SPF, DKIM and DMARC *records* are in the zone the
public reads. That is necessary and not sufficient: a record can be present,
well-formed, and still describe a sender that does not align with the domain it
sends as. The difference only shows up in the reports receivers send back to the
`rua` address, and nothing was reading them — so the question "can we tighten
`_dmarc.atheniq.courses` to `p=reject`?" had no answer in the estate, only an
opinion.

That question is why this check exists. `p=reject` fails **silently**: a message
that does not align is dropped by the receiver, and the symptom is mail that
never arrives — indistinguishable, from the sending side, from a slow queue. The
reports are the only evidence that says which streams would survive the change,
and the decision to tighten (`docs/Deployment.md` §9.11, atheniq-work) is gated on
them showing `spf=pass` or `dkim=pass` for every stream that actually carries mail.

WHAT IT READS
-------------
DMARC aggregate reports (the XML receivers mail to `rua=`) for the estate's
sending domains. Two sources, because the check has to work before and after the
reports are archived:

* the mailbox itself, over **IMAP/SSL** (`--host`, defaults to the estate's mail
  server) — credentials from `DMARC_USER`/`DMARC_PASSWORD` in the environment,
  with no default. A check must never carry a mailbox password; see the same rule
  on `check-mail-relay.py`.
* `--reports-dir`, a directory of saved reports (`.xml`, `.zip`, `.gz`), for a
  hand run against last month's archive or a test.

`policy_evaluated` is already the *aligned* verdict, so `dkim=fail spf=fail` on a
record is precisely a message `p=reject` would drop. The check does not try to
guess whether such a stream is a spoof (dropping it is the point) or a real sender
missing from SPF (dropping it is an outage) — it names the stream, the org, the
source address, the header From and the volume, which is what a person needs to
tell the two apart.

THE RULES
---------
1. A stream — an org's view of one `header_from` at one source address — whose
   messages fail **both** authentication results, at or above `--threshold`
   messages, is a **failure**: those are the messages `p=reject` would drop.
2. A stream that aligns by one of the two (typically `dkim=pass spf=fail`) is a
   **warning**: mail survives, but the failing half is the shape of a missing SPF
   include and is worth seeing before the policy tightens.
3. A report's own `policy_published` is read back: a `p=none` domain is a warning,
   because nothing is enforced and the reports are the only evidence there is.
4. No reports at all in the window is **not the same failure** as a broken mailbox, and
   conflating them is what made this check permanently red: for the first days after a
   `rua` is published there is nothing to read yet, because a receiver only reports on
   mail it has actually seen. Silence inside `--grace-days` of `--wired` is a **note**
   (exit 0: nothing has been sent yet, the first report is expected); silence *after* it
   is a **failure** (exit 1), because then the reporting path itself is broken — the
   `rua` is wrong, mail to it does not reach the mailbox, or a filter files the reports
   somewhere the check does not read. A mailbox that cannot be read is still exit 2,
   and so is a `--reports-dir` with nothing in it: those are "could not answer".

Usage:
    DMARC_USER=… DMARC_PASSWORD=… ./scripts/check-dmarc-alignment.py
    ./scripts/check-dmarc-alignment.py --reports-dir /var/tmp/dmarc --prom /path/to/textfile
    ./scripts/check-dmarc-alignment.py --domains atheniq.courses --threshold 5 --days 30
    ./scripts/check-dmarc-alignment.py --wired 2026-11-01 --grace-days 5   # a new rua

Exit codes: 0 = every stream aligns (or the rua is too new to have been reported on),
1 = at least one stream does not align, or the rua is old enough that silence means a
broken reporting path, 2 = the reports could not be read (no credential, unreadable
mailbox, a report that does not parse, an empty `--reports-dir`).
"""
from __future__ import annotations

import argparse
import email
import gzip
import imaplib
import io
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, write_prom  # noqa: E402

CHECK = "dmarc_alignment"

#: The mail server the reports land on — `mail` in the container address table
#: (`check-mail-relay.py` reads the same box). IMAP/SSL is 993, which is open here;
#: 25 is not (docs/Deployment.md §9.11, measured).
MAIL_HOST = "192.168.1.15"
MAIL_PORT = 993

#: The domains whose policy this check can speak about. Each must publish a DMARC
#: record with a `rua`; a domain absent from the mailbox simply contributes nothing
#: and is not silently reported as aligned.
DOMAINS = ("atheniq.courses", "innotel.us")

#: How far back a run reads. Receivers send daily, so a week is seven chances to
#: hear from each of them; `--days` exists for a hand run after a quiet period.
WINDOW_DAYS = 7

#: Messages a stream may lose before that is a finding. One is deliberate: a single
#: non-aligning message from a source is still a source the tightened policy drops,
#: and the first run against a real mailbox should show everything, not a filtered
#: view that makes the decision look safer than it is.
THRESHOLD = 1

#: The day the report mailbox and the `rua` records went live (2026-10-29 —
#: `postmaster@atheniq.courses` with an app password, `dmarc@innotel.us` aliased to it).
#: It only bounds the "nothing has been sent yet" case; a report that exists is read
#: whatever this says.
RUA_WIRED = date(2026, 10, 29)

#: Days of silence after `RUA_WIRED` that mean "no mail has been reported on yet" rather
#: than "the reporting path is broken". Receivers send daily, so three days is three
#: missed chances; past that, silence is the failure and not a wait.
GRACE_DAYS = 3

#: A report is XML; a receiver may send it raw, gzipped, or zipped (usually zipped).
_REPORT_SUFFIXES = (".xml", ".zip", ".gz", ".xml.gz")
_GZIP_MAGIC = b"\x1f\x8b"
_XML_START = re.compile(rb"<\?xml|<!DOCTYPE|<feedback", re.IGNORECASE)


class ReportsUnreadable(RuntimeError):
    """The reports could not be read — no mailbox, no reports, or none parsed."""


@dataclass
class Stream:
    """One org's view of one sending stream, summed over every report in the window."""

    org: str
    header_from: str
    source_ip: str
    messages: int = 0
    spf_pass: int = 0
    dkim_pass: int = 0
    dispositions: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.org, self.header_from, self.source_ip)

    @property
    def both_fail(self) -> int:
        """Messages that would be dropped under `p=reject`."""
        return self.messages - max(self.spf_pass, self.dkim_pass)


def _text(node: ElementTree.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def parse_report(payload: bytes) -> tuple[dict[str, str], list[dict[str, str]]]:
    """`(published, [record, …])` from one aggregate report's XML.

    `published` carries the report's own view of the policy — `org`, `domain`, `p`
    and `pct` — and each record carries `source_ip`, `count`, `disposition`, `dkim`,
    `spf` and `header_from`.

    `org_name` is read here rather than per record because one report is one org's
    view of the domain: two receivers legitimately report the same source address,
    and the org is the axis that keeps them apart.
    """
    root = ElementTree.fromstring(payload)
    if root.tag != "feedback":
        raise ValueError(f"root element is <{root.tag}>, not <feedback>")
    published = root.find("policy_published")
    policy = {
        "org": _text(root, "report_metadata/org_name") or "unknown",
        "domain": _text(published, "domain"),
        "p": _text(published, "p", "none"),
        "pct": _text(published, "pct", "100"),
    }
    records: list[dict[str, str]] = []
    for record in root.findall("record"):
        # `policy_evaluated` is a child of `row`, not of `record` — reading it at the
        # wrong depth finds nothing and every record then defaults to fail, which
        # would report a healthy domain as entirely unaligned.
        evaluated = record.find("row/policy_evaluated")
        records.append(
            {
                "source_ip": _text(record, "row/source_ip") or "unknown",
                "count": _text(record, "row/count", "1") or "1",
                "disposition": _text(evaluated, "disposition", "none"),
                "dkim": _text(evaluated, "dkim", "fail").lower(),
                "spf": _text(evaluated, "spf", "fail").lower(),
                "header_from": _text(record, "identifiers/header_from") or policy["domain"],
            }
        )
    return policy, records


def expand(name: str, payload: bytes) -> list[tuple[str, bytes]]:
    """Every XML report inside one attachment, whatever the receiver wrapped it in.

    A `.zip` usually holds one report but may hold several (a weekly digest); a
    `.gz` holds one. Bytes that are already XML pass through, which is what makes a
    raw attachment and a `.xml` on disk the same input.
    """
    if payload[:2] == _GZIP_MAGIC:
        try:
            return [(f"{name}:gz", gzip.decompress(payload))]
        except OSError:
            return []
    if payload[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                return [
                    (f"{name}:{member}", archive.read(member))
                    for member in archive.namelist()
                    if not member.endswith("/")
                ]
        except (zipfile.BadZipFile, OSError):
            return []
    if _XML_START.search(payload[:512]):
        return [(name, payload)]
    return []


def payloads_from_message(message: email.message.Message) -> list[tuple[str, bytes]]:
    """Every attachment in one mail, already unwrapped from its container."""
    out: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = part.get_filename() or "report"
        body = part.get_payload(decode=True)
        if not body:
            continue
        if not (name.lower().endswith(_REPORT_SUFFIXES) or _XML_START.search(body[:512])):
            continue
        out.extend(expand(name, body))
    return out


def collect(streams: dict[tuple[str, str, str], Stream], published, records) -> None:
    """Fold one report's records into `streams`. Pure: the grouping is the thing under test."""
    for record in records:
        try:
            count = int(record["count"])
        except ValueError:
            continue
        if count <= 0:
            continue
        key = (record["org"], record["header_from"], record["source_ip"])
        stream = streams.get(key)
        if stream is None:
            stream = Stream(org=record["org"], header_from=record["header_from"], source_ip=record["source_ip"])
            streams[key] = stream
        stream.messages += count
        if record["spf"] == "pass":
            stream.spf_pass += count
        if record["dkim"] == "pass":
            stream.dkim_pass += count
        stream.dispositions.add(record["disposition"])


def evaluate(streams: list[Stream], policies: dict[str, dict[str, str]], threshold: int) -> Audit:
    """Read the folded streams back as findings.

    `streams` is every stream seen in the window; `policies` is `domain -> policy`
    as the reports themselves published it. Pure, so what counts as aligned — and
    therefore whether the policy may tighten — is the thing under test.
    """
    out = Audit()
    for domain, policy in sorted(policies.items()):
        if policy.get("p", "none") == "none":
            out.findings.append(
                Finding(
                    "policy-none",
                    "warn",
                    f"{domain} publishes p=none — nothing is enforced, so these reports are the only evidence there is",
                )
            )
        else:
            out.findings.append(
                Finding(
                    "policy-read",
                    "note",
                    f"{domain} publishes p={policy['p']} pct={policy.get('pct', '100')}",
                )
            )

    if not streams:
        out.findings.append(Finding("no-streams", "fail", "no DMARC report in the window carried a record"))
        return out

    for stream in sorted(streams, key=lambda s: (s.header_from, s.source_ip)):
        where = f"{stream.header_from} from {stream.source_ip} seen by {stream.org}"
        volume = f"{stream.messages} message(s)"
        if stream.both_fail >= threshold:
            out.findings.append(
                Finding(
                    "stream-misaligned",
                    "fail",
                    f"{where}: {stream.both_fail} of {volume} failed both SPF and DKIM "
                    f"(disposition {'/'.join(sorted(stream.dispositions)) or 'none'}) — "
                    f"p=reject would drop these",
                )
            )
        elif stream.both_fail:
            out.findings.append(
                Finding(
                    "stream-misaligned-small",
                    "note",
                    f"{where}: {stream.both_fail} of {volume} failed both, below the {threshold}-message threshold",
                )
            )
        elif stream.spf_pass < stream.messages or stream.dkim_pass < stream.messages:
            half = "SPF" if stream.spf_pass < stream.messages else "DKIM"
            out.findings.append(
                Finding(
                    "stream-half-aligned",
                    "warn",
                    f"{where}: {volume} aligned, but {half} did not pass for every one — "
                    f"the shape of a missing include, and the half a receiver still weighs",
                )
            )
        else:
            out.findings.append(Finding("stream-aligned", "note", f"{where}: {volume}, SPF and DKIM both pass"))
    return out


def no_reports_finding(
    wired: date, grace: int, window: int, today: date | None = None
) -> tuple[Finding, bool]:
    """What silence means: waiting for the first report, or a reporting path that is broken.

    Returns the finding and whether it is a failure. The distinction is the whole point:
    a `rua` published yesterday has nothing to report on yet, and calling that "could not
    answer" keeps the estate red for a reason nobody can act on — while the same silence
    a week later is exactly the failure worth paging for.
    """
    today = today or date.today()
    age = (today - wired).days
    if age <= grace:
        return (
            Finding(
                "awaiting-first-report",
                "note",
                f"no DMARC report in the last {window} day(s): the rua has only been "
                f"published since {wired:%Y-%m-%d} ({age} day(s) ago) and a receiver reports "
                f"only on mail it has seen, so the first report is expected within "
                f"{grace} day(s). There is nothing to answer with yet",
            ),
            False,
        )
    return (
        Finding(
            "no-reports",
            "fail",
            f"no DMARC report in the last {window} day(s) although the rua has been published "
            f"since {wired:%Y-%m-%d} ({age} day(s)) — the reporting path is broken, not quiet: "
            f"check the rua on _dmarc.<domain>, that mail to it reaches the mailbox, and that "
            f"a filter is not filing reports outside INBOX",
        ),
        True,
    )


def alignment_series(streams: list[Stream], reports: int | None = None) -> str:
    """The numbers behind the verdict, so the panel shows volume and not only a colour.

    `reports` is how many aggregate reports were read, which is what separates "0 messages
    would be dropped" from "nothing has been reported on yet" on a panel that otherwise
    shows the same two zeros.
    """
    total = sum(s.messages for s in streams)
    dropped = sum(s.both_fail for s in streams)
    lines = [
            f"# HELP {METRIC}_dmarc_messages Messages named in the window's reports",
            f"# TYPE {METRIC}_dmarc_messages gauge",
            f'{METRIC}_dmarc_messages{{check="{CHECK}"}} {total}',
            f"# HELP {METRIC}_dmarc_messages_both_fail Messages failing both SPF and DKIM alignment",
            f"# TYPE {METRIC}_dmarc_messages_both_fail gauge",
            f'{METRIC}_dmarc_messages_both_fail{{check="{CHECK}"}} {dropped}',
            f"# HELP {METRIC}_dmarc_streams Sending streams the reports named",
            f"# TYPE {METRIC}_dmarc_streams gauge",
            f'{METRIC}_dmarc_streams{{check="{CHECK}"}} {len(streams)}',
        ]
    if reports is not None:
        lines += [
            f"# HELP {METRIC}_dmarc_reports Aggregate reports read in the window",
            f"# TYPE {METRIC}_dmarc_reports gauge",
            f'{METRIC}_dmarc_reports{{check="{CHECK}"}} {reports}',
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Reading the reports
# --------------------------------------------------------------------------------------


def _since(days: int, today: date | None = None) -> str:
    """IMAP's `SINCE` wants `%d-%b-%Y`; the earliest report we still care about."""
    today = today or date.today()
    return (today - timedelta(days=days)).strftime("%d-%b-%Y")


def fetch_from_mailbox(
    host: str, port: int, user: str, password: str, days: int, domains, connector=imaplib.IMAP4_SSL
) -> list[tuple[str, bytes]]:
    """Every report attachment in `days` of mail, read without marking anything seen."""
    connection = connector(host, port)
    try:
        connection.login(user, password)
        # Read-only: a check must not consume the reports an operator may still want.
        connection.select("INBOX", readonly=True)
        status, data = connection.search(None, f"(SINCE {_since(days)})")
        if status != "OK":
            raise ReportsUnreadable(f"IMAP search failed: {status}")
        out: list[tuple[str, bytes]] = []
        for number in (data[0] or b"").split():
            status, fetched = connection.fetch(number, "(RFC822)")
            if status != "OK":
                continue
            for part in fetched:
                if not isinstance(part, tuple) or len(part) < 2:
                    continue
                message = email.message_from_bytes(part[1])
                out.extend(payloads_from_message(message))
        return out
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001 — a failed logout must not hide the finding
            pass


def read_directory(path: Path, days: int, today: date | None = None) -> list[tuple[str, bytes]]:
    """Saved reports from a directory, filtered to the window by modification time."""
    cutoff = (today or date.today()) - timedelta(days=days)
    out: list[tuple[str, bytes]] = []
    for entry in sorted(path.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.lower().endswith(_REPORT_SUFFIXES) and entry.suffix.lower() not in _REPORT_SUFFIXES:
            continue
        if date.fromtimestamp(entry.stat().st_mtime) < cutoff:
            continue
        out.extend(expand(entry.name, entry.read_bytes()))
    return out


def fold(payloads: list[tuple[str, bytes]], domains) -> tuple[dict[tuple[str, str, str], Stream], dict[str, dict[str, str]], Audit]:
    """Parse every payload and fold it into streams. Returns the skipped attachments too."""
    streams: dict[tuple[str, str, str], Stream] = {}
    policies: dict[str, dict[str, str]] = {}
    skipped = Audit()
    wanted = set(domains)
    parsed = 0
    for name, body in payloads:
        try:
            policy, records = parse_report(body)
        except (ElementTree.ParseError, ValueError) as error:
            skipped.findings.append(Finding("report-skipped", "warn", f"{name} is not a usable report: {error}"))
            continue
        domain = policy.get("domain") or ""
        if wanted and domain not in wanted:
            # Someone else's report in the same mailbox (a customer's domain, a
            # forwarded copy) is not this estate's business and not a finding.
            continue
        parsed += 1
        policies[domain] = policy
        for record in records:
            record["org"] = policy["org"]
        collect(streams, policy, records)
    if parsed == 0:
        raise ReportsUnreadable(
            "no DMARC aggregate report could be parsed"
            + (f" ({len(skipped.findings)} attachment(s) skipped)" if skipped.findings else "")
        )
    return streams, policies, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=MAIL_HOST, help=f"IMAP server (default {MAIL_HOST})")
    parser.add_argument("--port", type=int, default=MAIL_PORT, help=f"IMAP/SSL port (default {MAIL_PORT})")
    parser.add_argument("--reports-dir", type=Path, help="read saved reports from here instead of the mailbox")
    parser.add_argument("--days", type=int, default=WINDOW_DAYS, help=f"window in days (default {WINDOW_DAYS})")
    parser.add_argument("--threshold", type=int, default=THRESHOLD, help=f"messages lost before it is a failure (default {THRESHOLD})")
    parser.add_argument("--domains", help="comma-separated domains to speak about (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument(
        "--wired", type=date.fromisoformat, default=RUA_WIRED,
        help=f"the day the rua was published, YYYY-MM-DD (default {RUA_WIRED:%Y-%m-%d})",
    )
    parser.add_argument(
        "--grace-days", type=int, default=GRACE_DAYS,
        help=f"days of silence after --wired that are waiting, not failing (default {GRACE_DAYS})",
    )
    args = parser.parse_args(argv)

    domains = tuple(d.strip() for d in args.domains.split(",") if d.strip()) if args.domains else DOMAINS
    streams: list[Stream] = []
    reports = 0

    def emit(result: Audit | None, ran: bool) -> None:
        if not args.prom:
            return
        try:
            write_prom(args.prom, CHECK, result, ran, extra=alignment_series(streams, reports))
        except OSError as error:
            print(f"check-dmarc-alignment: could not write {args.prom}: {error}", file=sys.stderr)

    try:
        if args.reports_dir:
            if not args.reports_dir.is_dir():
                raise ReportsUnreadable(f"{args.reports_dir} is not a directory")
            payloads = read_directory(args.reports_dir, args.days)
        else:
            user = os.environ.get("DMARC_USER")
            password = os.environ.get("DMARC_PASSWORD")
            if not user or not password:
                raise ReportsUnreadable(
                    "DMARC_USER/DMARC_PASSWORD are not set — the check carries no mailbox credential of its own"
                )
            payloads = fetch_from_mailbox(args.host, args.port, user, password, args.days, domains)
        if not payloads:
            if args.reports_dir:
                # A directory someone pointed at is a question about an archive: nothing in
                # it means the archive is empty, which is not a state the rua explains.
                raise ReportsUnreadable(f"no DMARC report in the last {args.days} day(s) — nothing to answer with")
            finding, broken = no_reports_finding(args.wired, args.grace_days, args.days)
            result = Audit(findings=[finding])
            emit(result, ran=True)
            print(f"{finding.level.upper():4} {finding.message}")
            print(
                f"\ncheck-dmarc-alignment: {1 if broken else 0} failure(s), 0 stream(s), "
                f"0 report(s) read — "
                + ("the reporting path is broken" if broken else "waiting for the first report")
            )
            return 1 if broken else 0
        folded, policies, skipped = fold(payloads, domains)
    except ReportsUnreadable as error:
        print(f"check-dmarc-alignment: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2
    except (OSError, imaplib.IMAP4.error) as error:
        print(f"check-dmarc-alignment: could not read the reports: {error}", file=sys.stderr)
        emit(None, ran=False)
        return 2

    streams = list(folded.values())
    reports = len(payloads)
    result = evaluate(streams, policies, args.threshold)
    result.findings.extend(skipped.findings)
    emit(result, ran=True)
    for finding in result.findings:
        print(f"{finding.level.upper():4} {finding.message}")
    total = sum(s.messages for s in streams)
    dropped = sum(s.both_fail for s in streams)
    print(
        f"\ncheck-dmarc-alignment: {len(result.failures)} failure(s), {len(streams)} stream(s), "
        f"{dropped}/{total} message(s) would not survive p=reject"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
