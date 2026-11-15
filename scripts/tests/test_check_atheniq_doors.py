#!/usr/bin/env python3
"""Unit tests for check-atheniq-doors.py — the classroom's gate, on both its names.

The property that matters is one a person would get wrong by eye: a classroom that answers
200 is not "up", it is *open*, and the links the courseware sends are three path segments
deep rather than `/`. So the fixtures are a correct gate on both doors and one broken
statement at a time — a client id that belongs to another app, a callback naming a
different door, an issuer that is neither of the estate's, a gate that covers only `/`.

`classroom.atheniq.courses` is allowed two states and no third: gated exactly like the
classroom, or redirecting to it. That is the retirement window written down in
`docs/atheniq-classroom-name.md`, and the note it produces is how the dashboard shows the
retirement happened rather than someone having to remember it did.

Everything here runs against a fake probe.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-atheniq-doors.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_atheniq_doors", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

CLASSROOM = "class.atheniq.courses"
ALIAS = "classroom.atheniq.courses"


def gate(host: str, *, client: str = "classroom", callback: str | None = None,
         auth: str = "auth.innotel.us") -> str:
    """The authorize URL a correct gate emits for `host`."""
    callback = callback or f"https://{host}/oauth2/callback"
    return (f"https://{auth}/application/o/authorize/?approval_prompt=auto"
            f"&client_id={client}&redirect_uri={quote(callback, safe='')}"
            "&response_type=code&scope=openid")


def answer(location: str, status: int = 302):
    return (status, {"Location": location}, b"", None)


OK = answer(gate(CLASSROOM))
OK_DEEP = answer(gate(CLASSROOM))
ALIAS_GATED = answer(gate(ALIAS))
ALIAS_DEEP = answer(gate(ALIAS))


def probe_for(*, root=OK, deep=OK_DEEP, alias=ALIAS_GATED, alias_deep=ALIAS_DEEP):
    answers = {
        f"https://{CLASSROOM}/": root,
        f"https://{CLASSROOM}/classroom/": deep,
        f"https://{ALIAS}/": alias,
        f"https://{ALIAS}/classroom/": alias_deep,
    }

    def probe(url, timeout=20.0):
        return answers[url]

    return probe


def codes(result) -> list[str]:
    return [finding.code for finding in result.findings]


class Healthy(unittest.TestCase):
    def test_both_doors_gated_on_the_canonical_issuer_is_clean(self):
        result, answered = check.audit(probe=probe_for())
        self.assertTrue(answered)
        self.assertTrue(result.ok)
        self.assertEqual(codes(result), [])

    def test_the_deep_path_is_probed_and_gated(self):
        # The links the courseware sends are `${host}/classroom/<id>`; a gate that covers
        # only `/` is the failure this pair of probes exists to separate.
        probe = probe_for(deep=(200, {}, b"<!doctype html>app", None))
        result, _ = check.audit(probe=probe)
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["deep_path_ungated"])

    def test_a_deep_path_that_answers_404_is_also_open(self):
        # A 404 from the app means the request *reached* the app: no gate, and a learner
        # following a real link sees a missing page rather than a sign-in.
        result, _ = check.audit(probe=probe_for(deep=(404, {}, b"", None)))
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["deep_path_ungated"])


class GateStatements(unittest.TestCase):
    def test_an_open_door_is_the_failure_this_check_exists_for(self):
        result, _ = check.audit(probe=probe_for(root=(200, {}, b"<html>catalog</html>", None)))
        self.assertFalse(result.ok)
        self.assertIn("door_open", codes(result))

    def test_the_other_identity_host_is_a_note_not_a_warning(self):
        # A door consistently on the cerulean name signs people in correctly, and
        # docs/identity-hosts.md makes the brand a direction rather than a correctness
        # property. It is a *note*, not a warning, for two reasons this asserts: the check
        # must stay green (no failure), and it must not hand `EstateCheckNotPinned` a
        # warning that pages every hour for a state only a provider change can end.
        result, _ = check.audit(probe=probe_for(
            root=answer(gate(CLASSROOM, auth="auth.cerulean.innotel.us")),
            deep=answer(gate(CLASSROOM, auth="auth.cerulean.innotel.us"))))
        self.assertTrue(result.ok)
        self.assertEqual(result.warnings, [])
        # Once for the door, not once per path: the deep probe is the same name.
        self.assertEqual([(f.code, f.level) for f in result.findings],
                         [("issuer_alias", "note")])

    def test_a_client_belonging_to_another_app_is_named(self):
        result, _ = check.audit(probe=probe_for(
            root=answer(gate(CLASSROOM, client="atheniq-studio")),
            deep=answer(gate(CLASSROOM, client="atheniq-studio"))))
        self.assertFalse(result.ok)
        self.assertIn("wrong_client", codes(result))

    def test_a_callback_naming_another_door_is_named(self):
        # The 2026-11-06 shape: `classroom.` asked for a callback that was not registered.
        result, _ = check.audit(probe=probe_for(
            root=answer(gate(CLASSROOM, callback=f"https://{ALIAS}/oauth2/callback")),
            deep=answer(gate(CLASSROOM, callback=f"https://{ALIAS}/oauth2/callback"))))
        self.assertFalse(result.ok)
        self.assertIn("wrong_callback", codes(result))

    def test_a_foreign_issuer_is_not_treated_as_the_estate_s_gate(self):
        result, _ = check.audit(probe=probe_for(
            root=answer("https://login.example.org/application/o/authorize/?client_id=classroom"),
            deep=answer("https://login.example.org/application/o/authorize/?client_id=classroom")))
        self.assertFalse(result.ok)
        self.assertIn("issuer_unrecognised", codes(result))

    def test_a_gateway_error_is_the_app_being_down(self):
        result, _ = check.audit(probe=probe_for(root=(502, {}, b"<html>502</html>", None)))
        self.assertFalse(result.ok)
        self.assertIn("door_status", codes(result))

    def test_a_certificate_that_does_not_cover_the_name_is_its_own_finding(self):
        # "Unreachable" would be wrong: the name answered. And exit 2 would be wrong too —
        # an uncovered certificate is a fact about the door, not a failure to judge it.
        result, answered = check.audit(probe=probe_for(root=(None, {}, b"", "tls")))
        self.assertTrue(answered)
        self.assertFalse(result.ok)
        self.assertEqual(codes(result), ["tls_not_covering"])
        self.assertIn(CLASSROOM, result.findings[0].message)


class Alias(unittest.TestCase):
    def test_the_alias_gated_like_the_classroom_is_accepted(self):
        result, _ = check.audit(probe=probe_for())
        self.assertEqual(codes(result), [])

    def test_the_alias_moved_to_the_classroom_is_accepted_with_a_note(self):
        # The documented end state: a 301 to the name the courseware uses, path preserved.
        moved = answer(f"https://{CLASSROOM}/classroom/", 301)
        result, _ = check.audit(probe=probe_for(alias=moved))
        self.assertTrue(result.ok)
        self.assertEqual([(f.code, f.level) for f in result.findings],
                         [("alias_moved", "note")])

    def test_the_alias_gated_with_another_client_is_still_a_failure(self):
        # Two names, two providers: the retirement is a move, not a second application.
        result, _ = check.audit(probe=probe_for(
            alias=answer(gate(ALIAS, client="atheniq-ai")),
            alias_deep=answer(gate(ALIAS, client="atheniq-ai"))))
        self.assertFalse(result.ok)
        self.assertEqual(set(codes(result)), {"wrong_client"})

    def test_the_alias_being_gone_is_a_note_rather_than_a_failure(self):
        # It is expendable by design, so deleting it must not page — the canonical door is
        # what the check is for, and it is asserted separately.
        gone = (None, {}, b"", "transport")
        result, answered = check.audit(probe=probe_for(alias=gone, alias_deep=gone))
        self.assertTrue(answered)
        self.assertTrue(result.ok)
        self.assertEqual([(f.code, f.level) for f in result.findings],
                         [("alias_unreachable", "note")] * 2)

    def test_the_table_keeps_one_canonical_door_and_an_alias_that_points_at_it(self):
        roles = {door.role: door for door in check.DOORS}
        self.assertEqual(set(roles), {"classroom", "alias"})
        self.assertEqual(roles["classroom"].host, CLASSROOM)
        self.assertEqual(roles["alias"].canonical, f"https://{CLASSROOM}")
        # Derived, not typed: a callback that names another host is the failure case above.
        for door in check.DOORS:
            self.assertEqual(door.callback, f"https://{door.host}/oauth2/callback")
            self.assertEqual(door.url, f"https://{door.host}/")


class ExitCodes(unittest.TestCase):
    def _run(self, probe, *args):
        original = check.fetch
        check.fetch = probe
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(list(args))
            return code, out.getvalue(), err.getvalue()
        finally:
            check.fetch = original

    def test_clean_run_exits_zero(self):
        code, out, _ = self._run(probe_for())
        self.assertEqual(code, 0)
        self.assertIn("0 failure(s)", out)

    def test_an_open_door_exits_one(self):
        code, out, _ = self._run(probe_for(root=(200, {}, b"", None)))
        self.assertEqual(code, 1)
        self.assertIn("door_open", out)

    def test_a_classroom_that_does_not_answer_is_exit_two(self):
        # Not a finding: this host cannot see the name, which is not an answer about the
        # gate. The same distinction `check-media-doors.py` makes.
        gone = (None, {}, b"", "transport")
        code, _, err = self._run(probe_for(root=gone, deep=gone))
        self.assertEqual(code, 2)
        self.assertIn("did not answer", err)

    def test_a_tls_failure_is_judged_rather_than_skipped(self):
        code, _, _ = self._run(probe_for(root=(None, {}, b"", "tls")))
        self.assertEqual(code, 1)

    def test_the_textfile_carries_the_shared_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "atheniq-doors.prom"
            code, _, _ = self._run(probe_for(root=(200, {}, b"", None)), "--prom", str(path))
            self.assertEqual(code, 1)
            body = path.read_text()
        self.assertIn('innotel_estate_check_last_status{check="atheniq_doors"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="atheniq_doors"} 1', body)
        self.assertNotIn("last_success_timestamp", body)

    def test_a_run_that_could_not_reach_the_name_publishes_no_failure_count(self):
        # Second 0 exit-code case, separately: the contract's exit-2 shape is "ran, but
        # nothing could be judged" — a failure count here would page on an unreachable
        # host as though the door were open.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "atheniq-doors.prom"
            gone = (None, {}, b"", "transport")
            code, _, _ = self._run(probe_for(root=gone, deep=gone), "--prom", str(path))
            self.assertEqual(code, 2)
            body = path.read_text()
        self.assertIn('innotel_estate_check_last_run_timestamp{check="atheniq_doors"}', body)
        self.assertNotIn("innotel_estate_check_failures", body)


if __name__ == "__main__":
    unittest.main()
