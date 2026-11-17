#!/usr/bin/env python3
"""Receive Mailjet's Parse API webhook and inject the message into Stalwart.

Why this exists
---------------
The estate's public IP is on Comcast, which blocks **inbound** port 25, so MX
delivery to `mail.innotel.us` can never work. Inbound mail is therefore received
by Mailjet (MX `parse.mailjet.com`) and this service puts it back into the local
Stalwart, which owns the real mailbox.

Flow
----
1. Mailjet's Parse API POSTs the parsed message as JSON: the headers, the text
   and html bodies, and every attachment as base64.
2. Mailjet does not offer the original bytes, so the message is rebuilt here:
   the sender's headers are kept, the bodies go back into a multipart structure
   and the attachments are re-attached with their original filenames. This is
   lossy in one deliberate way — `DKIM-Signature`, `ARC-Seal` and
   `ARC-Message-Signature` are dropped, because a signature over the old bytes
   would be invalid over the new ones and would only teach readers to ignore
   DKIM. Everything the mailbox needs to display the message is preserved.
3. Hand that message to Stalwart over SMTP from loopback - the listener path the
   server is already configured to treat as a relay (SPF not re-evaluated, its
   spam filter skipped, Received header added).

Mailjet retries a webhook on any non-2xx, so delivery is recorded by message
before the response is sent; a retry of a message already injected is answered
with 200 and not delivered twice.

Security
--------
Mailjet's Parse API does not sign its requests, so the endpoint is protected the
way Mailjet recommends: HTTP Basic credentials in the parseroute URL. With no
credentials configured the service refuses to start rather than accept mail
from anyone who can reach the port.

Config (`/etc/oasis/mailjet-inbound.env`, all optional except the credentials):
    MAILJET_INBOUND_USER=...          basic-auth user for the parseroute URL
    MAILJET_INBOUND_PASSWORD=...      basic-auth password for the parseroute URL
    LISTEN=127.0.0.1:8100             bind address
    STALWART_SMTP=127.0.0.1:25        where to inject
    LOCAL_DOMAINS=innotel.us,denovocredit.com
    STATE_FILE=/var/lib/mailjet-inbound/delivered.jsonl
"""
import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import sys
import threading
import time
import urllib.parse
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ENV_FILE = Path(os.environ.get("MAILJET_INBOUND_ENV", "/etc/oasis/mailjet-inbound.env"))

DEFAULTS = {
    "LISTEN": "127.0.0.1:8100",
    "STALWART_SMTP": "127.0.0.1:25",
    "LOCAL_DOMAINS": "innotel.us,denovocredit.com",
    "STATE_FILE": "/var/lib/mailjet-inbound/delivered.jsonl",
    "MAILJET_INBOUND_USER": "",
    "MAILJET_INBOUND_PASSWORD": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    for key in cfg:
        cfg[key] = os.environ.get(key, cfg[key])
    return cfg


CFG = load_config()
DOMAINS = {d.strip().lower() for d in CFG["LOCAL_DOMAINS"].split(",") if d.strip()}
STATE = Path(CFG["STATE_FILE"])
_lock = threading.Lock()

#: Headers this service writes itself, so the parsed copy must not be repeated.
_REWRITTEN = {
    "mime-version",
    "content-type",
    "content-transfer-encoding",
    # A part's disposition at the top level describes a part that no longer exists, and a
    # copied `Content-Disposition: attachment` makes a rebuilt message look like an
    # attachment whose body is the message. Measured on Google's 2026-11-04 report.
    "content-disposition",
    "received",
    "x-inbound-gateway",
}

#: Signatures over the original bytes. Rebuilding invalidates them (see the
#: module docstring); carrying them forward would report a forged tamper.
_UNSIGNED = {"dkim-signature", "arc-seal", "arc-message-signature"}


# ── helpers ─────────────────────────────────────────────────────────────────
def log(event: str, **fields) -> None:
    line = " ".join(f"{k}={v!r}" if " " in str(v) else f"{k}={v}" for k, v in fields.items())
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {event} {line}".rstrip(),
          flush=True)


def is_local(address: str) -> bool:
    _, _, domain = address.rpartition("@")
    return domain.strip().lower() in DOMAINS


def header_lines(name: str, value) -> list[str]:
    """One `Name: value` line per stored value, folded onto one line each.

    Mailjet hands most headers back as arrays (one entry per header line) and a
    few as strings; both become lines here, with any embedded newline collapsed
    so a header can never inject a second one.
    """
    values = value if isinstance(value, list) else [value]
    out = []
    for item in values:
        if item is None:
            continue
        text = re.sub(r"\s*[\r\n]+\s*", " ", str(item)).strip()
        if text:
            out.append(f"{name}: {text}")
    return out


def addresses(value) -> list[str]:
    """The addresses in a header value, without display names."""
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").split(",")
    found = []
    for part in parts:
        text = str(part)
        for match in re.findall(r"<([^<>@\s]+@[^<>\s]+)>", text) or ([text] if "@" in text else []):
            address = match.strip().strip("<>").strip()
            if "@" in address and address not in found:
                found.append(address)
    return found


def header_value(part: dict, name: str) -> str:
    """One header's text from a Mailjet `Parts` entry, whatever shape it arrived in.

    Mailjet returns header values as **arrays**, one entry per header line — the same shape
    `header_lines` already handles for the top-level message. Reading them with `str()` here
    is what dropped every attachment: `['attachment; filename="x.zip"]` does not start with
    `attachment`, so a message whose whole content is one `.zip` (a DMARC aggregate report is
    exactly that) was rebuilt as an empty `text/plain` body. Header names are matched
    case-insensitively because their case belongs to the sender, not to us.
    """
    headers = part.get("Headers") or {}
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            if isinstance(value, list):
                value = value[0] if value else ""
            return str(value or "").strip()
    return ""


def attachment_ref(part: dict, index: int) -> str:
    """The payload key holding this part's bytes: its `ContentRef`, else `Attachment<n>`.

    Mailjet names the content `Attachment1`, `Attachment2`, … in the same order it lists the
    parts, and `ContentRef` normally spells that name out. Falling back to the index keeps an
    attachment whose `ContentRef` was left out instead of dropping it silently.
    """
    return str(part.get("ContentRef") or "").strip() or f"Attachment{index + 1}"


def attachment_name(part: dict, index: int) -> str:
    disposition = header_value(part, "Content-Disposition")
    match = re.search(r'filename\*?=(?:"([^"]+)"|([^;\s]+))', disposition, re.IGNORECASE)
    name = (match.group(1) or match.group(2)) if match else ""
    return name or f"attachment-{index}.bin"


def attachment_type(part: dict) -> str:
    ctype = header_value(part, "Content-Type") or "application/octet-stream"
    return ctype.split(";")[0].strip() or "application/octet-stream"


def b64body(text: str) -> str:
    """The body as base64 lines, so no encoding or line-length rule can mangle it."""
    encoded = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
    return "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))


def build_message(payload: dict) -> tuple[str, list[str], bytes]:
    """Turn a Parse API payload into (envelope sender, recipients, raw message).

    Pure, so the shape Mailjet sends is the only thing under test.
    """
    headers = payload.get("Headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    sender = (payload.get("Sender") or "").strip().strip("<>")
    if not sender:
        sender = (addresses(payload.get("From") or headers.get("From")) or ["unknown@localhost"])[0]

    # The route is bound to one address, but a message can be addressed to more
    # (To/Cc on a local domain). Those are the recipients Stalwart should see.
    candidates = [payload.get("Recipient") or ""]
    candidates += addresses(headers.get("To"))
    candidates += addresses(headers.get("Cc"))
    recipients = [a for a in dict.fromkeys(a for a in candidates if a) if is_local(a)]

    lines: list[str] = []
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _REWRITTEN or lowered in _UNSIGNED:
            continue
        lines.extend(header_lines(name, value))

    # The headers the message is rebuilt around. From/To/Subject survive above
    # only when Mailjet returned them inside Headers; the single-string copies
    # are used when it did not.
    for name, value in (("From", payload.get("From")), ("To", payload.get("Recipient")),
                        ("Subject", payload.get("Subject"))):
        if not any(line.lower().startswith(f"{name.lower()}:") for line in lines) and value:
            lines.extend(header_lines(name, value))

    lines.append(f"Received: from parse.mailjet.com by {os.uname().nodename} "
                 f"with HTTP for <{', '.join(recipients)}>; {formatdate(localtime=False)}")
    lines.append("X-Inbound-Gateway: mailjet")

    text = payload.get("Text-part") or ""
    html = payload.get("Html-part") or ""
    parts = payload.get("Parts") if isinstance(payload.get("Parts"), list) else []
    attachments = [(part, attachment_name(part, index), attachment_type(part),
                    attachment_ref(part, index))
                   for index, part in enumerate(parts)
                   if header_value(part, "Content-Disposition").lower().startswith("attachment")]
    # AttachmentN keys are the same content the Parts entry points at.
    attachments = [item for item in attachments if isinstance(payload.get(item[3]), str)]

    top_cte = ""
    boundary = "mailjet-inbound-" + hashlib.sha256(
        (payload.get("Message-ID") or str(payload)).encode("utf-8", "replace")).hexdigest()[:16]

    if attachments and html:
        main_type = f'multipart/mixed; boundary="{boundary}"'
        body_parts = [f"--{boundary}", "Content-Type: multipart/alternative; "
                      f'boundary="{boundary}-alt"', ""]
        body_parts += _alternative(text, html, f"{boundary}-alt")
        body_parts.append(f"--{boundary}")
        for part, name, ctype, ref in attachments:
            body_parts += [f"Content-Type: {ctype}; name=\"{name}\"",
                           'Content-Disposition: attachment; '
                           f'filename="{name}"',
                           "Content-Transfer-Encoding: base64", ""]
            body_parts.append("\n".join(_wrap(str(payload[ref]))))
            body_parts.append(f"--{boundary}")
        body_parts.append(f"--{boundary}--")
    elif attachments:
        main_type = f'multipart/mixed; boundary="{boundary}"'
        body_parts = []
        if text:
            body_parts += [f"--{boundary}", "Content-Type: text/plain; charset=utf-8",
                           "Content-Transfer-Encoding: base64", "", b64body(text)]
        for part, name, ctype, ref in attachments:
            body_parts += [f"--{boundary}",
                           f"Content-Type: {ctype}; name=\"{name}\"",
                           f'Content-Disposition: attachment; filename="{name}"',
                           "Content-Transfer-Encoding: base64", "",
                           "\n".join(_wrap(str(payload[ref])))]
        body_parts.append(f"--{boundary}--")
    elif text and html:
        main_type = f'multipart/alternative; boundary="{boundary}"'
        body_parts = _alternative(text, html, boundary)
    elif html:
        main_type = "text/html; charset=utf-8"
        top_cte = "base64"
        body_parts = [b64body(html)]
    else:
        main_type = "text/plain; charset=utf-8"
        top_cte = "base64"
        body_parts = [b64body(text)]

    lines.append("MIME-Version: 1.0")
    lines.append(f"Content-Type: {main_type}")
    if top_cte:
        # A single-part message carries its encoding as a top-level header; only
        # the parts of a multipart message carry their own.
        lines.append(f"Content-Transfer-Encoding: {top_cte}")
    message = "\r\n".join(lines) + "\r\n\r\n" + "\r\n".join(body_parts) + "\r\n"
    return sender, recipients, message.encode("utf-8", "replace")


def _wrap(encoded: str) -> list[str]:
    """Mailjet's base64 attachment, re-wrapped to 76 columns."""
    compact = re.sub(r"\s+", "", encoded)
    return [compact[i:i + 76] for i in range(0, len(compact), 76)] or [""]


def _alternative(text: str, html: str, boundary: str) -> list[str]:
    out = []
    if text:
        out += [f"--{boundary}", "Content-Type: text/plain; charset=utf-8",
                "Content-Transfer-Encoding: base64", "", b64body(text)]
    if html:
        out += [f"--{boundary}", "Content-Type: text/html; charset=utf-8",
                "Content-Transfer-Encoding: base64", "", b64body(html)]
    out.append(f"--{boundary}--")
    return out


def message_key(payload: dict) -> str:
    """The identity a retry repeats; the Message-ID when the sender set one."""
    headers = payload.get("Headers") or {}
    message_id = headers.get("Message-ID") or headers.get("Message-Id")
    if isinstance(message_id, list):
        message_id = message_id[0] if message_id else None
    if message_id:
        return str(message_id).strip()
    digest = hashlib.sha256(
        "\x00".join(str(payload.get(k) or "") for k in ("From", "Recipient", "Subject", "Date"))
        .encode("utf-8", "replace")).hexdigest()
    return f"sha256:{digest[:32]}"


def already_delivered(key: str) -> bool:
    """True when this message was injected before (webhook retry)."""
    if not STATE.is_file():
        return False
    for line in STATE.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("key") == key:
                return True
        except ValueError:
            continue
    return False


def remember(key: str, recipients: list[str], sender: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "recipients": recipients, "sender": sender,
                             "at": int(time.time())}) + "\n")


def inject(sender: str, recipients: list[str], message: bytes) -> list[str]:
    """Hand the message to Stalwart; return the recipients that were accepted."""
    host, _, port = CFG["STALWART_SMTP"].rpartition(":")
    accepted = []
    with smtplib.SMTP(host or "127.0.0.1", int(port or "25"), timeout=60) as smtp:
        smtp.ehlo()
        code, _ = smtp.mail(sender)
        if code >= 400:
            raise RuntimeError(f"MAIL FROM refused: {code}")
        for rcpt in recipients:
            code, _ = smtp.rcpt(rcpt)
            if code < 400:
                accepted.append(rcpt)
            else:
                log("recipient-rejected", rcpt=rcpt, code=code)
        if accepted:
            code, detail = smtp.data(message)
            if code >= 400:
                raise RuntimeError(f"DATA refused: {code} {detail!r}")
    return accepted


def handle(payload: dict) -> None:
    sender, recipients, message = build_message(payload)
    key = message_key(payload)
    if not recipients:
        log("no-local-recipient", key=key, recipient=payload.get("Recipient"))
        return
    if already_delivered(key):
        log("duplicate-skipped", key=key)
        return
    accepted = inject(sender or "unknown@localhost", recipients, message)
    if not accepted:
        # Stalwart refused every recipient (no such mailbox). Raising here would
        # have Mailjet retry the same message forever, so the message is recorded
        # as handled and reported loudly instead - the sender's server is the
        # place that can bounce it, and it already accepted this message.
        remember(key, [], sender)
        log("dropped-no-mailbox", key=key, sender=sender, recipients=",".join(recipients))
        return
    remember(key, accepted, sender)
    log("injected", key=key, sender=sender, recipients=",".join(accepted), bytes=len(message))


def authorized(header: str | None) -> bool:
    """HTTP Basic, compared in constant time. An unset password refuses everyone."""
    user, password = CFG["MAILJET_INBOUND_USER"], CFG["MAILJET_INBOUND_PASSWORD"]
    if not (user and password):
        return False
    scheme, _, encoded = (header or "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        supplied = base64.b64decode(encoded.strip()).decode("utf-8", "replace")
    except ValueError:
        return False
    name, _, secret = supplied.partition(":")
    return (hmac.compare_digest(name, user) & hmac.compare_digest(secret, password)) == 1


def parse_body(body: bytes, content_type: str) -> dict:
    """JSON, or the form encoding older Parse API documentation still shows."""
    if "application/json" in (content_type or "").lower():
        return json.loads(body or b"{}")
    try:
        return json.loads(body or b"{}")
    except ValueError:
        pass
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    payload = {k: v[0] for k, v in form.items()}
    for key in ("Headers", "Parts"):
        if isinstance(payload.get(key), str):
            try:
                payload[key] = json.loads(payload[key])
            except ValueError:
                payload.pop(key, None)
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", ""):
            self._reply(200, {"status": "ok", "domains": sorted(DOMAINS),
                              "auth": bool(CFG["MAILJET_INBOUND_USER"])})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/mailjet", "/hooks/mailjet"):
            self._reply(404, {"error": "not found"})
            return
        if not authorized(self.headers.get("Authorization")):
            log("unauthorized", ip=self.client_address[0])
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="mailjet-inbound"')
            length = len(b"{}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(b"{}")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            payload = parse_body(body, self.headers.get("Content-Type") or "")
        except ValueError:
            self._reply(400, {"error": "invalid payload"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "invalid payload"})
            return
        with _lock:
            try:
                handle(payload)
            except Exception as exc:  # noqa: BLE001 - surface the reason to Mailjet's retry
                log("inject-failed", error=f"{type(exc).__name__}: {exc}")
                self._reply(500, {"error": str(exc)})
                return
        self._reply(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:  # silence the default stderr access log
        return


def main() -> int:
    if not (CFG["MAILJET_INBOUND_USER"] and CFG["MAILJET_INBOUND_PASSWORD"]):
        print("MAILJET_INBOUND_USER/MAILJET_INBOUND_PASSWORD are not set: the parseroute "
              "URL carries the credentials, and an unauthenticated receiver accepts mail "
              "from anyone", file=sys.stderr)
        return 2
    host, _, port = CFG["LISTEN"].rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    log("listening", addr=f"{host}:{port}", smtp=CFG["STALWART_SMTP"],
        domains=",".join(sorted(DOMAINS)), signature="basic")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
