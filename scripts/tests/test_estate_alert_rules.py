#!/usr/bin/env python3
"""The alert rules and the checks they watch must name the same checks.

`EstateCheckStale` and `EstateCheckNotPinned` key on a series, so they can only speak
about a check whose textfile reached node-exporter. `EstateCheckAbsent` is the rule that
catches the other case — a check that never reported at all — and it can only do that
because it lists each known check by name, one `absent(...)` branch each. That list is
the one place the rule set and the checks can silently drift: add a fourth check with no
branch, and its absence is invisible again, which is the failure the whole rule exists to
close (`docs/container-placement.md` §2026-10-01).

So this reads the checks' `CHECK` constants and the rule file, and fails if the two sets
are not the same. It parses the YAML as text on purpose — the point is the presence of a
branch per check, and that is a string, not a structure.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
RULES = ROOT / "extensions/monitoring/prometheus/rules/estate-checks.yml"

sys.path.insert(0, str(SCRIPTS))

#: The scripts that publish the shared metric family, each exporting `CHECK`.
CHECK_SCRIPTS = [
    "check-container-addresses",
    "check-container-limits",
    "check-estate-inventory",
    "check-address-latency",
]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _block(text: str, alert: str) -> str:
    """The text of the `- alert: <alert>` rule, up to the next `- alert:` or the end."""
    start = re.search(rf"^      - alert: {re.escape(alert)}\n", text, re.MULTILINE)
    if start is None:
        raise AssertionError(f"no rule named {alert} in {RULES.name}")
    tail = text[start.end():]
    end = re.search(r"^      - alert: ", tail, re.MULTILINE)
    return tail[: end.start()] if end else tail


class RuleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checks = {_load(name).CHECK for name in CHECK_SCRIPTS}
        cls.text = RULES.read_text(encoding="utf-8")

    def test_there_is_an_absent_rule(self):
        self.assertIn("absent(", _block(self.text, "EstateCheckAbsent"))

    def test_every_check_has_an_absent_branch(self):
        block = _block(self.text, "EstateCheckAbsent")
        named = set(re.findall(r'check="([^"]+)"', block))
        self.assertEqual(
            named,
            self.checks,
            "every check must be named in EstateCheckAbsent, or its absence is silent again",
        )

    def test_no_absent_branch_names_a_check_that_does_not_exist(self):
        # The other direction: a removed check leaves a branch that can never fire.
        named = set(re.findall(r'check="([^"]+)"', _block(self.text, "EstateCheckAbsent")))
        self.assertEqual(named - self.checks, set())

    def test_each_branch_keeps_the_check_label(self):
        # Without label_replace the whole rule fires as one anonymous series, and the
        # annotation cannot say which check went missing.
        block = _block(self.text, "EstateCheckAbsent")
        self.assertEqual(block.count("label_replace("), len(self.checks))
        # The summary is what a reader sees first, so it has to carry the name too.
        self.assertIn("{{ $labels.check }}", block)

    def test_the_other_rules_reference_the_shared_family(self):
        for alert in ("EstateCheckFailing", "EstateCheckCouldNotRun", "EstateCheckStale", "EstateCheckNotPinned"):
            self.assertIn("innotel_estate_check", _block(self.text, alert), alert)

    def test_the_stale_window_is_a_whole_day(self):
        # The checks run every 30 minutes, so 24h is the point at which "not one clean run
        # for a whole day" is unambiguous. Locked here so a later edit cannot quietly
        # widen the window back to a day-and-change.
        block = _block(self.text, "EstateCheckStale")
        self.assertRegex(block, r">\s*24\s*\*\s*60\s*\*\s*60")
        self.assertIn("24 hours", block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
