#!/usr/bin/env python3
"""Unit tests for check-orion-signin.py — which hop of the NAS door is broken.

The property that matters is the one nothing else could see on 2026-11-14: the proxy
trusted an issuer of one brand while the estate serves the same authentik under two, and
each brand reports **itself** as `issuer`. The login page answered 200, the API answered
403, and the only symptom was a 500 at the callback — which a bogus code produces too, so
it distinguishes nothing. These tests fix the three statements (configured issuer,
discovery `issuer`, the redirect the door actually emits) and check that every way they can
disagree is named, and that a door that answers nothing is exit 2 rather than a pass.

Everything runs against fake fetches; nothing here reaches a host.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-orion-signin.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_orion_signin", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

INNOTEL = "https://auth.innotel.us/application/o/orion/"
CERULEAN = "https://auth.cerulean.innotel.us/application/o/orion/"
CLIENT = "e_OcHDay5XrzR1XO9tC287aBJ37gyE3J7QQDw4zYtbE"


def authorize_url(issuer: str = INNOTEL, client_id: str = CLIENT) -> str:
    return (
        f"{issuer}authorize/?client_id={client_id}&redirect_uri=https%3A%2F%2Fnas.innotel.us"
        "%2Foauth2%2Fcallback&response_type=code&scope=openid+email+profile+groups"
    )


def fake(*, issuer=INNOTEL, discovery_issuer=None, start=None,
         authorize=(200, {}, b"<html>login</html>"), discovery_status=200):
    """A fake `get` for the three URLs the check asks about.

    The default redirect follows `issuer`, because that is what a healthy door does: a
    fixed brand here would make the "other brand" case fail with `redirect_wrong_brand`
    and pass the test for the wrong reason.
    """
    discovery_issuer = issuer if discovery_issuer is None else discovery_issuer
    if start is None:
        start = (302, {"Location": authorize_url(issuer)})

    def get(url, timeout=None):
        if url.endswith(".well-known/openid-configuration"):
            if discovery_status != 200:
                return discovery_status, {}, b""
            body = ('{"issuer": "%s"}' % discovery_issuer).encode()
            return 200, {}, body
        if url.endswith("/oauth2/start"):
            # A case may give a 2-tuple (status, headers) or a 3-tuple with a body.
            status = start[0]
            headers = start[1] if len(start) > 1 else {}
            return status, headers, b""
        return authorize

    return get


def codes(result) -> list[str]:
    return [finding.code for finding in result.findings]


def measure(**kwargs) -> "check.Measured":
    issuer = kwargs.pop("issuer", INNOTEL)
    client = kwargs.pop("client_id", CLIENT)
    return check.gather(issuer, client, get=fake(issuer=issuer, **kwargs))


class Agreement(unittest.TestCase):
    def test_the_three_statements_agreeing_is_a_note_not_a_silence(self):
        result = check.audit(measure())
        self.assertTrue(result.ok)
        self.assertEqual(codes(result), ["sign_in_agrees"])
        self.assertEqual(result.notes[0].level, "note")


class Issuer(unittest.TestCase):
    def test_a_discovery_that_names_the_other_brand_is_the_500_class(self):
        # The exact 2026-11-14 shape: the file says one brand, the document the tokens are
        # signed against says the other.
        result = check.audit(measure(discovery_issuer=CERULEAN))
        self.assertFalse(result.ok)
        self.assertIn("issuer_mismatch", codes(result))
        message = next(f.message for f in result.findings if f.code == "issuer_mismatch")
        self.assertIn(INNOTEL, message)
        self.assertIn(CERULEAN, message)

    def test_a_missing_trailing_slash_is_a_mismatch_not_a_detail(self):
        # oauth2-proxy compares the strings, so this is the same 500 with a subtler cause.
        result = check.audit(measure(discovery_issuer=INNOTEL.rstrip("/")))
        self.assertIn("issuer_mismatch", codes(result))

    def test_a_discovery_that_is_not_a_200_is_named(self):
        result = check.audit(measure(discovery_status=503))
        self.assertIn("discovery_status", codes(result))

    def test_a_discovery_that_does_not_answer_is_named(self):
        measured = check.gather(INNOTEL, CLIENT, get=fake(discovery_status=200))
        measured.discovery_status = None
        self.assertIn("discovery_unreachable", codes(check.audit(measured)))

    def test_an_unset_issuer_says_the_proxy_will_not_start(self):
        result = check.audit(check.Measured(issuer="", client_id=CLIENT))
        self.assertEqual(codes(result), ["issuer_unset"])

    def test_the_other_brand_is_a_warning_when_it_agrees_with_itself(self):
        # Self-consistent on cerulean works. It is not where the estate is going, which is
        # a warning and not a page — docs/identity-hosts.md.
        result = check.audit(measure(issuer=CERULEAN))
        self.assertTrue(result.ok)
        self.assertEqual([(f.code, f.level) for f in result.findings],
                         [("brand_not_canonical", "warn"), ("sign_in_works", "note")])


class Door(unittest.TestCase):
    def test_a_redirect_to_the_brand_it_will_not_trust_is_named(self):
        result = check.audit(measure(start=(302, {"Location": authorize_url(CERULEAN)})))
        self.assertIn("redirect_wrong_brand", codes(result))

    def test_a_different_client_is_named(self):
        result = check.audit(measure(start=(302, {"Location": authorize_url(client_id="other")})))
        self.assertIn("client_mismatch", codes(result))

    def test_the_authorize_endpoint_refusing_the_door_is_named(self):
        result = check.audit(measure(authorize=(400, {}, b"invalid_client")))
        self.assertIn("authorize_status", codes(result))
        message = next(f.message for f in result.findings if f.code == "authorize_status")
        self.assertIn("400", message)

    def test_a_status_that_is_not_a_redirect_is_named(self):
        result = check.audit(measure(start=(200, {}, b"<html>who are you</html>")))
        self.assertIn("start_status", codes(result))

    def test_a_redirect_with_no_location_is_named(self):
        result = check.audit(measure(start=(302, {})))
        self.assertIn("no_location", codes(result))


class EnvFile(unittest.TestCase):
    def test_comments_blanks_and_quotes_are_read_the_way_compose_reads_them(self):
        values = check.parse_env(
            "# Written by provision-authentik.py\n"
            "\n"
            f"{check.ISSUER_KEY}={INNOTEL}\n"
            f'{check.CLIENT_KEY}="{CLIENT}"\n'
        )
        self.assertEqual(values[check.ISSUER_KEY], INNOTEL)
        self.assertEqual(values[check.CLIENT_KEY], CLIENT)
        self.assertNotIn("# Written", values)


class ExitCodes(unittest.TestCase):
    """`main` with the two things it reaches the world through replaced.

    `fetch` is patched rather than `gather`, because `gather` resolves it at call time —
    so this drives the real reading path and only the network is fake.
    """

    def _run(self, *args, env_text=f"{check.ISSUER_KEY}={INNOTEL}\n{check.CLIENT_KEY}={CLIENT}\n",
             get=None):
        original_read, original_fetch = check.read_env_text, check.fetch
        check.read_env_text = lambda path, host: (env_text, "")
        check.fetch = fake(**(get or {}))
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(list(args))
            return code, out.getvalue(), err.getvalue()
        finally:
            check.read_env_text, check.fetch = original_read, original_fetch

    def test_a_healthy_door_exits_zero(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("0 failure(s)", out)
        self.assertIn("the door redirects to auth.innotel.us", out)

    def test_a_redirect_to_the_wrong_brand_exits_one(self):
        code, out, _ = self._run(get={"start": (302, {"Location": authorize_url(CERULEAN)})})
        self.assertEqual(code, 1)
        self.assertIn("the door sends people to auth.cerulean.innotel.us", out)

    def test_an_unreadable_env_exits_two(self):
        code, _, err = self._run(env_text=None)
        self.assertEqual(code, 2)
        self.assertIn("could not read the proxy's env", err)

    def test_an_env_missing_the_issuer_exits_two(self):
        code, _, err = self._run(env_text=f"{check.CLIENT_KEY}={CLIENT}\n")
        self.assertEqual(code, 2)
        self.assertIn(check.ISSUER_KEY, err)

    def test_a_door_that_answers_nothing_is_exit_two_not_a_failure(self):
        # Nothing about a door answering nothing is a verdict about its configuration.
        code, _, err = self._run(get={"start": (None, {})})
        self.assertEqual(code, 2)
        self.assertIn("cannot start a sign-in", err)

    def test_the_textfile_carries_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orion-signin.prom"
            code, _, _ = self._run(
                "--prom", str(path), get={"discovery_issuer": CERULEAN})
            self.assertEqual(code, 1)
            body = path.read_text()
        self.assertIn('innotel_estate_check_last_status{check="orion_signin"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="orion_signin"} 1', body)
        self.assertNotIn("last_success_timestamp", body)


if __name__ == "__main__":
    unittest.main()
