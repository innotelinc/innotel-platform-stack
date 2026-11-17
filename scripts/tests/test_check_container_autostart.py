#!/usr/bin/env python3
"""Unit tests for check-container-autostart.py — the "would it come back" invariant.

The cases are the two measured ones: `i2`'s nine instances all ran with
`boot.autostart=false` on 2026-11-17 after the host rejoined the estate, and `onyx` runs
that way on purpose because its disk is a USB device it must not start without. One is a
failure and the other is a note, and telling them apart is the whole check.

The module under test has hyphens in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
CHECK = SCRIPTS / "check-container-autostart.py"

sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("check_container_autostart", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


auto = _load()


def instance(name: str, state: str = "RUNNING", autostart: str | None = "true") -> "auto.Instance":
    return auto.Instance(name=name, state=state, autostart=autostart)


class AutostartCase(unittest.TestCase):
    def test_an_estate_that_would_come_back_has_no_findings(self):
        result = auto.audit({"i2": [instance("monarch"), instance("nas")], "i1": [instance("mail")]})
        self.assertTrue(result.ok)
        self.assertEqual(result.findings, [])

    def test_a_running_instance_that_would_not_come_back_is_a_failure(self):
        # The 2026-11-17 shape: the instance answers every probe and would vanish at boot.
        result = auto.audit({"i2": [instance("monarch", autostart="false"), instance("nas")]})
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["autostart_off"])
        message = result.failures[0].message
        for expected in ("i2 monarch", "boot.autostart=false", "incus config set monarch boot.autostart true"):
            self.assertIn(expected, message)

    def test_the_deliberate_case_is_a_note_not_a_failure(self):
        # `onyx` starts by hand once its USB disk is present; that must not read as drift,
        # and it must not read as silence either.
        result = auto.audit({"i1": [instance("onyx", autostart="false")]})
        self.assertTrue(result.ok)
        self.assertEqual(result.warnings, [])
        self.assertEqual([f.code for f in result.notes], ["autostart_off_deliberate"])
        self.assertIn("USB", result.notes[0].message)

    def test_the_exemption_is_keyed_by_host_not_only_by_name(self):
        # A container called `onyx` anywhere else is not the one with the USB disk.
        result = auto.audit({"i3": [instance("onyx", autostart="false")]})
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["autostart_off"])

    def test_a_stopped_instance_with_the_flag_off_is_not_a_finding(self):
        # A parked copy or a rollback; check-retired-instances.py owns that direction.
        result = auto.audit({"i1": [instance("monarch-retired", state="STOPPED", autostart="false")]})
        self.assertTrue(result.ok)
        self.assertEqual(result.findings, [])

    def test_an_unset_flag_counts_as_on(self):
        # Incus starts a container with no `boot.autostart`, so a missing key is not drift.
        self.assertTrue(auto.autostart_on(None))
        self.assertTrue(auto.autostart_on(""))
        result = auto.audit({"i4": [instance("vault", autostart=None)]})
        self.assertTrue(result.ok)

    def test_every_spelling_of_off_is_read_as_off(self):
        for value in ("false", "FALSE", "0", "no", "off", " false "):
            self.assertFalse(auto.autostart_on(value), value)
        for value in ("true", "TRUE", "1", "yes", "on"):
            self.assertTrue(auto.autostart_on(value), value)

    def test_the_flag_is_read_from_the_expanded_config(self):
        # The host acts on the profile-merged value, so the check must read that one.
        payload = json.dumps(
            [
                {"name": "monarch", "status": "Running", "expanded_config": {"boot.autostart": "false"}},
                {"name": "nas", "status": "Running", "expanded_config": {}},
                {"name": "sage", "status": "Stopped", "expanded_config": {"boot.autostart": "false"}},
            ]
        )
        views = auto.gather({"i2": "root@192.168.1.52"}, runner=lambda target, command: payload)
        self.assertEqual(
            views["i2"],
            [
                auto.Instance("monarch", "RUNNING", "false"),
                auto.Instance("nas", "RUNNING", None),
                auto.Instance("sage", "STOPPED", "false"),
            ],
        )
        self.assertEqual([f.code for f in auto.audit(views).failures], ["autostart_off"])

    def test_every_exemption_states_a_reason(self):
        for host, instances in auto.EXEMPT.items():
            for name, reason in instances.items():
                self.assertTrue(reason.strip(), f"{host} {name} is exempt with no reason")
                self.assertIn(host, auto.HOSTS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
