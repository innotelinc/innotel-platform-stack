#!/usr/bin/env python3
"""The provisioned estate-checks dashboard must stay loadable and pointed at the checks.

A dashboard is code in this repo (it is provisioned read-only from
`extensions/monitoring/grafana/dashboards`), so it can break the same way anything else
can: a malformed file leaves Grafana with no dashboard and nothing in CI notices, and a
Grafana-generated datasource uid breaks every panel's query (the datasource pins `uid:
prometheus` for exactly that reason — see provisioning/datasources/datasource.yml).

This is also where the dashboard and the checks are held together. The panels query
`innotel_estate_check`, the family `scripts/estate_check.py` writes; if that name or the
latency metric changes, this fails rather than leaving a dashboard that quietly shows
nothing.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ROOT / "extensions/monitoring/grafana/dashboards"
DASHBOARD = DASHBOARDS / "estate-checks.json"
DATASOURCE_UID = "prometheus"


class DashboardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = DASHBOARD.read_text(encoding="utf-8")
        cls.doc = json.loads(cls.raw)

    def test_it_is_valid_json_with_an_identity(self):
        self.assertTrue(self.doc["title"])
        self.assertEqual(self.doc["uid"], "innotel-estate-checks")

    def test_it_is_not_ui_editable(self):
        # Provisioned dashboards are versioned here, not clicked into existence.
        self.assertFalse(self.doc["editable"])

    def test_every_panel_and_target_uses_the_pinned_datasource(self):
        panels = self.doc["panels"]
        self.assertTrue(panels, "a dashboard with no panels shows nothing")
        for panel in panels:
            self.assertEqual(panel["datasource"]["uid"], DATASOURCE_UID, panel["title"])
            for target in panel["targets"]:
                self.assertEqual(target["datasource"]["uid"], DATASOURCE_UID, panel["title"])

    def test_panel_ids_are_unique(self):
        ids = [panel["id"] for panel in self.doc["panels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_it_queries_the_shared_metric_family(self):
        self.assertIn("innotel_estate_check", self.raw)

    def test_it_charts_the_address_latency_the_check_publishes(self):
        # The panel that makes the power-save regression visible; if the metric is renamed
        # the dashboard must be renamed with it.
        self.assertIn("innotel_estate_check_address_latency_ms", self.raw)

    def test_it_charts_the_disk_headroom_the_check_publishes(self):
        self.assertIn("innotel_estate_check_host_disk_percent", self.raw)

    def test_it_shows_whether_the_alert_receiver_delivered(self):
        # The one number that says a silenced receiver is not a healthy one; if the check
        # is renamed the panel must go with it, or the dashboard shows a stale green.
        self.assertIn("innotel_estate_check_alert_delivered", self.raw)

    def test_the_rationale_is_written_where_the_next_reader_looks(self):
        # Grafana ignores unknown top-level keys, which is what lets `__comment` carry the
        # "why" beside the JSON rather than in a separate doc nobody opens.
        self.assertIsInstance(self.doc.get("__comment"), str)
        self.assertIn("estate_check", self.doc["__comment"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
