#!/usr/bin/env python3
"""A config that loads alert rules must also name the receiver.

`prometheus.host.yml` and `prometheus.yml` both mount `prometheus/rules/`, and until
2026-10-15 neither carried an `alerting:` section — so every rule in that directory was
evaluated on every evaluation interval, drawn in the host's own UI, and delivered
nowhere. It is the failure `check-alert-delivery.py` exists for, one hop further up the
chain, and it is invisible in exactly the same way: `promtool check config` says SUCCESS,
Prometheus's own `alertmanagers` page shows an empty list, the rules' alerts show as
firing, and nobody is told anything. That is why this is a test and not a comment.

The receiver's address is **read from the container address table**, not repeated here:
the estate's one Alertmanager is a container (`signara`), it is dialled at its own
address rather than a host's, and a table that moves it must move these configs with it —
so the assertion is that both files agree with the table, and with the port Signara's
group compose publishes. Parsed as text, like `test_estate_alert_rules.py`: what is under
test is the presence of the section and the address in it, which is a string.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
PROMETHEUS = ROOT / "extensions/monitoring/prometheus"

#: Every config in the extension that a compose file mounts as `/etc/prometheus/…`.
CONFIGS = sorted(PROMETHEUS.glob("*.yml"))

#: Where the receiver's published port is declared, and what `signara`'s stack maps it to.
GROUP_COMPOSE = ROOT / "groups/1-primary.yml"
PORT_DEFAULT = re.compile(r"ALERTMANAGER_BIND_PORT:-(\d+)")

#: The container whose address is the receiver's. It is the estate's only Alertmanager.
RECEIVER_CONTAINER = "signara"


def _load(name: str):
    """Load a hyphenated script by path — the address table lives in one."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def receiver_address() -> str:
    """`signara`'s address and the host it is on, from the container address table."""
    table = _load("check-container-addresses")
    for host, containers in table.EXPECTED.items():
        if RECEIVER_CONTAINER in containers:
            return containers[RECEIVER_CONTAINER]
    raise AssertionError(f"no host in the address table carries {RECEIVER_CONTAINER}")


def receiver_port() -> str:
    """The port `signara`'s group compose publishes for Alertmanager."""
    text = GROUP_COMPOSE.read_text()
    match = PORT_DEFAULT.search(text)
    if match is None:
        raise AssertionError(f"no ALERTMANAGER_BIND_PORT default in {GROUP_COMPOSE.name}")
    return match.group(1)


def alerting_targets(text: str) -> list[str]:
    """Every target under the config's top-level `alerting:` section, in order.

    Stops at the next top-level key, so a target added to some other section cannot
    satisfy the assertions below.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("alerting:"))
    except StopIteration:
        return []

    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        block.append(line)
    return re.findall(r"targets:\s*\[\s*[\"']([^\"']+)[\"']", "\n".join(block))


class PrometheusDelivery(unittest.TestCase):
    def test_the_receiver_is_where_the_address_table_says(self):
        # The premise of every assertion below: the table still carries the receiver.
        self.assertEqual(receiver_address(), "192.168.1.44")
        self.assertEqual(receiver_port(), "9093")

    def test_configs_that_load_rules_name_the_receiver(self):
        with_rules = 0
        for config in CONFIGS:
            text = config.read_text()
            if not re.search(r"^rule_files:", text, re.MULTILINE):
                continue
            with_rules += 1
            with self.subTest(config=config.name):
                targets = alerting_targets(text)
                self.assertTrue(
                    targets,
                    f"{config.name} loads rules and names no alertmanager, so every alert "
                    "it evaluates is displayed and dropped",
                )
                self.assertIn(
                    f"{receiver_address()}:{receiver_port()}",
                    targets,
                    f"{config.name} sends its alerts somewhere other than the estate's "
                    f"receiver ({receiver_address()}:{receiver_port()})",
                )
        # A glob that matched nothing would make the loop above pass vacuously.
        self.assertEqual(with_rules, 2, f"expected two configs with rules, found {with_rules}")

    def test_every_config_in_the_extension_is_covered(self):
        # The files this test reads are the ones the compose files mount; a new config
        # that loads rules is a new silent host unless it is listed here too.
        self.assertEqual(
            sorted(c.name for c in CONFIGS),
            ["prometheus.host.yml", "prometheus.yml"],
            "a config was added to the extension: decide whether it loads rules and, if "
            "it does, that it names the receiver",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
