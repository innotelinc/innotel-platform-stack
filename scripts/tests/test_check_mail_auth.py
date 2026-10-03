#!/usr/bin/env python3
"""Unit tests for check-mail-auth.py — the records the public zone must serve.

The failure this check exists to catch is silent by construction: a missing SPF, DKIM
or DMARC record does not stop mail leaving and does not make anything look unhealthy —
it only makes the mail fail verification at the far end. The estate is especially
exposed because Stalwart's automatic DNS publishing is refused by the public zone, so
the records are a hand-maintained set. What is worth locking here is that "no record"
is a **failure** (never a quiet pass), that a split RSA key reads back as one record
(dig prints it as two quoted chunks on one line — measured 2026-10-03), and that a
server that cannot be read is exit 2 rather than a domain that looks unauthenticated.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-mail-auth.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_mail_auth", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mail = _load()

#: A dig answer for a long RSA key: one answer line, two quoted character-strings.
RSA_ANSWER = (
    ";; Got answer:\n"
    ";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n"
    ";; OPT PSEUDOSECTION:\n"
    ";; ANSWER SECTION:\n"
    'v1-rsa-20260901._domainkey.innotel.us. 300 IN TXT "v=DKIM1; k=rsa; h=sha256; p=AAAABBBB" "CCCCDDDD"\n'
)
SPF_ANSWER = (
    ";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 2\n"
    ";; ANSWER SECTION:\n"
    'innotel.us. 300 IN TXT "v=spf1 a mx -all"\n'
)
EMPTY_ANSWER = ";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 3\n\n;; Query time: 1 msec\n"
TIMEOUT = ";; communications error to 192.168.1.71#53: timed out\n"


class ParseCase(unittest.TestCase):
    def test_a_split_rsa_key_reads_back_as_one_record(self):
        status, records = mail.parse_answer(RSA_ANSWER)
        self.assertEqual(status, "NOERROR")
        self.assertEqual(len(records), 1)
        self.assertIn("AAAABBBBCCCCDDDD", records[0])

    def test_no_answer_is_an_empty_list_not_an_error(self):
        status, records = mail.parse_answer(EMPTY_ANSWER)
        self.assertEqual(status, "NOERROR")
        self.assertEqual(records, [])

    def test_an_unreachable_server_is_unreadable(self):
        status, records = mail.parse_answer(TIMEOUT)
        self.assertEqual(status, "UNREADABLE")
        self.assertEqual(records, [])

    def test_a_refused_query_is_readable_as_a_status(self):
        # REFUSED is a parseable status; `query_txt` turns it into DnsUnreadable.
        status, _ = mail.parse_answer(";; ->>HEADER<<- opcode: QUERY, status: REFUSED, id: 4\n")
        self.assertEqual(status, "REFUSED")


class MatchCase(unittest.TestCase):
    def test_spf_dmarc_and_dkim_are_recognised_case_insensitively(self):
        self.assertEqual(mail._spf(["V=SPF1 -all"]), "V=SPF1 -all")
        self.assertEqual(mail._dmarc(["v=DMARC1; p=reject"]), "v=DMARC1; p=reject")
        self.assertEqual(mail._dkim(["v=DKIM1; p=abc"]), "v=DKIM1; p=abc")

    def test_a_dkim_record_without_a_key_is_not_a_match(self):
        self.assertIsNone(mail._dkim(["v=DKIM1; k=rsa"]))


class EvaluateCase(unittest.TestCase):
    def _lookup(self, texts):
        return lambda name: ("NOERROR", texts)

    def test_a_published_selector_is_a_note(self):
        finding = mail.evaluate("innotel.us", "s1", self._lookup(["v=DKIM1; p=abc"]))
        self.assertEqual(finding.level, "note")

    def test_a_missing_selector_is_a_failure_naming_it(self):
        finding = mail.evaluate("innotel.us", "s1", self._lookup([]))
        self.assertEqual(finding.level, "fail")
        self.assertIn("s1._domainkey.innotel.us", finding.message)


class AuditCase(unittest.TestCase):
    def test_a_missing_spf_is_a_failure(self):
        result = mail.audit([("innotel.us", "spf", None)])
        self.assertFalse(result.ok)
        self.assertIn("SPF", result.failures[0].message)

    def test_a_missing_dmarc_names_the_underscore_host(self):
        result = mail.audit([("innotel.us", "dmarc", None)])
        self.assertIn("_dmarc.innotel.us", result.failures[0].message)

    def test_a_present_record_i_not_a_failure(self):
        result = mail.audit([("innotel.us", "spf", "v=spf1 -all")])
        self.assertTrue(result.ok)


class CollectCase(unittest.TestCase):
    """The whole read, with `lookup` stubbed: every expected record, no network."""

    def _lookup(self, present: dict[str, list[str]]):
        def lookup(name, server):
            return "NOERROR", present.get(name, [])

        return lookup

    def test_a_fully_published_domain_is_clean(self):
        present = {
            "innotel.us": ["v=spf1 mx -all"],
            "_dmarc.innotel.us": ["v=DMARC1; p=reject"],
            "v1-ed25519-20260901._domainkey.innotel.us": ["v=DKIM1; p=abc"],
            "v1-rsa-20260901._domainkey.innotel.us": ["v=DKIM1; p=def"],
        }
        result, ok, total = mail.collect(
            {"innotel.us": {"dkim": ["v1-ed25519-20260901", "v1-rsa-20260901"]}},
            "192.168.1.71", self._lookup(present),
        )
        self.assertTrue(result.ok)
        self.assertEqual((ok, total), (4, 4))

    def test_a_dropped_dkim_selector_is_a_failure(self):
        present = {
            "innotel.us": ["v=spf1 mx -all"],
            "_dmarc.innotel.us": ["v=DMARC1; p=reject"],
            "v1-rsa-20260901._domainkey.innotel.us": ["v=DKIM1; p=def"],
        }
        result, ok, total = mail.collect(
            {"innotel.us": {"dkim": ["v1-ed25519-20260901", "v1-rsa-20260901"]}},
            "192.168.1.71", self._lookup(present),
        )
        self.assertFalse(result.ok)
        self.assertLess(ok, total)

    def test_a_refused_query_raises_dns_unreadable(self):
        def lookup(name, server):
            raise mail.DnsUnreadable(server, "REFUSED")

        with self.assertRaises(mail.DnsUnreadable):
            mail.collect({"innotel.us": {"dkim": []}}, "192.168.1.71", lookup)


class CommandCase(unittest.TestCase):
    def test_the_query_names_the_authoritative_server_and_the_txt_type(self):
        command = mail.dig_command("innotel.us", "192.168.1.71")
        self.assertIn("TXT", command)
        self.assertIn("@192.168.1.71", command)


class MainCase(unittest.TestCase):
    def _run(self, present):
        def lookup(name, server):
            return "NOERROR", present.get(name, [])

        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory) / "mail-auth.prom"
            with mock.patch.object(mail, "query_txt", side_effect=lookup):
                code = mail.main(["--prom", str(prom), "--domains", "innotel.us"])
            body = prom.read_text(encoding="utf-8") if prom.exists() else ""
        return code, body

    def test_a_clean_domain_exits_zero_and_publishes_status_one(self):
        code, body = self._run({
            "innotel.us": ["v=spf1 mx -all"],
            "_dmarc.innotel.us": ["v=DMARC1; p=reject"],
            "v1-ed25519-20260901._domainkey.innotel.us": ["v=DKIM1; p=abc"],
            "v1-rsa-20260901._domainkey.innotel.us": ["v=DKIM1; p=def"],
        })
        self.assertEqual(code, 0)
        self.assertIn('innotel_estate_check_last_status{check="mail_auth"} 1', body)
        self.assertIn('innotel_estate_check_mail_auth_records_expected{check="mail_auth"} 4', body)

    def test_a_missing_record_exits_one_and_publishes_status_zero(self):
        code, body = self._run({"innotel.us": ["v=spf1 mx -all"]})
        self.assertEqual(code, 1)
        self.assertIn('innotel_estate_check_last_status{check="mail_auth"} 0', body)

    def test_an_unreadable_zone_exits_two(self):
        with mock.patch.object(mail, "query_txt", side_effect=mail.DnsUnreadable("192.168.1.71", "timed out")):
            code = mail.main(["--domains", "innotel.us"])
        self.assertEqual(code, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
