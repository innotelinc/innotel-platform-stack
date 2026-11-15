#!/usr/bin/env python3
"""Unit tests for check-orion-signin.py — the redirect that *is* the login page.

authentik answers `/application/o/authorize/` with a 302 into its own flow executor and
serves the form there. Reading that 302 as "no login page" failed a door that works
(measured 2026-11-15), so what is locked here is the shape of the judgement: a redirect
into a 200 flow page on the issuer's own host reaches a login page, a redirect that lands
anywhere else does not, and a provider that serves the form itself still counts.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-orion-signin.py"
ISSUER = "https://auth.innotel.us"
CLIENT = "e_OcHDay5XrzR1XO9tC287aBJ37gyE3J7QQDw4zYtbE"
AUTHORIZE = f"{ISSUER}/application/o/authorize/?client_id={CLIENT}&response_type=code"
FLOW = f"/if/flow/default-authentication-flow/?client_id={CLIENT}"


def _load():
    spec = importlib.util.spec_from_file_location("check_orion_signin", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()


def fake_get(authorize=(302, {"Location": FLOW}), flow=(200, {})):
    """The door's three answers, with only the URLs the check reads actually served."""
    def get(url):
        if url.endswith(".well-known/openid-configuration"):
            return 200, {}, json.dumps({"issuer": ISSUER}).encode()
        if url.endswith("/oauth2/start"):
            return 302, {"Location": AUTHORIZE}, b""
        if url.startswith(AUTHORIZE.split("?")[0]):
            return authorize[0], authorize[1], b""
        return flow[0], flow[1], b""
    return get


def measured(**kwargs):
    return probe.gather(ISSUER, CLIENT, get=fake_get(**kwargs))


class FlowRedirectCase(unittest.TestCase):
    def test_the_page_the_authorize_request_hands_on_to_is_measured(self):
        result = measured()
        self.assertEqual(result.authorize_status, 302)
        self.assertEqual(result.login_status, 200)
        self.assertEqual(result.login_location, ISSUER + FLOW)

    def test_a_door_that_lands_on_its_flow_is_clean(self):
        result = probe.audit(measured())
        self.assertEqual(result.failures, [])
        self.assertEqual([f.code for f in result.findings], ["sign_in_agrees"])

    def test_a_flow_page_that_does_not_answer_is_a_failure(self):
        result = probe.audit(measured(flow=(500, {})))
        self.assertEqual([f.code for f in result.failures], ["authorize_status"])
        self.assertIn("never reaches a login page", result.failures[0].message)
        self.assertIn("500", result.failures[0].message)

    def test_a_redirect_to_another_name_reaches_no_login_page(self):
        result = probe.audit(measured(authorize=(302, {"Location": "https://elsewhere.example/login"})))
        self.assertEqual([f.code for f in result.failures], ["authorize_status"])

    def test_a_redirect_with_nothing_to_follow_is_a_failure(self):
        result = probe.audit(measured(authorize=(302, {})))
        self.assertEqual([f.code for f in result.failures], ["authorize_status"])

    def test_a_provider_that_serves_the_form_itself_still_counts(self):
        result = probe.audit(measured(authorize=(200, {})))
        self.assertEqual(result.failures, [])


if __name__ == "__main__":
    unittest.main()
