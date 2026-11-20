#!/usr/bin/env python3
"""Tests for `scripts/check-vaultwarden-config-drift.py`.

The two things worth testing are pure and are the two ways this check can lie: a parser
that reads the wrong service's `environment:` block, and a comparison that reports
agreement when it has compared nothing.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "check-vaultwarden-config-drift.py"
SPEC = importlib.util.spec_from_file_location("check_vaultwarden_config_drift", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

COMPOSE = """
services:
  vaultwarden:
    image: vaultwarden/server:1.37.5
    environment:
      # Outbound mail for password hints.
      SMTP_HOST: "mail.innotel.us"
      SIGNUPS_VERIFY: "false"
      ADMIN_TOKEN: 'quoted'
    volumes:
      - ./vw-data/:/data/
  other:
    environment:
      SIGNUPS_VERIFY: "true"
"""


class ParseComposeEnvironment(unittest.TestCase):
    def test_reads_the_first_services_block_only(self):
        env = MODULE.parse_compose_environment(COMPOSE)
        self.assertEqual(env["SMTP_HOST"], "mail.innotel.us")
        self.assertEqual(env["ADMIN_TOKEN"], "quoted", "single quotes are stripped too")

    def test_a_later_service_does_not_leak_in(self):
        env = MODULE.parse_compose_environment(COMPOSE)
        self.assertEqual(env["SIGNUPS_VERIFY"], "false", "the second service's value must not win")

    def test_comments_and_keys_outside_the_block_are_ignored(self):
        env = MODULE.parse_compose_environment(COMPOSE)
        self.assertNotIn("image", env)
        self.assertNotIn("Outbound mail for password hints.", env)


class Compare(unittest.TestCase):
    def test_agreement_is_silent(self):
        self.assertEqual(
            MODULE.compare({"SIGNUPS_VERIFY": "false"}, {"signups_verify": False}),
            [],
            "a case-insensitive match across the two surfaces is agreement",
        )

    def test_a_disagreement_is_reported_with_both_values(self):
        differing = MODULE.compare({"SIGNUPS_VERIFY": "false"}, {"signups_verify": True})
        self.assertEqual(differing, [("SIGNUPS_VERIFY", "false", "True")])

    def test_a_credential_is_reported_by_length_only(self):
        differing = MODULE.compare({"SMTP_PASSWORD": "a" * 40}, {"smtp_password": "b" * 12})
        self.assertEqual(differing, [("SMTP_PASSWORD", "<40 chars>", "<12 chars>")])

    def test_keys_only_one_surface_holds_are_not_compared(self):
        self.assertEqual(
            MODULE.compare({"PANEL_ONLY": "1", "ENV_ONLY": "2"}, {"panel_only": 1}),
            [],
            "only the overlap is the trap; a key in one place takes effect normally",
        )


if __name__ == "__main__":
    unittest.main()
