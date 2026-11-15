#!/usr/bin/env python3
"""Unit tests for trust-estate-hosts.py — the two untracked files, made reproducible.

`i4` could not be read by the checks because two host-side files were missing, and
neither is in the repo: the host's `authorized_keys` and the edge's `/root/.ssh/config`.
This script is the fix, so what has to hold is that it is **idempotent** (a second run
changes nothing), that it **only owns its marked block** (a hand-written `Host` entry for
something else survives), and that `--check` answers the question without a password or a
write. Those are the properties tested here.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

TRUST = SCRIPTS / "trust-estate-hosts.py"

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN35j2EowvIN3w0mCkAGcYr20gKVQkrXXvLGpbiZuc0L container-address-check"
BLOB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN35j2EowvIN3w0mCkAGcYr20gKVQkrXXvLGpbiZuc0L"

HAND_WRITTEN = "# my own host\nHost example\n  HostName 192.0.2.10\n"


def _load():
    spec = importlib.util.spec_from_file_location("trust_estate_hosts", TRUST)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


trust = _load()


class ConfigBlockCase(unittest.TestCase):
    def test_the_block_names_every_host_address_and_the_check_key(self):
        block = trust.config_block()
        for target in trust.HOSTS.values():
            self.assertIn(trust.address_of(target), block)
        self.assertIn(trust.CHECK_KEY, block)
        self.assertIn("IdentitiesOnly yes", block)
        self.assertTrue(block.startswith(trust.BEGIN))
        self.assertTrue(block.endswith(trust.END))

    def test_one_host_line_pattern_matches_how_the_checks_dial(self):
        # The checks build `root@<address>` themselves, so the pattern has to be the
        # address; and IdentitiesOnly must be set or ssh also offers the agent's keys.
        block = trust.config_block({"i1": "root@192.168.1.51"})
        self.assertIn("Host 192.168.1.51", block)
        self.assertIn("IdentityFile /root/.ssh/container-address\n", block + "\n")


class ConfigSpliceCase(unittest.TestCase):
    def test_a_config_with_no_block_gets_one_appended(self):
        out = trust.config_with(HAND_WRITTEN)
        self.assertIn("# my own host", out)
        self.assertIn(trust.BEGIN, out)
        self.assertIn("192.168.1.54", out)

    def test_appending_twice_is_idempotent(self):
        once = trust.config_with("")
        self.assertEqual(trust.config_with(once), once)

    def test_a_hand_written_host_survives_a_rewrite(self):
        installed = trust.config_with(HAND_WRITTEN)
        again = trust.config_with(installed)
        self.assertEqual(again, installed)
        self.assertIn("HostName 192.0.2.10", again)
        self.assertEqual(again.count(trust.BEGIN), 1, "one block, replaced in place, never doubled")

    def test_drift_is_noticed(self):
        self.assertTrue(trust.config_drift(HAND_WRITTEN))
        self.assertFalse(trust.config_drift(trust.config_with(HAND_WRITTEN)))


class AuthorizedKeysCase(unittest.TestCase):
    def test_the_key_is_read_without_its_comment(self):
        self.assertEqual(trust.key_blob(PUBKEY), BLOB)

    def test_a_key_with_an_options_prefix_still_matches(self):
        # authorized_keys lines may carry `from="…"` or `command=…` before the key, and
        # ssh matches the key itself — so a present key must not be appended a second time.
        line = f'from="192.168.1.71" {BLOB}'
        self.assertTrue(trust.key_present(line, PUBKEY))

    def test_an_absent_key_is_not_reported_present(self):
        self.assertFalse(trust.key_present("# empty\n", PUBKEY))

    def test_the_install_is_guarded_and_locked_down(self):
        script = trust.authorized_keys_install(PUBKEY)
        self.assertIn(f"grep -qF '{BLOB}'", script)
        self.assertIn(BLOB, script)
        self.assertIn("chmod 600 /root/.ssh/authorized_keys", script)
        self.assertIn("install -d -m 700 /root/.ssh", script)


class CheckModeCase(unittest.TestCase):
    """`--check` is the key path plus the config block, with no password and no writes."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.config = Path(self._dir.name) / "config"

    def _hosts(self):
        return "i1=root@192.168.1.51,i2=root@192.168.1.52,i3=root@192.168.1.53,i4=root@192.168.1.54"

    def test_a_host_whose_key_is_not_trusted_is_drift(self):
        self.config.write_text(trust.config_with(""), encoding="utf-8")
        original = trust.key_works
        trust.key_works = lambda target: target.endswith("192.168.1.51")  # only i1 works
        self.addCleanup(lambda: setattr(trust, "key_works", original))
        code = trust.main(["--check", "--config", str(self.config), "--hosts", self._hosts()])
        self.assertEqual(code, 1)

    def test_a_fully_trusted_estate_passes(self):
        self.config.write_text(trust.config_with(""), encoding="utf-8")
        original = trust.key_works
        trust.key_works = lambda target: True
        self.addCleanup(lambda: setattr(trust, "key_works", original))
        code = trust.main(["--check", "--config", str(self.config), "--hosts", self._hosts()])
        self.assertEqual(code, 0)

    def test_an_adrift_config_is_drift_even_when_the_keys_work(self):
        self.config.write_text(HAND_WRITTEN, encoding="utf-8")
        original = trust.key_works
        trust.key_works = lambda target: True
        self.addCleanup(lambda: setattr(trust, "key_works", original))
        code = trust.main(["--check", "--config", str(self.config), "--hosts", self._hosts()])
        self.assertEqual(code, 1, "keys that work are not enough — the edge must also know where the host is")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
