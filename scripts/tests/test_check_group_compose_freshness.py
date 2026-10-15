#!/usr/bin/env python3
"""Unit tests for check-group-compose-freshness.py — what counts as behind, and what does not.

The check exists because a tracked generated file can fall behind the repos it was built
from, and the first symptom is a rebuild that starts the wrong set of services. What has
to hold is narrow and worth pinning: a group whose file differs from its regeneration is a
*failure* (not a note), the finding names the group and the services that moved, a group
with no checked-in file is a failure rather than a skip, and being unable to compare is
never a pass.

The comparison is exercised against a stub generator, so the test states the rule rather
than re-running the real generator over the real estate; `gen-group-compose.py`'s own
behaviour is covered by `test_gen_group_compose.py`.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-group-compose-freshness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_group_compose_freshness", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


freshness = _load()


def compose(*services: str, extra: str = "") -> str:
    """A compose as the generator emits one: a `services:` tree then the other keys."""
    body = ["name: innotel-groupN", "", "services:"]
    for name in services:
        body += [f"  {name}:", "    image: example/" + name]
    body += [extra, "volumes:", "  data:", "networks:", "  mesh:", ""]
    return "\n".join(body)


class StubGenerator:
    """The half of `gen-group-compose.py` this check uses: `SOURCES`, `generate`, `output_path`."""

    def __init__(self, sources: dict[str, str], groups_dir: Path, generated: dict[str, str]) -> None:
        self.SOURCES = {group: {} for group in sources}
        self._paths = {group: groups_dir / f"{group}.yml" for group in sources}
        self._generated = generated

    def output_path(self, group: str) -> Path:
        return self._paths[group]

    def generate(self, group: str, root: Path):  # noqa: ARG002 — root is the generator's business
        return self._generated[group], [], []


class ServiceNamesCase(unittest.TestCase):
    def test_service_blocks_are_read_from_the_services_tree(self):
        text = compose("jellyfin", "homarr") + "  x-not-a-service: &anchor\n"
        self.assertEqual(freshness.service_names(text), ["jellyfin", "homarr"])

    def test_an_anchor_is_not_a_service(self):
        # `x-common: &common` sits at column 0 in the hand-written files; a top-level key
        # of any indent must not be counted as a service.
        text = "x-common: &common\n  restart: unless-stopped\nservices:\n  proxy:\n    image: x\n"
        self.assertEqual(freshness.service_names(text), ["proxy"])

    def test_volumes_and_networks_are_not_services(self):
        text = compose("proxy", extra="  # a comment: with a key shape")
        self.assertIn("proxy", freshness.service_names(text))
        self.assertNotIn("data", freshness.service_names(text))


class AuditCase(unittest.TestCase):
    def _states(self, tracked: dict[str, str], generated: dict[str, str], groups_dir: Path):
        stub = StubGenerator(tracked, groups_dir, generated)
        for group, text in tracked.items():
            if text is not None:
                (groups_dir / f"{group}.yml").write_text(text, encoding="utf-8")
        return freshness.read_states(stub, groups_dir)

    def test_a_file_that_matches_its_regeneration_is_not_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = Path(tmp)
            same = compose("jellyfin", "homarr")
            states = self._states({"3-media": same}, {"3-media": same}, groups)
            self.assertEqual(freshness.audit(states).findings, [])
            self.assertTrue(freshness.audit(states).ok)

    def test_a_service_the_repos_added_is_a_failure_that_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = Path(tmp)
            states = self._states(
                {"3-media": compose("jellyfin")},
                {"3-media": compose("jellyfin", "homarr")},
                groups,
            )
            result = freshness.audit(states)
            self.assertFalse(result.ok, "a file the repos have moved past is the whole point")
            self.assertEqual([finding.code for finding in result.findings], ["behind"])
            self.assertEqual(result.findings[0].level, "fail")
            self.assertIn("3-media", result.findings[0].message)
            self.assertIn("homarr", result.findings[0].message)

    def test_a_service_the_repos_retired_is_a_failure_that_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = Path(tmp)
            states = self._states(
                {"2-voice": compose("kokoro", "piper")},
                {"2-voice": compose("piper")},
                groups,
            )
            finding = freshness.audit(states).findings[0]
            self.assertIn("retire", finding.message)
            self.assertIn("kokoro", finding.message)

    def test_a_group_with_no_checked_in_file_is_a_failure_not_a_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = Path(tmp)
            states = self._states({"5-dev": None}, {"5-dev": compose("oasis-mail")}, groups)
            finding = freshness.audit(states).findings[0]
            self.assertEqual(finding.code, "missing")
            self.assertEqual(finding.level, "fail")
            self.assertIn("no checked-in 5-dev.yml", finding.message)

    def test_a_whitespace_only_difference_is_still_behind(self):
        # The generated file is compared byte-for-byte, the way `--check` compares it: a
        # diff nobody regenerated is a diff a rebuild reads.
        with tempfile.TemporaryDirectory() as tmp:
            groups = Path(tmp)
            states = self._states(
                {"4-social": compose("mastodon")},
                {"4-social": compose("mastodon") + "\n"},
                groups,
            )
            self.assertEqual([f.code for f in freshness.audit(states).findings], ["behind"])

    def test_a_body_only_change_says_no_service_name_moved(self):
        # The common regeneration: a profile, an image tag or an env default changes and
        # the service set does not. The finding says so, rather than listing nothing.
        with tempfile.TemporaryDirectory() as tmp:
            groups = Path(tmp)
            states = self._states(
                {"2-voice": compose("piper").replace("image: example/piper", "image: example/piper:2")},
                {"2-voice": compose("piper")},
                groups,
            )
            finding = freshness.audit(states).findings[0]
            self.assertIn("every service name matches", finding.message)
            self.assertNotIn("the repos add", finding.message)


class SeriesCase(unittest.TestCase):
    def test_it_publishes_a_total_and_one_series_per_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = Path(tmp)
            same = compose("proxy")
            stub = StubGenerator({"1-primary": same, "2-voice": same}, groups, {"1-primary": same, "2-voice": compose("piper")})
            (groups / "1-primary.yml").write_text(same, encoding="utf-8")
            (groups / "2-voice.yml").write_text(same, encoding="utf-8")
            states = freshness.read_states(stub, groups)
            series = freshness.freshness_series(states)
            self.assertIn('innotel_estate_check_group_compose_stale_groups{check="group_compose_freshness"} 1', series)
            self.assertIn('group="1-primary"} 0', series)
            self.assertIn('group="2-voice"} 1', series)


class CannotRunCase(unittest.TestCase):
    def test_a_missing_fetch_script_cannot_run_rather_than_passing(self):
        # Both sides come from the remote, so a checkout that is not the stack repo is a
        # could-not-run (exit 2), never an empty comparison that reads as clean.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(freshness.CannotRun):
                freshness.fetch_members(Path(tmp), Path(tmp) / "estate")

    def test_a_directory_without_the_generator_cannot_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(freshness.CannotRun):
                freshness.load_generator(Path(tmp))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
