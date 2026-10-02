#!/usr/bin/env python3
"""Unit tests for check-estate-inventory.py — the estate-inventory invariant.

The cases are the two things the 2026-10-01 pass found by eye and nothing would have
told it about: a trusted host the inventory did not name (`i4`) and a storage pool that
appeared on i2 (`main-pool` — since consolidated onto, `tank` retired). Each case builds
the hosts as data and asserts what the check said.

The module under test has hyphens in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
CHECK = SCRIPTS / "check-estate-inventory.py"

sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("check_estate_inventory", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inv = _load()

ALL_KNOWN_REMOTES = ["192.168.1.51", "192.168.1.52", "192.168.1.53", "192.168.1.54"]


def views(overrides=None):
    """The estate as it should be, with hosts replaced by `overrides` (name → HostView)."""
    out = []
    for name in inv.HOSTS:
        out.append(
            inv.HostView(
                name=name,
                remotes=list(ALL_KNOWN_REMOTES),
                pools=sorted(inv.POOLS[name]),
                reachable=True,
            )
        )
    for name, change in (overrides or {}).items():
        for index, view in enumerate(out):
            if view.name == name:
                out[index] = change
                break
        else:
            out.append(change)
    return out


class InventoryCase(unittest.TestCase):
    def test_the_estate_as_it_should_be_has_no_failures(self):
        result = inv.audit(views())
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual(result.warnings, [])

    def test_a_trusted_host_the_inventory_does_not_name_is_a_failure(self):
        # The i4 case: reachable, trusted, and absent from the page.
        result = inv.audit(views({"i1": inv.HostView("i1", remotes=[*ALL_KNOWN_REMOTES, "192.168.1.99"], pools=["incus"])}))
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["unknown_host"])
        self.assertIn("192.168.1.99", result.failures[0].message)

    def test_a_storage_pool_the_inventory_does_not_name_is_a_failure(self):
        # A pool appeared on a host beside the one it is allowed to have.
        result = inv.audit(views({"i2": inv.HostView("i2", remotes=ALL_KNOWN_REMOTES, pools=["main-pool", "scratch"])}))
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["unknown_pool"])
        self.assertIn("scratch", result.failures[0].message)

    def test_a_named_pool_that_is_gone_is_a_warning_not_a_failure(self):
        # i2's one pool is gone but the host is otherwise as the inventory describes.
        result = inv.audit(views({"i2": inv.HostView("i2", remotes=ALL_KNOWN_REMOTES, pools=[])}))
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual([f.code for f in result.warnings], ["missing_pool"])
        self.assertIn("main-pool", result.warnings[0].message)

    def test_an_unreachable_bench_is_a_warning_and_hides_nothing(self):
        result = inv.audit(
            views(
                {
                    "i4": inv.HostView("i4", reachable=False),
                    "i3": inv.HostView("i3", remotes=ALL_KNOWN_REMOTES, pools=["tank", "extra"]),
                }
            )
        )
        self.assertFalse(result.ok, "the bench being down must not excuse an unknown pool")
        self.assertEqual([f.code for f in result.failures], ["unknown_pool"])
        self.assertEqual([f.code for f in result.warnings], ["bench_unreachable"])

    def test_the_remote_url_is_read_for_its_address(self):
        self.assertEqual(inv._remote_address("https://192.168.1.52:8443"), "192.168.1.52")
        self.assertIsNone(inv._remote_address("unix://"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
