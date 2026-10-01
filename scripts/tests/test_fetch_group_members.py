#!/usr/bin/env python3
"""Unit tests for fetch-group-members.py — the CI estate hydrator.

The script is what makes `gen-group-compose.py --check` able to fail in CI: it
clones each group's member repos into a scratch root so the check has an estate to
read. These cover the parts worth pinning — that the repo list comes from
`SOURCES` (so the two cannot drift), that the URL is built from the configurable
base, and that a real clone lands in the group layout the generator expects.

    python3 -m unittest discover -s scripts/tests -t scripts/tests
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FETCH = Path(__file__).resolve().parents[1] / "fetch-group-members.py"


def _load():
    spec = importlib.util.spec_from_file_location("fetch_group_members", FETCH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch = _load()


class SourceTests(unittest.TestCase):
    """The repo list is the generator's, not a second one."""

    def test_members_come_from_the_generators_sources(self):
        sources = {"9-test": {"widget": ["docker-compose.yml"], "gadget": ["x.yml"]},
                   "8-other": {"sprocket": ["y.yml"]}}
        self.assertEqual(fetch.members(sources), [
            ("8-other", "sprocket"), ("9-test", "gadget"), ("9-test", "widget")])

    def test_a_repo_added_to_sources_is_fetched_without_editing_this(self):
        actual = {repo for _, repo in fetch.members(fetch._load_sources())}
        self.assertIn("genesis", actual)          # in SOURCES["1-primary"]
        self.assertIn("zeus", actual)
        self.assertNotIn("ips", actual)


class UrlTests(unittest.TestCase):
    """The base is configurable so a host with no egress can point at a mirror."""

    def test_the_default_base_is_the_org_on_github(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fetch.clone_url("genesis"),
                             "https://github.com/innotelinc/genesis.git")

    def test_the_org_and_base_are_overridable(self):
        with mock.patch.dict(os.environ, {"INNOTEL_ORG": "example"}, clear=True):
            self.assertTrue(fetch.clone_url("x").startswith("https://github.com/example/"))
        with mock.patch.dict(os.environ, {"INNOTEL_GIT_BASE": "https://git.internal/"},
                             clear=True):
            self.assertEqual(fetch.clone_url("x"), "https://git.internal/x.git")


class CloneTests(unittest.TestCase):
    """A clone lands at `<root>/<group>/<repo>`, where the generator reads."""

    def _remote(self, root: Path, repo: str) -> None:
        origin = root / "remotes" / f"{repo}.git"
        origin.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet", str(origin)], check=True)
        (origin / "docker-compose.yml").write_text("services:\n  x:\n    image: x:1\n")
        run = ["git", "-C", str(origin), "add", "docker-compose.yml"]
        subprocess.run(run, check=True)
        subprocess.run(
            ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "--quiet", "-m", "x"], check=True)

    def test_a_clone_lands_in_the_group_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._remote(tmp, "widget")
            target = tmp / "estate"
            saved = fetch._load_sources
            fetch._load_sources = lambda: {"9-test": {"widget": ["docker-compose.yml"]}}
            try:
                with mock.patch.dict(
                        os.environ,
                        {"INNOTEL_GIT_BASE": str(tmp / "remotes")}, clear=True):
                    with mock.patch.object(sys, "argv",
                                           ["fetch-group-members.py", str(target)]):
                        code = fetch.main()
            finally:
                fetch._load_sources = saved
            self.assertEqual(code, 0)
            self.assertTrue((target / "9-test" / "widget" / "docker-compose.yml").is_file())

    def test_a_repo_the_sources_do_not_name_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    sys, "argv",
                    ["fetch-group-members.py", tmp, "--repo", "not-a-repo"]):
                self.assertEqual(fetch.main(), 2)


if __name__ == "__main__":
    unittest.main()
