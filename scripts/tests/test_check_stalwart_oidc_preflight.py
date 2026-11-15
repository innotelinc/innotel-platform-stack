#!/usr/bin/env python3
"""The preflight's output is the contract between a shell script and a check.

The check streams `stalwart-oidc-discovery-setup.sh` into the container and reads what
`preflight` prints, so the two files share a format and neither owns it. That is the
seam worth testing: a change to the preflight's `printf` that nobody notices would not
break the runbook (a person still reads it) and would silently turn every fact into
"no verdict" here — which is exit 2, a permanent "could not run" on a healthy server.

The parser, the codes it derives and the rules over them are pure, so they are what is
asserted. The one end-to-end case uses an injected runner, because the real one needs a
mail container.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check-stalwart-oidc-preflight")

#: A run as the script actually prints it: section headings, padded labels, both markers.
CLEAN = """
units
  ok   stalwart                active
  ok   stalwart-oidc-proxy     active
  ok   stalwart-oidc-redirect  active
the copy
  ok   issuer served           https://auth.cerulean.innotel.us/application/o/stalwart-webui/
  ok   copy age                31s
the pieces
  ok   leaf covers             auth.cerulean.innotel.us
  ok   anchor expires          Nov 11 21:29:35 2036 GMT
  ok   dnat rules              2
the fetch and the server
  ok   provider from here      200
  ok   bundle readable         stalwart
  ok   copy readable           oidcproxy
  ok   bearer probe            401

PREFLIGHT OK: every piece the switch depends on is here and working
"""

DIRTY = """
the pieces
  ok   leaf covers             auth.cerulean.innotel.us
  FAIL dnat rules              0
  FAIL bearer probe            no answer

PREFLIGHT NOT OK: fix the lines above before changing anything
"""


class ParseCase(unittest.TestCase):
    def test_every_fact_is_read_with_its_label_intact(self):
        facts = check.parse(CLEAN)
        self.assertEqual(len(facts.lines), 12)
        self.assertEqual(facts.lines[0], (True, "stalwart", "active"))
        # A label with a space in it must survive: splitting on the first space would
        # have produced ("leaf", "covers  auth.cerulean…").
        self.assertIn((True, "leaf covers", "auth.cerulean.innotel.us"), facts.lines)
        self.assertEqual(facts.verdict, "OK")

    def test_the_section_headings_are_not_facts(self):
        labels = [label for _, label, _ in check.parse(CLEAN).lines]
        for heading in ("units", "the copy", "the pieces", "the fetch and the server"):
            self.assertNotIn(heading, labels)

    def test_a_failed_line_is_the_only_kind_that_counts(self):
        facts = check.parse(DIRTY)
        self.assertEqual(
            facts.failures,
            [("dnat rules", "0"), ("bearer probe", "no answer")],
        )
        self.assertEqual(facts.verdict, "NOT OK")

    def test_output_with_no_verdict_is_not_a_pass(self):
        # What a truncated run looks like. It has facts and no summary, and the check
        # turns that into exit 2 rather than into "0 failures".
        facts = check.parse("  ok   stalwart                active\n")
        self.assertIsNone(facts.verdict)
        self.assertEqual(len(facts.lines), 1)


class AuditCase(unittest.TestCase):
    def test_a_clean_run_is_one_note_and_no_failure(self):
        report = check.audit(check.parse(CLEAN))
        self.assertEqual(report.failures, [])
        self.assertEqual(len(report.notes), 1)
        self.assertIn("12 facts", report.notes[0].message)

    def test_each_failing_line_becomes_a_named_finding(self):
        report = check.audit(check.parse(DIRTY))
        self.assertEqual([f.code for f in report.failures], ["dnat_rules", "bearer_probe"])
        self.assertIn("0", report.failures[0].message)

    def test_not_ok_with_no_marked_line_is_still_a_failure(self):
        # The preflight's own contract: it only says NOT OK when `rc` was set, which only
        # `need` does. If that ever stops being true, the check must not read as green.
        facts = check.Facts(lines=[], verdict="NOT OK")
        report = check.audit(facts)
        self.assertEqual([f.code for f in report.failures], ["verdict_without_reason"])

    def test_the_codes_are_label_slugs(self):
        self.assertEqual(check.code_for("leaf covers"), "leaf_covers")
        self.assertEqual(check.code_for("stalwart-oidc-redirect"), "stalwart_oidc_redirect")


class RunCase(unittest.TestCase):
    def test_a_refusal_is_exit_two_and_a_verdict_is_exit_zero(self):
        calls = []

        def runner(script, host, container, **_):
            calls.append(host)
            return check.Result(0, CLEAN, "")

        original = check.run_preflight
        check.run_preflight = runner
        try:
            self.assertEqual(check.main([]), 0)
        finally:
            check.run_preflight = original
        self.assertEqual(calls, [check.HOST])

    def test_the_container_saying_this_is_not_the_mail_server_is_exit_two(self):
        original = check.run_preflight
        check.run_preflight = lambda *a, **k: check.Result(
            2, "", "CANNOT RUN: /etc/stalwart-oidc and /etc/stalwart/stalwart.env are not both here")
        try:
            self.assertEqual(check.main([]), 2)
        finally:
            check.run_preflight = original

    def test_a_preflight_that_finds_something_is_exit_one(self):
        original = check.run_preflight
        check.run_preflight = lambda *a, **k: check.Result(1, DIRTY, "")
        try:
            self.assertEqual(check.main([]), 1)
        finally:
            check.run_preflight = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
