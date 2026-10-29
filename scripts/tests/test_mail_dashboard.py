#!/usr/bin/env python3
"""The mail dashboard and the check that feeds it must name the same metrics.

This is the pair that fails silently when it drifts: `mail.json` is provisioned
read-only from this repo (so a hand edit in Grafana is impossible and a broken file
leaves no dashboard at all), and every panel queries a series that only
`scripts/check-mail-traffic.py` writes. Rename a series there and the dashboard
does not error — it draws empty panels, which on a dashboard about mail looks like
a server that has gone quiet.

So the test builds the textfile the check would write and checks that every
`innotel_estate_check_mail_*` metric the dashboard asks for is in it, in both
directions: no panel without a series, and no series nobody can see.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "extensions/monitoring/grafana/dashboards/mail.json"
CHECK = ROOT / "scripts" / "check-mail-traffic.py"
DATASOURCE_UID = "prometheus"

sys.path.insert(0, str(ROOT / "scripts"))


def _load_check():
    spec = importlib.util.spec_from_file_location("check_mail_traffic", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check()


def _published_metrics() -> set[str]:
    """Every metric name the check can write, from a window that fills every family."""
    traffic = check.Traffic(
        lines=10,
        delivered=Counter({"local": 3}),
        failed=Counter({"permanent": 1}),
        queued=Counter({"submission": 2}),
        ingested=Counter({"ham": 2}),
        auth=Counter({("submission", "success"): 2, ("imaps", "failure"): 1}),
        senders=Counter({"innotel.us": 3}),
        recipients=Counter({"innotel.us": 2}),
        blocked_ips=1,
    )
    text = check.series(traffic)
    return set(re.findall(r"^(innotel_[a-z_]+?)(?:\{| )", text, re.MULTILINE)) | \
        set(re.findall(r"^# HELP (innotel_[a-z_]+?) ", text, re.MULTILINE))


class MailDashboardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = DASHBOARD.read_text(encoding="utf-8")
        cls.doc = json.loads(cls.raw)
        cls.exprs = [target["expr"] for panel in cls.doc["panels"]
                     for target in panel["targets"]]
        cls.queried = {metric for expr in cls.exprs for metric in
                       re.findall(r"\b(innotel_[a-z_]+)", expr)}

    def test_it_is_valid_json_with_an_identity(self):
        self.assertTrue(self.doc["title"])
        self.assertEqual(self.doc["uid"], "innotel-mail")

    def test_it_is_not_ui_editable(self):
        self.assertFalse(self.doc["editable"])

    def test_every_panel_and_target_uses_the_pinned_datasource(self):
        self.assertTrue(self.doc["panels"], "a dashboard with no panels shows nothing")
        for panel in self.doc["panels"]:
            self.assertEqual(panel["datasource"]["uid"], DATASOURCE_UID, panel["title"])
            for target in panel["targets"]:
                self.assertEqual(target["datasource"]["uid"], DATASOURCE_UID, panel["title"])

    def test_panel_ids_are_unique(self):
        ids = [panel["id"] for panel in self.doc["panels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_panel_says_what_it_is_for(self):
        # The panels carry the reasoning the checks' docstrings carry: a number with
        # no explanation is a number nobody acts on.
        for panel in self.doc["panels"]:
            self.assertTrue(panel.get("description"), panel["title"])

    def test_every_metric_it_queries_is_one_the_check_publishes(self):
        published = _published_metrics()
        unknown = {metric for metric in self.queried
                   if not metric.startswith("innotel_estate_check_") and metric != "innotel"}
        self.assertEqual(unknown, set(), "the dashboard queries a family nothing writes")
        for metric in sorted(self.queried):
            if metric.startswith("innotel_estate_check_mail_"):
                self.assertIn(metric, published,
                              f"{metric} is charted but check-mail-traffic never writes it")

    def test_the_counts_it_charts_are_all_covered(self):
        # The other direction: a series the check publishes and the dashboard never
        # shows is a measurement nobody reads. `window_hours` is exempt on purpose —
        # it is the label on every other number ("last 24 h"), not a number itself.
        exempt = {"innotel_estate_check_mail_window_hours"}
        charted = {metric for metric in self.queried
                   if metric.startswith("innotel_estate_check_mail_")} | exempt
        published = {metric for metric in _published_metrics()
                     if metric.startswith("innotel_estate_check_mail_")}
        self.assertEqual(published - charted, set(),
                         "every published mail series should have a panel")

    def test_it_shows_the_failures_that_have_no_other_signal(self):
        # A bounce is a message the sender believes was sent. Nothing else in the
        # estate can see it, so the panel is load-bearing.
        self.assertTrue(any("mail_delivery_failed" in expr for expr in self.exprs))

    def test_it_shows_the_mail_checks_beside_the_traffic(self):
        # Traffic with no verdict is a dashboard that looks calm while a check is red.
        self.assertTrue(any("last_status" in expr and "mail_traffic" in expr
                            for expr in self.exprs))

    def test_the_rationale_is_written_where_the_next_reader_looks(self):
        self.assertIsInstance(self.doc.get("__comment"), str)
        self.assertIn("check-mail-traffic", self.doc["__comment"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
