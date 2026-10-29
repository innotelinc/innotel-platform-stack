#!/usr/bin/env python3
"""Unit tests for estate_check.py — the telemetry contract every check publishes.

The contract is what the alert rules and the dashboard read, so the shape of the textfile
is the thing under test: one series per check for the verdict, and — since 2026-10-29 —
the **notes** as their own series. Notes used to reach the terminal and nowhere else, so
the panel could show "0 failures" while a check was saying `parked`, `DEV-OFF` or
"signara.innotel.us is a CNAME" — stated, true, and invisible to everyone who was not
watching that run scroll past.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import estate_check  # noqa: E402


class NotesCase(unittest.TestCase):
    def _text(self, findings):
        return estate_check.prometheus_text(
            "edge_vhost_targets", estate_check.Audit(findings=findings), True, 1000.0)

    def test_a_note_is_counted_and_published_with_its_reason(self):
        text = self._text([
            estate_check.Finding("development_target", "note", "192.168.1.93:8080 is off"),
            estate_check.Finding("dead", "fail", "10.0.0.1:80 does not answer"),
        ])
        self.assertIn('innotel_estate_check_notes{check="edge_vhost_targets"} 1', text)
        self.assertIn(
            'innotel_estate_check_note{check="edge_vhost_targets",code="development_target",'
            'detail="192.168.1.93:8080 is off"} 1', text)
        # A note is not a failure: the verdict is the failure's.
        self.assertIn('innotel_estate_check_last_status{check="edge_vhost_targets"} 0', text)
        self.assertIn('innotel_estate_check_failures{check="edge_vhost_targets"} 1', text)

    def test_notes_alone_keep_the_check_green(self):
        text = self._text([estate_check.Finding("parked", "note", "127.0.0.1:9 is parked")])
        self.assertIn('innotel_estate_check_last_status{check="edge_vhost_targets"} 1', text)
        self.assertIn('innotel_estate_check_notes{check="edge_vhost_targets"} 1', text)

    def test_the_detail_label_is_escaped(self):
        """A message carries quotes and newlines; a raw one would break the scrape."""
        text = self._text([estate_check.Finding(
            "alias-not-a-mail-domain", "note",
            'signara.innotel.us is a CNAME to "innotel.us"\nand inherits its DMARC')])
        line = next(line for line in text.splitlines() if line.startswith(
            "innotel_estate_check_note{"))
        self.assertIn('\\"innotel.us\\"', line)
        self.assertIn("\\n", line)
        self.assertEqual(line.count("\n"), 0, "an escaped newline must not split the series")
        self.assertTrue(line.endswith("} 1"))

    def test_a_clean_run_publishes_no_note_series(self):
        text = self._text([])
        self.assertIn('innotel_estate_check_notes{check="edge_vhost_targets"} 0', text)
        self.assertNotIn("innotel_estate_check_note{", text)

    def test_a_check_that_could_not_run_publishes_no_counts(self):
        text = estate_check.prometheus_text("host_disk", None, False, 1000.0)
        self.assertIn('innotel_estate_check_last_status{check="host_disk"} 0', text)
        self.assertNotIn("_notes{", text)


if __name__ == "__main__":
    unittest.main()
