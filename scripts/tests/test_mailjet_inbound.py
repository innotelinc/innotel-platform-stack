#!/usr/bin/env python3
"""What Mailjet actually sends, and what the rebuilt message must keep.

WHY THIS EXISTS
---------------
The Parse API hands header values back as **arrays** (one entry per header line) — the
service's own `header_lines` already documents that for the top-level message. A DMARC
aggregate report is a message whose entire content is one `.zip` attachment, so its
headers are exactly `Content-Type: ['application/zip']` and
`Content-Disposition: ['attachment; filename="…zip"']`.

Read those arrays with `str()` and the value becomes `"['attachment; filename=…']"`,
which does not start with `"attachment"`: the part is dropped, no body is left, and the
rebuilt message is an empty `text/plain` that still carries the original
`Content-Disposition: attachment` header. That is what the mail server holds for the
Google report of 2026-11-04 — 2116 bytes stored, **0 bytes of body** — and no DMARC check
can ever read a report that arrives like that.

The module is loaded from the canonical copy this repository carries
(`hosts/i1/mail/mailjet-inbound.py`), not from a paraphrase: the running service is that
file, and a test against a rewritten copy would pass while the mail path stayed broken.
"""
from __future__ import annotations

import base64
import email
import importlib.util
import sys
import unittest
import zipfile
import io
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "hosts" / "i1" / "mail" / "mailjet-inbound.py"
NAME = "google.com!innotel.us!1793750400!1793836799.zip"
SUBJECT = "Report domain: innotel.us Submitter: google.com"


def load_service():
    """Import the service as a module, the way its unit runs it (`python3 <file>`)."""
    spec = importlib.util.spec_from_file_location("mailjet_inbound", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # dataclasses and friends resolve via sys.modules
    spec.loader.exec_module(module)
    return module


def report_zip(payload: bytes = b"PK\x03\x04probe-report-bytes") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.xml", "<feedback>probe</feedback>")
    return buffer.getvalue()


def report_payload(raw: bytes, *, content_ref: str | None = "Attachment1") -> dict:
    """A DMARC report as Mailjet parses it: no text part, one attachment, array headers."""
    part = {
        "Headers": {
            "Content-Type": ["application/zip"],
            "Content-Disposition": [f'attachment; filename="{NAME}"'],
        },
    }
    if content_ref:
        part["ContentRef"] = content_ref
    return {
        "From": "Google <noreply-dmarc-support@google.com>",
        "Recipient": "dmarc@innotel.us",
        "Subject": SUBJECT,
        "Headers": {
            "From": ["Google <noreply-dmarc-support@google.com>"],
            "To": ["dmarc@innotel.us"],
            "Subject": [SUBJECT],
            "Message-ID": ["<12489092503316464144@mail.gmail.com>"],
            "Content-Type": ["application/zip"],
            "Content-Disposition": [f'attachment; filename="{NAME}"'],
        },
        "Parts": [part],
        "Attachment1": base64.b64encode(raw).decode(),
    }


class AttachmentsSurviveRebuilding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = load_service()

    def test_an_attachment_only_report_keeps_its_zip(self):
        """The 2026-11-04 failure: the report arrived, and its payload did not."""
        raw = report_zip()
        sender, recipients, message = self.service.build_message(report_payload(raw))
        built = email.message_from_bytes(message)

        self.assertEqual(sender, "noreply-dmarc-support@google.com", "the envelope sender is the address, not the display name")
        self.assertEqual(recipients, ["dmarc@innotel.us"])
        self.assertEqual(built.get_content_type(), "multipart/mixed")

        attached = [p for p in built.walk() if p.get_filename()]
        self.assertEqual(len(attached), 1, "the attachment is the whole message — losing it empties the mail")
        self.assertEqual(attached[0].get_filename(), NAME)
        self.assertEqual(attached[0].get_content_type(), "application/zip")
        self.assertEqual(attached[0].get_payload(decode=True), raw, "the bytes must survive byte for byte")

    def test_the_rebuilt_message_is_not_itself_an_attachment(self):
        """The original top-level `Content-Disposition: attachment` describes a part, not the mail."""
        built = email.message_from_bytes(self.service.build_message(report_payload(report_zip()))[2])
        self.assertIsNone(built.get("Content-Disposition"),
                          "a copied top-level Content-Disposition makes an empty mail look like an attachment")

    def test_an_attachment_without_a_content_ref_still_attaches(self):
        """Mailjet has sent `Attachment<n>` without a `ContentRef`; the index is the same pointer."""
        raw = report_zip()
        payload = report_payload(raw, content_ref=None)
        built = email.message_from_bytes(self.service.build_message(payload)[2])
        attached = [p for p in built.walk() if p.get_filename()]
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0].get_payload(decode=True), raw)

    def test_a_text_message_is_unchanged(self):
        """The ordinary case must not regress: one base64 text part and nothing else."""
        payload = {
            "From": "someone@example.com",
            "Recipient": "dmarc@innotel.us",
            "Subject": "hello",
            "Headers": {"From": ["someone@example.com"], "To": ["dmarc@innotel.us"],
                        "Subject": ["hello"], "Message-ID": ["<1@example.com>"]},
            "Text-part": "hello there",
        }
        built = email.message_from_bytes(self.service.build_message(payload)[2])
        self.assertEqual(built.get_content_type(), "text/plain")
        self.assertEqual(built.get_payload(decode=True), b"hello there")


if __name__ == "__main__":
    unittest.main()
