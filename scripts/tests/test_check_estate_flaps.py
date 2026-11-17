#!/usr/bin/env python3
"""Unit tests for check-estate-flaps.py — flapping read from the published series.

The distinction under test is the one that matters at 3am: a check that went red once and
repaired itself is a note; a check that goes red and green repeatedly is a warning, because
it costs the same attention as an outage. Coverage is a note of its own, so "no flaps" cannot
be read as a claim about a week of history Prometheus no longer holds.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-estate-flaps.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_estate_flaps", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

STEP = 1800
START = 1_000_000


def series(name: str, values: list[int]) -> dict:
    return {"metric": {"check": name},
            "values": [[START + index * STEP, str(value)] for index, value in enumerate(values)]}


def findings(result) -> list[tuple[str, str]]:
    return [(finding.code, finding.level) for finding in result.findings]


class Counting(unittest.TestCase):
    def test_episodes_not_transitions(self):
        # green → red → green is two transitions but ONE episode: the blip this check was
        # written for must not read as flapping.
        self.assertEqual(check.count_episodes([1, 1, 1]), 0)
        self.assertEqual(check.count_episodes([1, 0, 1]), 1)
        self.assertEqual(check.count_episodes([1, 0, 0, 1, 1]), 1)
        self.assertEqual(check.count_episodes([1, 0, 1, 0, 1]), 2)
        self.assertEqual(check.count_episodes([0, 1, 1]), 1)

    def test_nan_samples_are_dropped(self):
        item = {"values": [[START, "1"], [START + STEP, "NaN"], [START + 2 * STEP, "0"]]}
        self.assertEqual([value for _, value in check.values_of(item)], [1.0, 0.0])


class Audit(unittest.TestCase):
    def test_a_flapping_check_is_a_warning(self):
        result = check.audit([series("media_doors", [1, 0, 1, 0, 1, 1])], now=START + 6 * STEP)
        self.assertTrue(result.ok)
        self.assertIn(("flapping", "warn"), findings(result))

    def test_a_single_episode_is_a_note(self):
        result = check.audit([series("dmarc_alignment", [1, 1, 0, 1, 1])], now=START + 5 * STEP)
        self.assertIn(("single-episode", "note"), findings(result))
        self.assertNotIn(("flapping", "warn"), findings(result))

    def test_a_steady_check_is_neither(self):
        result = check.audit([series("alert_delivery", [1, 1, 1, 1])], now=START + 4 * STEP)
        self.assertEqual(findings(result), [("coverage", "note")])

    def test_coverage_names_the_oldest_sample(self):
        result = check.audit([series("x", [1, 1])], now=START + 48 * 3600)
        coverage = [f for f in result.findings if f.code == "coverage"][0]
        self.assertIn("1 check series", coverage.message)
        self.assertIn("48.0h", coverage.message)

    def test_no_history_is_a_warning_not_a_pass(self):
        result = check.audit([], now=START)
        self.assertTrue(result.ok)
        self.assertEqual(findings(result), [("no-history", "warn")])

    def test_the_window_tolerance_is_configurable(self):
        result = check.audit([series("x", [1, 0, 1, 0, 1])], now=START + 5 * STEP, max_episodes=4)
        self.assertNotIn(("flapping", "warn"), findings(result))


class Main(unittest.TestCase):
    def _run(self, *args, query=None):
        original = check.query_range
        if query is not None:
            check.query_range = query
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(list(args))
            return code, out.getvalue(), err.getvalue()
        finally:
            check.query_range = original

    def test_a_flapping_check_exits_zero_but_warns(self):
        code, out, _ = self._run("--days", "7",
                                 query=lambda *a, **k: [series("media_doors", [1, 0, 1, 0])])
        self.assertEqual(code, 0)
        self.assertIn("WARN", out)
        self.assertIn("media_doors", out)

    def test_an_unreadable_prometheus_is_exit_two(self):
        def query(*args, **kwargs):
            raise check.PrometheusUnreadable("connection refused")

        code, _, err = self._run("--days", "7", query=query)
        self.assertEqual(code, 2)
        self.assertIn("could not be queried", err)

    def test_the_textfile_carries_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flaps.prom"
            code, _, _ = self._run("--days", "7", "--prom", str(path),
                                   query=lambda *a, **k: [series("media_doors", [1, 0, 1, 0])])
            self.assertEqual(code, 0)
            body = path.read_text()
        self.assertIn('innotel_estate_check_last_status{check="estate_flaps"} 1', body)
        self.assertIn('innotel_estate_check_warnings{check="estate_flaps"} 1', body)

    def test_json_output_is_machine_readable(self):
        code, out, _ = self._run("--json", "--days", "7",
                                 query=lambda *a, **k: [series("x", [1, 1])])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["window_days"], 7)
        self.assertEqual(payload["warnings"], 0)


if __name__ == "__main__":
    unittest.main()
