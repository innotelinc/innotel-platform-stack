#!/usr/bin/env python3
"""Unit tests for migrate-stack.py — the pack/restore portability tool.

These cover the parts that decide **what moves**: which services a compose file
declares, how a restore plans its ``up`` calls, how ``vault://`` references are
read, and that the streaming pack paths stream and fail loudly.

Docker is faked at the module boundary (``mig.docker``), so the suite runs
anywhere — including CI, where no daemon exists. The module under test has a
hyphen in its filename, so it is loaded by path rather than by import.
"""
from __future__ import annotations

import gzip
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

MIGRATE = Path(__file__).resolve().parents[1] / "migrate-stack.py"


def _load():
    spec = importlib.util.spec_from_file_location("migrate_stack", MIGRATE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mig = _load()


def fake_docker(stdout: str = "", returncode: int = 0, recorder: dict | None = None):
    """A stand-in for the module's ``docker()`` that records its argv."""

    def _docker(*args, check=True, input=None):  # noqa: A002 - mirrors the real signature
        if recorder is not None:
            recorder["args"] = list(args)
        return subprocess.CompletedProcess(args=list(args), returncode=returncode,
                                           stdout=stdout.encode(), stderr=b"")

    return _docker


class MigrateModuleTest(unittest.TestCase):
    """Restores whatever a test monkeypatches on the module under test."""

    _PATCHED = ("docker", "compose_declared_services")

    def setUp(self):
        self._saved = {name: getattr(mig, name) for name in self._PATCHED}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(mig, name, value)


# ── compose_declared_services ─────────────────────────────────────────────────

class ComposeDeclaredServices(MigrateModuleTest):
    def test_services_are_parsed_sorted_and_deduped(self):
        mig.docker = fake_docker("jellyfin\nsonarr\nradarr\nsonarr\n")
        self.assertEqual(mig.compose_declared_services(["/x/docker-compose.yml"]),
                         ["jellyfin", "radarr", "sonarr"])

    def test_a_compose_file_that_will_not_render_declares_nothing(self):
        # An unreadable overlay must not invent services — the caller falls back
        # to the containers it actually saw.
        mig.docker = fake_docker("jellyfin\n", returncode=1)
        self.assertEqual(mig.compose_declared_services(["/x/docker-compose.yml"]), [])

    def test_every_file_in_the_list_is_passed_through(self):
        # An overlay is only meaningful with the file it overlays: dropping
        # either would resolve a different service set than the deployment used.
        rec: dict = {}
        mig.docker = fake_docker("studio\n", recorder=rec)
        mig.compose_declared_services(["/a/docker-compose.yml", "/a/compose.gateway-sso.yml"])
        self.assertEqual(rec["args"], ["compose",
                                       "-f", "/a/docker-compose.yml",
                                       "-f", "/a/compose.gateway-sso.yml",
                                       "config", "--services"])


# ── declared_services ────────────────────────────────────────────────────────

class DeclaredServices(MigrateModuleTest):
    def test_one_entry_per_distinct_compose_invocation(self):
        records = [
            {"compose_dir": "/r/2-voice/zeus", "service": "pbx",
             "compose_files": ["/r/2-voice/zeus/docker-compose.yml"]},
            {"compose_dir": "/r/2-voice/zeus", "service": "portal",
             "compose_files": ["/r/2-voice/zeus/docker-compose.yml"]},
        ]
        mig.compose_declared_services = lambda files: ["pbx", "portal", "autoheal"]
        self.assertEqual(mig.declared_services(records), [{
            "compose_dir": "/r/2-voice/zeus",
            "compose_files": ["/r/2-voice/zeus/docker-compose.yml"],
            "services": ["pbx", "portal", "autoheal"],
        }])

    def test_an_overlay_gets_its_own_entry(self):
        records = [
            {"compose_dir": "/r/o", "service": "studio",
             "compose_files": ["/r/o/docker-compose.yml"]},
            {"compose_dir": "/r/o", "service": "gateway-sso",
             "compose_files": ["/r/o/docker-compose.yml", "/r/o/compose.gateway-sso.yml"]},
        ]
        mig.compose_declared_services = lambda files: sorted(files)
        out = mig.declared_services(records)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["services"], sorted(records[1]["compose_files"]))

    def test_a_record_without_a_compose_file_list_is_skipped(self):
        records = [{"compose_dir": "/r/x", "service": "s", "compose_files": []}]
        self.assertEqual(mig.declared_services(records), [])

    def test_a_file_that_declares_nothing_produces_no_entry(self):
        records = [{"compose_dir": "/r/x", "service": "s",
                    "compose_files": ["/r/x/docker-compose.yml"]}]
        mig.compose_declared_services = lambda files: []
        self.assertEqual(mig.declared_services(records), [])


# ── build_restore_plans ──────────────────────────────────────────────────────

def _rel(p: str) -> str:
    return p.removeprefix("/r/")


class BuildRestorePlans(unittest.TestCase):
    def test_container_services_come_back_in_recorded_order(self):
        bundle = {"containers": [
            {"service": "jellyfin", "compose_dir": "/r/3-media/monarch",
             "compose_files": ["/r/3-media/monarch/docker-compose.yml"]},
            {"service": "sonarr", "compose_dir": "/r/3-media/monarch",
             "compose_files": ["/r/3-media/monarch/docker-compose.yml"]},
        ]}
        plans, packed = mig.build_restore_plans(bundle, _rel)
        self.assertEqual(plans, {
            ("3-media/monarch", ("3-media/monarch/docker-compose.yml",)):
                ["jellyfin", "sonarr"],
        })
        self.assertEqual(packed, {"jellyfin", "sonarr"})

    def test_a_declared_service_with_no_container_is_added(self):
        # The monarch bundle had no container for monarch-recs / monarch-health /
        # monarch-seed / … . Replaying containers alone left them behind, and
        # nothing reported it: no container, so no error.
        bundle = {
            "containers": [{"service": "jellyfin", "compose_dir": "/r/m",
                            "compose_files": ["/r/m/docker-compose.yml"]}],
            "service_sets": [{"compose_dir": "/r/m",
                              "compose_files": ["/r/m/docker-compose.yml"],
                              "services": ["jellyfin", "monarch-recs", "monarch-health"]}],
        }
        plans, packed = mig.build_restore_plans(bundle, _rel)
        self.assertEqual(plans[("m", ("m/docker-compose.yml",))],
                         ["jellyfin", "monarch-recs", "monarch-health"])
        self.assertEqual(packed, {"jellyfin"})

    def test_an_excluded_service_is_not_started_even_if_declared(self):
        # omniroute stays on the source host: every other stack dials it by
        # address, so moving it would silently break them.
        bundle = {
            "excluded_services": ["omniroute"],
            "containers": [{"service": "omniroute", "compose_dir": "/r/v",
                            "compose_files": ["/r/v/docker-compose.yml"]}],
            "service_sets": [{"compose_dir": "/r/v",
                              "compose_files": ["/r/v/docker-compose.yml"],
                              "services": ["omniroute", "pbx"]}],
        }
        plans, _ = mig.build_restore_plans(bundle, _rel)
        self.assertEqual(plans, {("v", ("v/docker-compose.yml",)): ["pbx"]})

    def test_a_plan_whose_every_service_is_excluded_is_dropped(self):
        bundle = {
            "excluded_services": ["omniroute"],
            "containers": [{"service": "omniroute", "compose_dir": "/r/v",
                            "compose_files": ["/r/v/docker-compose.yml"]}],
        }
        plans, _ = mig.build_restore_plans(bundle, _rel)
        self.assertEqual(plans, {("v", ("v/docker-compose.yml",)): []})

    def test_a_second_compose_file_gets_its_own_plan(self):
        bundle = {
            "containers": [{"service": "studio", "compose_dir": "/r/o",
                            "compose_files": ["/r/o/docker-compose.yml"]}],
            "service_sets": [{"compose_dir": "/r/o",
                              "compose_files": ["/r/o/docker-compose.yml",
                                                "/r/o/compose.gateway-sso.yml"],
                              "services": ["studio", "gateway-sso"]}],
        }
        plans, _ = mig.build_restore_plans(bundle, _rel)
        # Two keys: the base file alone (studio) and the overlay pair
        # (studio + gateway-sso). Each keeps the `-f` list it was created with.
        self.assertEqual(sorted(plans), [
            ("o", ("o/docker-compose.yml",)),
            ("o", ("o/docker-compose.yml", "o/compose.gateway-sso.yml")),
        ])

    def test_a_bundle_without_service_sets_still_plans_from_containers(self):
        # A bundle packed before the declared set was recorded must still
        # restore — no upgrade step, no crash.
        bundle = {"containers": [{"service": "gitea", "compose_dir": "/r/a",
                                  "compose_files": ["/r/a/docker-compose.yml"]}]}
        plans, _ = mig.build_restore_plans(bundle, _rel)
        self.assertEqual(plans, {("a", ("a/docker-compose.yml",)): ["gitea"]})

    def test_a_service_set_without_files_is_ignored(self):
        bundle = {"containers": [],
                  "service_sets": [{"compose_dir": "/r/a", "compose_files": [],
                                    "services": ["ghost"]}]}
        plans, _ = mig.build_restore_plans(bundle, _rel)
        self.assertEqual(plans, {})

    def test_a_container_without_a_service_or_dir_is_ignored(self):
        bundle = {"containers": [
            {"service": "", "compose_dir": "/r/a",
             "compose_files": ["/r/a/docker-compose.yml"]},
            {"service": "x", "compose_dir": "", "compose_files": []},
        ]}
        plans, packed = mig.build_restore_plans(bundle, lambda p: p)
        self.assertEqual(plans, {})
        self.assertEqual(packed, {"", "x"})


# ── vault references ─────────────────────────────────────────────────────────

class VaultRefs(unittest.TestCase):
    def test_a_reference_is_split_into_mount_path_key(self):
        self.assertEqual(mig.parse_vault_ref("vault://cerulean/distro#TOKEN"),
                         ("cerulean", "distro", "TOKEN"))

    def test_a_malformed_reference_is_refused(self):
        for bad in ("vault://cerulean/distro", "vault://cerulean#K",
                    "vault:///path#K", "vault://mount/path"):
            with self.assertRaises(ValueError, msg=bad):
                mig.parse_vault_ref(bad)

    def test_only_references_are_collected_and_quotes_are_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(
                "# VAULT_TOKEN=vault://cerulean/x#NO\n"
                "VAULT_ADDR=http://vault:8200\n"
                'PG_PASSWORD="vault://cerulean/atlas#PG"\n'
                "PLAIN=not-a-reference\n"
            )
            self.assertEqual(mig.vault_refs(env),
                             {"PG_PASSWORD": "vault://cerulean/atlas#PG"})


# ── stream_to_file ───────────────────────────────────────────────────────────

class StreamToFile(unittest.TestCase):
    def test_output_is_written_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "payload.tar"
            mig.stream_to_file(["sh", "-c", "head -c 1048576 /dev/zero"], out, compress=False)
            self.assertEqual(out.stat().st_size, 1048576)

    def test_a_compressed_payload_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "images.tar.gz"
            mig.stream_to_file(["sh", "-c", "printf 'layer-data'"], out, compress=True)
            with gzip.open(out, "rb") as fh:
                self.assertEqual(fh.read(), b"layer-data")

    def test_a_failed_command_raises_instead_of_leaving_a_truncated_file(self):
        # A swallowed failure here is a bundle that restores short — the whole
        # reason pack streams the output rather than buffering it.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "vol.tar"
            with self.assertRaises(SystemExit) as caught:
                mig.stream_to_file(["sh", "-c", "printf partial; echo boom >&2; exit 3"], out,
                                   compress=False)
            self.assertIn("boom", str(caught.exception))

    def test_a_chatty_command_does_not_deadlock(self):
        # stderr goes to a temp file, not a pipe: more stderr than a pipe buffer
        # holds must not block the writer while we are blocked reading stdout.
        # This command writes ~200 KiB to stderr and would hang on a pipe.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "vol.tar"
            mig.stream_to_file(
                ["sh", "-c", "printf data; head -c 200000 /dev/zero | tr '\\0' 'e' >&2"],
                out, compress=False)
            self.assertEqual(out.read_bytes(), b"data")


if __name__ == "__main__":
    unittest.main(verbosity=2)
