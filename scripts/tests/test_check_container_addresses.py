#!/usr/bin/env python3
"""Unit tests for check-container-addresses.py — the address invariant.

The cases are the 2026-10-01 outage, taken apart: a container that moved to a DHCP
address, a stack that publishes on an address it does not have (the `Exited (255)`
containers), and a container whose address is still handed out by DHCP so the same thing
can happen again. Each case builds the estate as data and asserts what the check said, so
a regression here is a regression in the check's reason to exist.

The module under test has hyphens in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
CHECK = SCRIPTS / "check-container-addresses.py"

sys.path.insert(0, str(SCRIPTS))
from estate_check import prometheus_text  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("check_container_addresses", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


chk = _load()


def estate(overrides=None):
    """The expected estate, with the named containers replaced.

    `overrides` is keyed by `(host, container)`, so a case reads as the one thing it
    changes: `estate({("i1", "ontrak"): dict(state="RUNNING", address="192.168.1.20")})`.
    """
    observed = {
        host: [chk.Instance(name=name, state="RUNNING", address=address) for name, address in containers.items()]
        for host, containers in chk.EXPECTED.items()
    }
    for (host, name), change in (overrides or {}).items():
        instances = observed[host]
        for index, inst in enumerate(instances):
            if inst.name == name:
                instances[index] = chk.Instance(name=name, **change)
                break
        else:
            instances.append(chk.Instance(name=name, **change))
    return observed


class AddressCase(unittest.TestCase):
    def test_the_estate_as_it_should_be_has_no_failures(self):
        result = chk.audit(estate())
        self.assertTrue(result.ok, [f.message for f in result.failures])

    def test_a_container_that_moved_is_a_failure_that_names_what_broke(self):
        # The outage: ontrak came up on .20 instead of .21.
        result = chk.audit(estate({("i1", "ontrak"): dict(state="RUNNING", address="192.168.1.20")}))
        self.assertFalse(result.ok)
        moved = [f for f in result.failures if f.code == "address_moved"]
        self.assertEqual(len(moved), 1)
        self.assertIn("192.168.1.20", moved[0].message)
        self.assertIn("192.168.1.21", moved[0].message)
        # It says what is dialled there, so the reader knows the blast radius.
        self.assertIn("ontrak family", moved[0].message)

    def test_a_stack_publishing_on_an_address_it_does_not_have_is_a_failure(self):
        # The Exited (255) containers: `.env` names .21, the container is at .20.
        result = chk.audit(estate({("i1", "ontrak"): dict(state="RUNNING", address="192.168.1.20")}))
        mismatched = [f for f in result.failures if f.code == "bind_mismatch"]
        self.assertEqual(len(mismatched), 2, "both ONTRAK_API_BIND and ONTRAK_WEB_BIND")
        self.assertTrue(all("cannot bind" in f.message for f in mismatched))

    def test_a_dhcp_address_is_a_warning_not_a_pass(self):
        result = chk.audit(estate({("i1", "ontrak"): dict(state="RUNNING", address="192.168.1.21", pinned=False)}))
        self.assertTrue(result.ok, "right address is not a failure")
        warnings = [f for f in result.findings if f.level == "warn"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "not_pinned")
        self.assertIn("DHCP", warnings[0].message)

    def test_a_completely_absent_container_is_a_failure(self):
        observed = estate()
        observed["i1"] = [i for i in observed["i1"] if i.name != "ontrak"]
        result = chk.audit(observed)
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["missing"])

    def test_a_stopped_container_is_reported_and_not_failed(self):
        # A stopped container has no address to be wrong; it is a note, not a failure.
        result = chk.audit(estate({("i1", "mail"): dict(state="STOPPED", address=None)}))
        self.assertTrue(result.ok, [f.message for f in result.failures])
        self.assertEqual([f.code for f in result.findings if f.level == "note"], ["stopped"])

    def test_a_running_container_the_table_forgot_is_reported(self):
        observed = estate()
        observed["i1"].append(chk.Instance(name="newthing", state="RUNNING", address="192.168.1.99"))
        result = chk.audit(observed)
        self.assertTrue(result.ok, "a stale table is not a broken address")
        self.assertEqual([f.code for f in result.findings if f.level == "warn"], ["unexpected"])

    def test_a_running_container_without_an_address_is_a_failure(self):
        result = chk.audit(estate({("i2", "www"): dict(state="RUNNING", address=None)}))
        self.assertFalse(result.ok)
        self.assertEqual([f.code for f in result.failures], ["no_address"])


class ParseCase(unittest.TestCase):
    def test_the_address_is_read_out_of_incus_csv(self):
        text = (
            'monarch,RUNNING,"192.168.1.56 (eth0)\n'
            "172.20.0.1 (br-ff11149a0a80)\n"
            '172.17.0.1 (docker0)"'
        )
        self.assertEqual(chk._first_lan_address(text), "192.168.1.56")

    def test_a_container_with_only_bridge_addresses_has_no_lan_address(self):
        text = '"172.20.0.1 (br-ff11149a0a80)\n172.17.0.1 (docker0)"'
        self.assertIsNone(chk._first_lan_address(text))

    def test_the_table_covers_every_host_the_estate_runs_containers_on(self):
        self.assertEqual(set(chk.EXPECTED), set(chk.HOSTS))

    def test_every_declared_bind_names_a_container_the_table_holds(self):
        # A bind for a host or container the table does not carry would never be read,
        # which is the check quietly losing the case it was written for.
        for host, containers in chk.BINDS.items():
            self.assertIn(host, chk.EXPECTED)
            for name, binds in containers.items():
                self.assertIn(name, chk.EXPECTED[host])
                for var, address in binds.items():
                    self.assertEqual(
                        address,
                        chk.EXPECTED[host][name],
                        f"{host} {name}: {var} is a promise about the address the table names",
                    )


class PromCase(unittest.TestCase):
    """The scheduled signal: a renumber must reach an alert, not sit in a log."""

    def test_a_clean_run_publishes_status_one_and_advances_success(self):
        text = prometheus_text(chk.CHECK, chk.audit(estate()), ran_ok=True, now=1000.0)
        self.assertIn('innotel_estate_check_last_status{check="container_address"} 1', text)
        self.assertIn('innotel_estate_check_failures{check="container_address"} 0', text)
        self.assertIn('innotel_estate_check_last_success_timestamp{check="container_address"} 1000', text)

    def test_a_moved_address_publishes_status_zero_and_names_the_count(self):
        result = chk.audit(estate({("i1", "ontrak"): dict(state="RUNNING", address="192.168.1.20")}))
        text = prometheus_text(chk.CHECK, result, ran_ok=True, now=2000.0, prior_success=900.0)
        self.assertIn('innotel_estate_check_last_status{check="container_address"} 0', text)
        self.assertIn(f'innotel_estate_check_failures{{check="container_address"}} {len(result.failures)}', text)
        self.assertGreater(len(result.failures), 0)
        # A bad run must not look like a fresh success — the stale-rule depends on it.
        self.assertIn('innotel_estate_check_last_success_timestamp{check="container_address"} 900', text)

    def test_a_run_that_could_not_happen_is_a_failure_not_silence(self):
        # Exit 2 (a host unreachable) must read as status 0, because "no data" is how
        # the 2026-10-01 outage stayed invisible.
        text = prometheus_text(chk.CHECK, None, ran_ok=False, now=3000.0, prior_success=900.0)
        self.assertIn('innotel_estate_check_last_status{check="container_address"} 0', text)
        self.assertIn('innotel_estate_check_last_run_timestamp{check="container_address"} 3000', text)
        self.assertNotIn("innotel_estate_check_failures", text)
        self.assertIn('innotel_estate_check_last_success_timestamp{check="container_address"} 900', text)

    def test_the_written_file_carries_a_good_run_forward_over_a_bad_one(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/container-address.prom"
            chk.write_prom(path, chk.CHECK, chk.audit(estate()), ran_ok=True, now=1000.0)
            chk.write_prom(path, chk.CHECK, None, ran_ok=False, now=2000.0)
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            # The textfile collector reads as an unprivileged user; a 0600 file reads
            # as "no metrics" and took this alert path dark once already.
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertIn('innotel_estate_check_last_status{check="container_address"} 0', body)
        self.assertIn('innotel_estate_check_last_success_timestamp{check="container_address"} 1000', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
