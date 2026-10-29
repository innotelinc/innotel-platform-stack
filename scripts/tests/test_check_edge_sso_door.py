#!/usr/bin/env python3
"""Unit tests for check-edge-sso-door.py.

What matters is that the check grades the *verdict*, not the plumbing: a 200 without a
token is a failure (the endpoint answered, the user is still not signed in), a 403 with
any other body is not the refusal this estate means, and a forged request must be asked
for with headers convincing enough that only the peer test can refuse it.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-edge-sso-door.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_edge_sso_door", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


door = _load()


def build():
    return door.cases(door.LOOPBACK_URL, "learner@innotel.us",
                      "cerulean-platform,AthenIQ LMS", "cerulean-platform")


class CasesTest(unittest.TestCase):
    def test_three_verdicts_and_the_learner_carries_the_group(self):
        learner, outsider, forged = build()
        self.assertEqual([c.expect for c in (learner, outsider, forged)], [200, 403, 403])
        self.assertIn("X-Forwarded-Groups: cerulean-platform,AthenIQ LMS", learner.headers)
        self.assertNotIn("cerulean-platform", outsider.headers[-1])
        self.assertEqual(forged.url, door.EDGE_PEER_URL)

    def test_learner_headers_name_an_identity_not_a_session(self):
        learner = build()[0]
        self.assertIn("X-Forwarded-Email: learner@innotel.us", learner.headers)


class VerdictTest(unittest.TestCase):
    def test_a_token_is_what_makes_a_200_pass(self):
        learner = build()[0]
        self.assertTrue(door.verdict(learner, 200, '{"token": "abc", "expires": "x"}'))
        self.assertFalse(door.verdict(learner, 200, '{"token": ""}'))
        self.assertFalse(door.verdict(learner, 200, "not json"))
        self.assertFalse(door.verdict(learner, 403, "{}"))

    def test_the_refusal_has_to_be_the_endpoints_own(self):
        outsider = build()[1]
        self.assertTrue(door.verdict(outsider, 403,
                                     '{"error":{"code":403,"message":"Permission Denied"}}'))
        self.assertFalse(door.verdict(outsider, 403, "<html>502 Bad Gateway</html>"))
        self.assertFalse(door.verdict(outsider, 200, '{"error":{}}'))


if __name__ == "__main__":
    unittest.main()
