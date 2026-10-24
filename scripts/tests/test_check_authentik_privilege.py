#!/usr/bin/env python3
"""Unit tests for check-authentik-privilege.py — the four rules and where they bite.

The 2026-10-23 audit found `plutus-admins` and `signara-admins` flagged as superuser
groups while their members were already superusers: the estate looked right and the next
person added to a tenant group would have become a global administrator. These tests pin
that case, the machine-account case, and the two warnings, so the rule set cannot drift
back to "this looked fine when a human read it".
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-authentik-privilege.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_authentik_privilege", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


privilege = _load()

NOW = time.mktime(time.strptime("2026-10-23T12:00:00", "%Y-%m-%dT%H:%M:%S"))


def group(name: str, superuser: bool = False) -> dict:
    return {"name": name, "is_superuser": superuser}


def user(name: str, groups: list[dict], superuser: bool = False, kind: str = "internal", last_login: str | None = None, active: bool = True) -> dict:
    return {
        "username": name,
        "type": kind,
        "is_superuser": superuser,
        "is_active": active,
        "last_login": last_login,
        "groups_obj": groups,
    }


def codes(result) -> list[str]:
    return [finding.code for finding in result.findings]


class SuperuserGroupCase(unittest.TestCase):
    def test_a_tenant_group_flagged_superuser_is_a_failure(self):
        result = privilege.audit(
            [user("dhunter", [group("plutus-admins", True)])],
            [group("plutus-admins", True), group("authentik Admins", True)],
            now=NOW,
        )
        self.assertIn("tenant_superuser_group", codes(result))
        self.assertFalse(result.ok)

    def test_the_estate_group_is_allowed_to_grant_superuser(self):
        result = privilege.audit([], [group("authentik Admins", True)], now=NOW)
        self.assertEqual(result.findings, [])

    def test_a_plain_admin_group_is_not_a_finding(self):
        result = privilege.audit([], [group("plutus-admins", False)], now=NOW)
        self.assertEqual(result.findings, [])


class ServiceAccountCase(unittest.TestCase):
    def test_a_non_human_account_with_admin_rights_fails(self):
        result = privilege.audit(
            [user("outpost", [group("authentik Admins", True)], kind="outpost", last_login=None)],
            [group("authentik Admins", True)],
            now=NOW,
        )
        self.assertIn("service_account_admin", codes(result))
        self.assertFalse(result.ok)

    def test_a_normal_login_with_admin_rights_is_not_a_service_finding(self):
        result = privilege.audit(
            [user("admin", [group("authentik Admins", True)], superuser=True, last_login="2026-10-23T09:00:00Z")],
            [group("authentik Admins", True)],
            now=NOW,
        )
        self.assertEqual(codes(result), [])


class DormantSuperuserCase(unittest.TestCase):
    def test_never_signed_in_warns(self):
        result = privilege.audit(
            [user("akadmin", [group("authentik Admins", True)], superuser=True)],
            [group("authentik Admins", True)],
            now=NOW,
        )
        self.assertIn("dormant_superuser", codes(result))
        self.assertTrue(result.ok)  # a warning, not a failure

    def test_unused_for_the_line_warns(self):
        result = privilege.audit(
            [user("old", [group("authentik Admins", True)], superuser=True, last_login="2026-06-01T00:00:00Z")],
            [group("authentik Admins", True)],
            now=NOW,
        )
        self.assertIn("dormant_superuser", codes(result))

    def test_a_recent_sign_in_is_quiet(self):
        result = privilege.audit(
            [user("fresh", [group("authentik Admins", True)], superuser=True, last_login="2026-10-22T00:00:00Z")],
            [group("authentik Admins", True)],
            now=NOW,
        )
        self.assertEqual(result.findings, [])

    def test_the_line_is_configurable(self):
        result = privilege.audit(
            [user("fresh", [group("authentik Admins", True)], superuser=True, last_login="2026-10-01T00:00:00Z")],
            [group("authentik Admins", True)],
            now=NOW,
            dormant_days=7,
        )
        self.assertIn("dormant_superuser", codes(result))


class CrossTenantCase(unittest.TestCase):
    def test_two_admin_groups_without_superuser_warns(self):
        result = privilege.audit(
            [user("ops", [group("plutus-admins"), group("signara-admins")], last_login="2026-10-20T00:00:00Z")],
            [group("plutus-admins"), group("signara-admins")],
            now=NOW,
        )
        self.assertIn("cross_tenant_admin", codes(result))
        self.assertTrue(result.ok)

    def test_one_admin_group_is_quiet(self):
        result = privilege.audit(
            [user("ops", [group("plutus-admins")], last_login="2026-10-20T00:00:00Z")],
            [group("plutus-admins")],
            now=NOW,
        )
        self.assertEqual(result.findings, [])

    def test_a_superuser_is_allowed_to_reach_across(self):
        result = privilege.audit(
            [user("dhunter", [group("plutus-admins"), group("signara-admins")], superuser=True, last_login="2026-10-20T00:00:00Z")],
            [group("plutus-admins"), group("signara-admins")],
            now=NOW,
        )
        self.assertEqual(codes(result), [])


class SeriesCase(unittest.TestCase):
    def test_the_counts_are_published(self):
        body = privilege.privilege_series(
            [user("a", [], superuser=True), user("b", [])],
            [group("authentik Admins", True), group("plutus-admins", True)],
        )
        self.assertIn('innotel_estate_check_authentik_superusers{check="authentik_privilege"} 1', body)
        self.assertIn('innotel_estate_check_authentik_superuser_groups{check="authentik_privilege"} 2', body)
        self.assertIn('group="plutus-admins"', body)


class EnvCase(unittest.TestCase):
    def test_quoted_and_plain_values_are_read(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write('AUTHENTIK_API_URL="https://sso.example/api/v3"\nAUTHENTIK_BOOTSTRAP_TOKEN=abc\n# comment\n')
            path = handle.name
        try:
            env = privilege.read_env(path)
        finally:
            Path(path).unlink()
        self.assertEqual(env["AUTHENTIK_API_URL"], "https://sso.example/api/v3")
        self.assertEqual(env["AUTHENTIK_BOOTSTRAP_TOKEN"], "abc")


if __name__ == "__main__":
    unittest.main()
