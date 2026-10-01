#!/usr/bin/env python3
"""Unit tests for stack-lib.sh — the shared LAN-address and .env helpers.

These cover the two rules that decide whether a *published* address is useful:

* ``stack_lib_agent_net_env`` — the network a builder deployment is handed. It
  derives every value from the host's LAN address (``stack_lib_lan_ip``), and
  with no LAN address it must fall back to loopback-and-private rather than
  naming an address nothing answers on. An operator's own value always wins.
* ``stack_lib_env_set`` — how provisioning writes that into a ``.env``. It runs
  on every ``up``, so it has to be idempotent, and it must never clobber a value
  somebody set by hand.

``stack_lib_lan_ip`` is stubbed inside each shell so the tests do not depend on
the machine they run on having a LAN interface (CI does not).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "stack-lib.sh"


def run_sourced(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a snippet with the library sourced, failing loudly on error."""
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n. "{LIB}"\n{script}'],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def agent_env(lan: str, env: dict | None = None) -> dict[str, str]:
    """``stack_lib_agent_net_env`` as a dict, with the LAN IP stubbed."""
    proc = run_sourced(
        f"stack_lib_lan_ip() {{ printf '%s' {lan!r}; }}\nstack_lib_agent_net_env",
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    pairs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        pairs[key] = value
    return pairs


class AgentNetEnvTest(unittest.TestCase):
    def test_a_lan_address_becomes_the_builder_network(self) -> None:
        env = agent_env("192.168.1.21")
        self.assertEqual(env["LAN_IP"], "192.168.1.21")
        self.assertEqual(env["AGENT_LAN_IP"], "192.168.1.21")
        # The app binds every interface so the published port is the host's, and
        # the address is advertised; the sandbox shares that same network.
        self.assertEqual(env["AGENT_PREVIEW_HOST"], "all")
        self.assertEqual(env["AGENT_PREVIEW_PUBLISH"], "true")
        self.assertEqual(env["AGENT_SANDBOX_NETWORK"], "host")

    def test_no_lan_address_stays_private(self) -> None:
        env = agent_env("")
        self.assertEqual(env["LAN_IP"], "")
        self.assertEqual(env["AGENT_LAN_IP"], "")
        # Nothing to name, so nothing is exposed and nothing is advertised.
        self.assertEqual(env["AGENT_PREVIEW_HOST"], "loopback")
        self.assertEqual(env["AGENT_PREVIEW_PUBLISH"], "false")
        self.assertEqual(env["AGENT_SANDBOX_NETWORK"], "none")

    def test_an_operators_value_wins(self) -> None:
        env = agent_env(
            "192.168.1.21",
            env={"AGENT_LAN_IP": "10.9.9.9", "AGENT_PREVIEW_PUBLISH": "false"},
        )
        self.assertEqual(env["AGENT_LAN_IP"], "10.9.9.9")
        self.assertEqual(env["AGENT_PREVIEW_PUBLISH"], "false")
        # ...while the keys they did not set are still derived.
        self.assertEqual(env["AGENT_PREVIEW_HOST"], "all")


class EnvSetTest(unittest.TestCase):
    def _provision(self, body: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("LAN_IP=\nKEEP=1\n")
            proc = run_sourced(body.format(env=str(env_file)))
            if proc.returncode != 0:
                raise AssertionError(proc.stderr)
            return env_file.read_text()

    def test_it_replaces_an_empty_value_then_leaves_it_alone(self) -> None:
        text = self._provision(
            'stack_lib_env_set "{env}" LAN_IP 192.168.1.21\n'
            'stack_lib_env_set "{env}" LAN_IP 192.168.1.99\n'
        )
        self.assertEqual(text.count("LAN_IP="), 1)
        self.assertIn("LAN_IP=192.168.1.21", text)

    def test_it_never_clobbers_a_set_value_without_force(self) -> None:
        text = self._provision(
            'stack_lib_env_set "{env}" KEEP 2\n'
            'stack_lib_env_set "{env}" KEEP 3 --force\n'
            'stack_lib_env_set "{env}" NEW value\n'
        )
        self.assertIn("KEEP=3", text)
        self.assertIn("NEW=value", text)

    def test_it_appends_a_key_that_is_not_there(self) -> None:
        # LAN_IP is already in the template; a key that is not gets appended.
        text = self._provision('stack_lib_env_set "{env}" AGENT_LAN_IP 192.168.1.21\n')
        self.assertTrue(text.rstrip().endswith("AGENT_LAN_IP=192.168.1.21"))


class SelfTestTest(unittest.TestCase):
    def test_the_library_self_test_reports_the_agent_network(self) -> None:
        proc = subprocess.run(
            ["bash", str(LIB), "--selftest"], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LAN_IP=", proc.stdout)
        self.assertIn("AGENT_LAN_IP=", proc.stdout)
        self.assertIn("AGENT_SANDBOX_NETWORK=", proc.stdout)


if __name__ == "__main__":
    unittest.main()
