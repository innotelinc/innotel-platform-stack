#!/usr/bin/env python3
"""Unit tests for check-edge-front-door.py.

The finding this exists for is a real one: `next.atheniq.courses` resolved to
`192.168.1.120` and served the catalog over plain HTTP, so the Secure CSRF cookie
was never set and the page failed with a generic error. The container was healthy
the whole time; only the name was wrong.

So the fixtures here are the three states a name can be in — on the edge, at a
container, and not resolving — plus the state that used to read as a pass: a name
that answers from both.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-edge-front-door.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_edge_front_door", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check = _load()
EDGE = "73.68.203.71"


class ClassifyTest(unittest.TestCase):
    def test_a_name_on_the_edge_passes(self) -> None:
        self.assertEqual(check.classify([EDGE], EDGE), "edge")

    def test_a_name_at_a_container_is_a_bypass(self) -> None:
        self.assertEqual(check.classify(["192.168.1.120"], EDGE), "bypass")
        self.assertEqual(check.classify(["10.20.0.5"], EDGE), "bypass")
        self.assertEqual(check.classify(["127.0.0.1"], EDGE), "bypass")

    def test_a_name_that_answers_from_both_is_still_a_finding(self) -> None:
        # Half the clients would skip the certificate and the secure-cookie path.
        self.assertEqual(check.classify([EDGE, "192.168.1.59"], EDGE), "mixed")

    def test_a_name_that_does_not_resolve_is_reported_separately(self) -> None:
        self.assertEqual(check.classify([], EDGE), "unresolved")

    def test_a_second_public_address_is_not_the_edge(self) -> None:
        self.assertEqual(check.classify(["203.0.113.9"], EDGE), "bypass")


class PublicAddressTest(unittest.TestCase):
    def test_private_and_loopback_are_not_public(self) -> None:
        for address in ("192.168.1.120", "10.0.0.1", "172.16.4.2", "127.0.0.1", "169.254.1.1", "::1", "fd00::1"):
            self.assertFalse(check.is_public(address), address)

    def test_the_edge_is_public(self) -> None:
        self.assertTrue(check.is_public(EDGE))


class TargetsFileTest(unittest.TestCase):
    """The pin file is `name<TAB>host<TAB>port<TAB>why`, not one name per line.

    Reading whole lines as names produced eight invented failures the first time this
    was wired to `edge-vhost-targets.tsv`, and one line then died inside `idna` — a
    crash that, in a unit whose every other check publishes a textfile, would have
    taken this series quiet rather than red.
    """

    def test_only_the_first_column_is_a_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pins.tsv"
            path.write_text(
                "# name\thost\tport\twhy\n"
                "next.example\t192.168.1.120\t80\tthe catalog\n"
                "\n"
                "bot.example\t192.168.1.52\t8801\topenmausbot\n",
                encoding="utf-8",
            )
            self.assertEqual(check.read_targets(str(path)), ["next.example", "bot.example"])

    def test_a_malformed_name_is_a_finding_not_a_crash(self) -> None:
        self.assertEqual(check.resolve("a..b"), [])
        self.assertEqual(check.resolve("x" * 80), [])


class MainTest(unittest.TestCase):
    def test_exit_code_is_one_when_any_name_bypasses(self) -> None:
        original = check.resolve
        try:
            check.resolve = lambda name, port=443: ["192.168.1.120"] if name == "next.example" else [EDGE]
            self.assertEqual(check.main(["next.example", "catalog.example"]), 1)
            check.resolve = lambda name, port=443: [EDGE]
            self.assertEqual(check.main(["catalog.example"]), 0)
            check.resolve = lambda name, port=443: []
            self.assertEqual(check.main(["gone.example"]), 1)
        finally:
            check.resolve = original

    def test_nothing_to_check_is_not_a_pass(self) -> None:
        self.assertEqual(check.main([]), 2)


if __name__ == "__main__":
    unittest.main()
