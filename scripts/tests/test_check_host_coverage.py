#!/usr/bin/env python3
"""Every estate host must be read by every check — no host left silently unwatched.

The 2026-10-02 `i4` incident was not one bug but two, and the second is the one this
test is for. `i4` was reachable but unread by the checks: the inventory listed it as an
*optional* bench (an unreachable `i4` was a warning, not a failure), and neither
`check-container-addresses.py` nor `check-container-limits.py` named it at all. So when
the edge could not ssh into `i4` — its key was not in `i4`'s `authorized_keys` and `i4`
was not in the edge's `/root/.ssh/config` — every check still exited clean and nothing
alerted. The host was simply outside everything.

`check-estate-inventory.py` now makes every host required (`OPTIONAL` is empty), so an
unreadable host is exit 2 and `EstateCheckCouldNotRun` fires. That closes the *runtime*
hole. This closes the *repo-side* one: a host cannot be added to one check's list and
left out of another's, and cannot be pinned to a different address in one place than
another, because that is how a host goes unwatched without anyone editing an alert.

A host missing from `EXPECTED` is read but never asserted about — as good as absent.
A host in a different check at a different address is two checks reading two estates.
Both fail here.

It guards the same thing for `check-host-latency.py` (which reads HOSTS directly) and for
`trust-estate-hosts.py` (which decides which addresses the edge's ssh config must carry):
a host the checks read but the trust script does not name is a host that is read only
until the key is missing, and the reverse is a config entry for a box nobody watches.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inv = _load("check-estate-inventory")
addr = _load("check-container-addresses")
lim = _load("check-container-limits")
lat = _load("check-host-latency")
trust = _load("trust-estate-hosts")


class CoverageCase(unittest.TestCase):
    def test_the_inventory_names_at_least_one_host(self):
        self.assertTrue(inv.HOSTS, "an empty inventory watches nothing")

    def test_every_check_reads_the_same_hosts(self):
        self.assertEqual(set(addr.HOSTS), set(inv.HOSTS))
        self.assertEqual(set(lim.HOSTS), set(inv.HOSTS))
        self.assertEqual(set(lat.HOSTS), set(inv.HOSTS))
        self.assertEqual(set(trust.HOSTS), set(inv.HOSTS))

    def test_every_check_reads_each_host_at_the_same_address(self):
        # Same names is not enough: a host named in two checks but pinned to two
        # addresses is two checks reading two different boxes.
        self.assertEqual(addr.HOSTS, inv.HOSTS)
        self.assertEqual(lim.HOSTS, inv.HOSTS)
        self.assertEqual(lat.HOSTS, inv.HOSTS)
        self.assertEqual(trust.HOSTS, inv.HOSTS)

    def test_the_inventory_names_a_pool_for_every_host(self):
        self.assertEqual(set(inv.POOLS), set(inv.HOSTS))

    def test_the_address_check_has_a_row_for_every_host(self):
        # A host in HOSTS but not in EXPECTED is read and then asserted about not at
        # all — the check would pass on any address it came up on.
        self.assertEqual(set(addr.EXPECTED), set(addr.HOSTS))

    def test_no_host_is_optional(self):
        # The exact shape of the i4 gap: a host the checks are allowed to skip.
        self.assertEqual(inv.OPTIONAL, set())


class DiagnosticCase(unittest.TestCase):
    def test_an_unreadable_host_names_the_two_host_side_files(self):
        # The runtime half of the guard: when ssh cannot reach a host, the exit-2
        # message must name the host and the two untracked files that make key-only ssh
        # work — otherwise it reads as an outage and takes a hand-run to tell apart from
        # a missing key.
        from estate_check import HostUnreadable

        message = str(HostUnreadable("i4", "root@192.168.1.54", "exit status 255"))
        self.assertIn("i4", message)
        self.assertIn("192.168.1.54", message)
        self.assertIn("authorized_keys", message)
        self.assertIn(".ssh/config", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
