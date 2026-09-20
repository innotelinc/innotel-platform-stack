#!/usr/bin/env python3
"""Unit tests for check-group-compose-drift.py — the group-vs-repos comparator.

These cover the rules, not the estate: each case builds a throwaway tree with a
group directory and its member repos, runs the checker over it, and asserts what
it said. The cases are the shape of the drift this check was written after (see
the module docstring): a host rebuilt from a group file that is missing a member
repo's service, and a group file holding onto a service no repo declares any
more.

No Docker and no estate are needed, which is what lets this run in CI.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / "check-group-compose-drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_group_compose_drift", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before executing: the module uses dataclasses, and `@dataclass`
    # resolves its own module through sys.modules while the class body is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


chk = _load()


def group_file(services: tuple[str, ...] = ("app", "db"),
               extra: tuple[str, ...] = ()) -> str:
    """A group compose declaring these services, plus its own mesh registration."""
    names = (*services, *extra, "consul-reg-g9")
    body = "".join(f"  {name}:\n    image: example/{name}:latest\n" for name in names)
    return f"name: innotel-group9\nservices:\n{body}\nvolumes:\n  db-data:\n"


# The estate's group file is a pointer, not the compose: it includes the
# generated file one level up in the stack repo. `POINTER` is that shape.
POINTER = "name: innotel-group9\ninclude:\n  - ../groups/9-group.yml\n"


def repo(*services: str, gated: tuple[str, ...] = ()) -> str:
    """A member repo's compose; services named in `gated` sit behind a profile."""
    body = ""
    for name in services:
        body += f"  {name}:\n    image: example/{name}:latest\n"
        if name in gated:
            body += '    profiles: ["opt-in"]\n'
    return f"name: member\nservices:\n{body}"


class DriftCase(unittest.TestCase):
    """Build a tree, run the checker over it, hand back (exit code, output)."""

    def run_check(self, files: dict[str, str], *args: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(buffer):
                code = chk.main(["--root", str(root), *args])
            return code, buffer.getvalue()

    def report_for(self, files, group: str = "9-group"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            for group_dir in chk.find_groups(root):
                if group_dir.name == group:
                    return chk.compare(group_dir)
        self.fail(f"no group {group} in the tree")


class RuleTests(DriftCase):
    def test_a_matching_group_is_clean(self):
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(),
            "9-group/alpha/docker-compose.yml": repo("app"),
            "9-group/beta/docker-compose.yml": repo("db"),
        })
        self.assertEqual(code, 0, out)
        self.assertIn("ok: every group compose declares what its member repos run", out)

    def test_a_repo_service_the_group_lacks_is_a_violation(self):
        """The failure this check exists for: a host built from the group file
        comes up without the service the repo actually runs."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(),
            "9-group/alpha/docker-compose.yml": repo("app"),
            "9-group/beta/docker-compose.yml": repo("db", "worker"),
        })
        self.assertEqual(code, 1)
        self.assertIn("MISSING (1)", out)
        self.assertIn("worker", out)
        self.assertIn("beta", out)

    def test_a_group_service_no_repo_declares_is_a_violation(self):
        """The stale entry: 5-dev declared `chef` long after Atlas retired it."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(extra=("chef",)),
            "9-group/alpha/docker-compose.yml": repo("app"),
            "9-group/beta/docker-compose.yml": repo("db"),
        })
        self.assertEqual(code, 1)
        self.assertIn("EXTRA (1)", out)
        self.assertIn("chef", out)

    def test_the_groups_own_registration_is_not_drift(self):
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(),
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
        })
        self.assertEqual(code, 0, out)
        self.assertIn("consul-reg-g9", out)

    def test_a_prefixed_rename_is_reported_and_not_failed(self):
        """plutus's `backend` is `plutus-backend` in the group file; a rebuilt
        host is complete either way, so the pair must not fail the check."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(services=("app", "plutus-db")),
            "9-group/alpha/docker-compose.yml": repo("app"),
            "9-group/beta/docker-compose.yml": repo("db"),
        })
        self.assertEqual(code, 0, out)
        self.assertIn("renamed (1)", out)
        self.assertIn("plutus-db", out)

    def test_a_profile_gated_repo_service_is_opt_in_not_missing(self):
        """transmission/deluge/autobrr sit behind profiles in monarch's compose
        deliberately, so the group file is not required to declare them."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(),
            "9-group/alpha/docker-compose.yml": repo("app", "db", gated=("db",)),
            "9-group/beta/docker-compose.yml": repo("db"),
        })
        self.assertEqual(code, 0, out)
        self.assertIn("opt-in (profiles), not compared", out)

    def test_a_group_service_gated_in_a_repo_is_opt_in_on_both_sides(self):
        """cerulean declares `vault` under a profile and the 1-primary group
        declares it plainly: served either way, so not an extra."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(extra=("vault",)),
            "9-group/alpha/docker-compose.yml": repo("app", "vault", gated=("vault",)),
            "9-group/beta/docker-compose.yml": repo("db"),
        })
        self.assertEqual(code, 0, out)
        self.assertNotIn("EXTRA", out)

    def test_a_family_outside_every_repo_is_group_owned(self):
        """Tutor/Open edX runs from Tutor's own compose project, so `tutor-*`
        has no repo file to compare against (atheniq's compose says so)."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(extra=("tutor-lms",)),
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
        })
        self.assertEqual(code, 0, out)
        self.assertIn("tutor-lms", out)

    def test_anchors_are_not_services(self):
        group = "x-common: &common\n  restart: unless-stopped\n\n" + group_file()
        report = self.report_for({
            "9-group/docker-compose.yml": group,
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
        })
        self.assertNotIn("x-common", chk.parse_services(report.group_file).services)

    def test_a_repo_on_its_own_host_is_reported_and_not_compared(self):
        """`ontrak` sits under `5-dev/` because that is its group directory, but
        its range runs on a host of its own from its own compose, and the group-5
        host cannot open /dev/kvm — so declaring it here would start a second
        range. Neither `SOURCES` nor the group file lists it, and that is
        correct: the repo is named in `OWN_HOST_REPOS`, skipped, and printed."""
        code, out = self.run_check({
            "5-dev/docker-compose.yml": group_file(),
            "5-dev/alpha/docker-compose.yml": repo("app", "db"),
            "5-dev/ontrak/docker-compose.yml": repo("gateway", "guacd", "portal"),
        })
        self.assertEqual(code, 0, out)
        self.assertNotIn("MISSING", out)
        self.assertIn("on its own host (1)", out)
        self.assertIn("ontrak", out)

    def test_an_own_host_entry_the_group_now_deploys_fails(self):
        """The fail-open case the guard exists for: if the repo is added to the
        group's sources while the entry still exempts it, the exemption would
        skip a real member — so that is a violation, not a note (rule 9)."""
        group = ("# Sources:\n"
                 "#   5-dev/alpha/docker-compose.yml\n"
                 "#   5-dev/ontrak/docker-compose.yml\n" + group_file())
        code, out = self.run_check({
            "5-dev/docker-compose.yml": group,
            "5-dev/alpha/docker-compose.yml": repo("app", "db"),
            "5-dev/ontrak/docker-compose.yml": repo("gateway", "portal"),
        })
        self.assertEqual(code, 1)
        self.assertIn("OWN HOST (1)", out)
        self.assertIn("ontrak", out)

    def test_an_own_host_entry_with_no_repo_here_warns_and_does_not_fail(self):
        """The table is a claim, so a checkout that moved away must not leave
        the exemption silent — but nothing is being exempted, so it is a
        warning and the run still passes."""
        code, out = self.run_check({
            "5-dev/docker-compose.yml": group_file(),
            "5-dev/alpha/docker-compose.yml": repo("app", "db"),
        })
        self.assertEqual(code, 0, out)
        self.assertIn("warning:", out)
        self.assertIn("5-dev/ontrak", out)

    def test_the_exemption_is_the_named_repo_not_the_group(self):
        """A sibling in the same directory is still a member, so a repo the
        group genuinely forgets is still a finding."""
        code, out = self.run_check({
            "5-dev/docker-compose.yml": group_file(),
            "5-dev/alpha/docker-compose.yml": repo("app", "db"),
            "5-dev/ontrak/docker-compose.yml": repo("gateway", "portal"),
            "5-dev/omega/docker-compose.yml": repo("app", "db", "worker"),
        })
        self.assertEqual(code, 1)
        self.assertIn("MISSING (1)", out)
        self.assertIn("worker", out)

    def test_a_pointer_group_file_follows_its_include(self):
        """The estate's group files no longer declare the services at all — they
        include the generated compose in the stack repo, and a host deploys that.
        Following the include is what keeps the check meaningful."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": POINTER,
            "groups/9-group.yml": group_file(),
            "9-group/alpha/docker-compose.yml": repo("app"),
            "9-group/beta/docker-compose.yml": repo("db"),
        })
        self.assertEqual(code, 0, out)
        self.assertIn("via 1 include(s)", out)

    def test_a_service_missing_behind_a_pointer_is_still_a_violation(self):
        code, out = self.run_check({
            "9-group/docker-compose.yml": POINTER,
            "groups/9-group.yml": group_file(),
            "9-group/alpha/docker-compose.yml": repo("app"),
            "9-group/beta/docker-compose.yml": repo("db", "worker"),
        })
        self.assertEqual(code, 1)
        self.assertIn("MISSING (1)", out)
        self.assertIn("worker", out)

    def test_a_stale_name_behind_a_pointer_points_at_the_included_file(self):
        """The finding names the file that declares it, so a reader is sent to
        the generated compose rather than to the pointer that includes it."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": POINTER,
            "groups/9-group.yml": group_file(extra=("chef",)),
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
            "9-group/beta/docker-compose.yml": repo("app", "db"),
        })
        self.assertEqual(code, 1)
        self.assertIn("EXTRA (1)", out)
        self.assertIn("chef", out)
        report = self.report_for({
            "9-group/docker-compose.yml": POINTER,
            "groups/9-group.yml": group_file(extra=("chef",)),
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
        })
        where = [f.where for f in report.findings if f.service == "chef"]
        self.assertTrue(where and where[0].endswith("groups/9-group.yml"), where)

    def test_an_include_cycle_is_not_followed(self):
        """A wiring mistake must not turn the check into a hang."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": POINTER,
            "groups/9-group.yml": group_file() + "include:\n  - ../9-group/docker-compose.yml\n",
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
        })
        self.assertEqual(code, 0, out)

    def test_an_include_that_does_not_exist_is_not_a_crash(self):
        """`gen-group-compose.py` writes the include target; until it has, this
        check reports the pointer as declaring nothing rather than dying."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": POINTER,
            "9-group/alpha/docker-compose.yml": repo("app"),
        })
        self.assertEqual(code, 1)
        self.assertIn("MISSING (1)", out)

    def test_an_extra_overlay_names_its_services(self):
        """A repo's services are spread across files by design — npm keeps the
        edge in compose.cerulean.yml, zeus the full stack in
        docker-compose.full.yml — so every compose-shaped file counts."""
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(extra=("edge",)),
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
            "9-group/alpha/compose.edge.yml": "services:\n  edge:\n    image: example/edge:latest\n",
        })
        self.assertEqual(code, 0, out)


class InvocationTests(DriftCase):
    def test_a_group_filter_runs_one_group(self):
        code, out = self.run_check({
            "9-group/docker-compose.yml": group_file(),
            "9-group/alpha/docker-compose.yml": repo("app", "db"),
            "8-other/docker-compose.yml": group_file(),
            "8-other/alpha/docker-compose.yml": repo("app"),
        }, "--group", "8-other")
        self.assertEqual(code, 1)
        self.assertNotIn("9-group  (", out)

    def test_an_explicit_root_with_no_group_is_an_error(self):
        code, out = self.run_check({"repo/docker-compose.yml": repo("app")})
        self.assertEqual(code, 2)
        self.assertIn("no group compose under", out)

    def test_json_reports_the_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "9-group" / "alpha").mkdir(parents=True)
            (root / "9-group" / "docker-compose.yml").write_text(group_file(), encoding="utf-8")
            (root / "9-group" / "alpha" / "docker-compose.yml").write_text(
                repo("app", "db", "worker"), encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = chk.main(["--root", str(root), "--json"])
            payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["violations"], 1)
        self.assertEqual(payload["groups"][0]["group"], "9-group")
        self.assertEqual(payload["groups"][0]["findings"][0]["service"], "worker")

    def test_json_names_the_own_host_repos(self):
        """The machine-readable output carries the exemption too, so a dashboard
        can tell `not compared on purpose` from `not checked`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("alpha", "ontrak"):
                (root / "5-dev" / name).mkdir(parents=True)
            (root / "5-dev" / "docker-compose.yml").write_text(group_file(), encoding="utf-8")
            (root / "5-dev" / "alpha" / "docker-compose.yml").write_text(
                repo("app", "db"), encoding="utf-8")
            (root / "5-dev" / "ontrak" / "docker-compose.yml").write_text(
                repo("portal"), encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = chk.main(["--root", str(root), "--json"])
            payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["groups"][0]["own_host"],
                         {"ontrak": chk.OWN_HOST_REPOS["5-dev/ontrak"]})
        self.assertNotIn("ontrak", payload["groups"][0]["members"])

    def test_a_directory_that_is_not_a_group_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "loose").mkdir(parents=True)
            (root / "loose" / "docker-compose.yml").write_text(group_file(), encoding="utf-8")
            self.assertEqual(chk.find_groups(root), [])


if __name__ == "__main__":
    unittest.main()
