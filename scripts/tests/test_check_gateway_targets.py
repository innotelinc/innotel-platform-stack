#!/usr/bin/env python3
"""Unit tests for check-gateway-targets.py — the stale-door scanner.

These cover the rules, not the estate: each case builds a throwaway tree, scans it,
and asserts what the scanner said. The cases are the failures this check was written
after, so a regression in any of them is a regression in the check's reason to exist
(see the module docstring for the five consumers that were still dialling the
gateway's own port when it stopped being bound on the LAN).

Docker is faked where ``--live`` is exercised, so the suite runs anywhere, including
CI. The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CHECK = Path(__file__).resolve().parents[1] / "check-gateway-targets.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_gateway_targets", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before executing: the module uses dataclasses, and `@dataclass`
    # resolves its own module through sys.modules while the class body is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


chk = _load()


class ScanCase(unittest.TestCase):
    """Build a tree, scan it, hand back the findings."""

    def scan(self, files: dict[str, str]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            findings = []
            for path in chk.walk(root):
                findings.extend(chk.scan_file(path, root))
            return findings, chk.walk(root)

    def violations(self, files):
        return [f for f in self.scan(files)[0] if f.is_violation]

    def exempt(self, files):
        return [f for f in self.scan(files)[0] if f.exempt]

    def conditional(self, files):
        return [f for f in self.scan(files)[0] if f.is_conditional]


class RuleTests(ScanCase):
    def test_a_lan_address_on_the_gateway_port_is_a_violation(self):
        found = self.violations({"app/.env": "OMNIROUTE_BASE_URL=http://192.168.1.46:20128/v1\n"})
        self.assertEqual(len(found), 1)
        self.assertIn("192.168.1.46:20128", found[0].text)
        self.assertIn("20129", found[0].reason)

    def test_the_proxy_port_is_what_it_asks_for(self):
        self.assertEqual(self.violations({"app/.env": "OMNIROUTE_BASE_URL=http://192.168.1.46:20129/v1\n"}), [])

    def test_loopback_is_the_gateway_s_actual_binding(self):
        # A host-mode process on the gateway's host dials this, and the gateway's own
        # compose declares it. Flagging it would make the check unusable.
        for value in ("http://127.0.0.1:20128", "http://localhost:20128/v1", "http://0.0.0.0:20128"):
            with self.subTest(value=value):
                self.assertEqual(self.violations({"gateway/.env": f"GATEWAY_SSO_UPSTREAM={value}\n"}), [])

    def test_the_docker_alias_is_a_violation_outside_the_gateway_s_own_compose(self):
        # This is n8n and dashboard-api on `.30`: the alias resolved to their own
        # docker0, so it had been broken silently since the host split.
        found = self.violations({"other/docker-compose.yml": "      N8N_MODEL_URL: http://host.docker.internal:20128/v1\n"})
        self.assertEqual(len(found), 1)
        self.assertIn("dialer's own docker0", found[0].reason)

    def test_the_docker_alias_is_allowed_in_the_file_that_declares_the_gateway(self):
        compose = (
            "services:\n"
            "  omniroute:\n"
            "    container_name: omniroute\n"
            "    environment:\n"
            "      SELF: http://host.docker.internal:20128\n"
        )
        self.assertEqual(self.violations({"2-voice/capstone/docker-compose.yml": compose}), [])

    def test_a_compose_service_name_from_another_project_is_a_violation(self):
        # This is rizz-api: `omniroute` is a service on capstone's network, so it never
        # resolved for a container on rizzaura's.
        found = self.violations({"4-social/rizzaura/.env": "AI_BASE_URL=http://omniroute:20128/v1\n"})
        self.assertEqual(len(found), 1)
        self.assertIn("compose service name", found[0].reason)

    def test_a_service_name_the_file_declares_is_fine(self):
        compose = "services:\n  omniroute:\n    ports:\n      - \"127.0.0.1:20128:20128\"\n    environment:\n      URL: http://omniroute:20128\n"
        self.assertEqual(self.violations({"2-voice/capstone/docker-compose.yml": compose}), [])

    def test_a_container_name_the_file_declares_is_fine_too(self):
        # `2-voice/docker-compose.yml` names it `g2-omniroute` via container_name and
        # its own services dial that; compose resolves it on the project's network.
        compose = (
            "services:\n"
            "  omniroute:\n"
            "    container_name: g2-omniroute\n"
            "  n8n:\n"
            "    environment:\n"
            "      OMNIROUTE_URL: http://g2-omniroute:20128\n"
        )
        self.assertEqual(self.violations({"2-voice/docker-compose.yml": compose}), [])

    def test_the_same_name_from_another_project_is_still_a_violation(self):
        compose = "services:\n  api:\n    environment:\n      URL: http://g2-omniroute:20128\n"
        found = self.violations({"4-social/other/docker-compose.yml": compose})
        self.assertEqual(len(found), 1)
        self.assertIn("another", found[0].reason + "another project")


class ConditionalTests(ScanCase):
    """The two targets this can place in one deployment and not in another.

    Both were live defects that this check passed over in silence — found by hand,
    which is the whole argument for reporting them instead.
    """

    def test_loopback_is_conditional_never_a_violation(self):
        # olympus's SSO proxy dials the gateway's loopback and is right to: it runs
        # on the gateway's host. The same file's `host.docker.internal` value would
        # be right there too — and `.46`'s shell export of this very value reached
        # a container, where localhost is the container.
        found = self.conditional({"gateway/.env": "GATEWAY_SSO_UPSTREAM=http://127.0.0.1:20128\n"})
        self.assertEqual(len(found), 1)
        self.assertIn("host-mode process on the gateway's host", found[0].note)
        self.assertIn("20129", found[0].note)
        self.assertEqual(self.violations({"gateway/.env": "GATEWAY_SSO_UPSTREAM=http://127.0.0.1:20128\n"}), [])

    def test_the_declaring_file_s_alias_is_conditional(self):
        # capstone's compose declares the gateway, so the alias is legitimate — on
        # `.46`. The same file runs on `.30`, where both of its targets were dead.
        compose = (
            "services:\n"
            "  omniroute:\n"
            "    container_name: omniroute\n"
            "  n8n:\n"
            "    environment:\n"
            "      N8N_MODEL_URL: http://host.docker.internal:20128/v1\n"
        )
        found = self.conditional({"2-voice/capstone/docker-compose.yml": compose})
        self.assertEqual(len(found), 1)
        self.assertIn("on any other host", found[0].note)
        self.assertEqual(self.violations({"2-voice/capstone/docker-compose.yml": compose}), [])

    def test_a_conditional_target_does_not_fail_the_scan(self):
        files = {"gateway/.env": "GATEWAY_SSO_UPSTREAM=http://127.0.0.1:20128\n"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            self.assertEqual(chk.main(["--root", str(root)]), 0)

    def test_a_sanctioned_target_is_not_even_conditional(self):
        self.assertEqual(self.conditional({"app/.env": "X=http://192.168.1.46:20129/v1\n"}), [])

    def test_a_service_name_the_file_declares_is_not_conditional_either(self):
        # Same project, same network: unambiguous, so nothing to report.
        compose = "services:\n  omniroute:\n    environment:\n      URL: http://omniroute:20128\n"
        self.assertEqual(self.conditional({"2-voice/capstone/docker-compose.yml": compose}), [])


class DeclaredNamesTests(ScanCase):
    def declared(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docker-compose.yml"
            path.write_text(content, encoding="utf-8")
            return chk.declared_names(path)

    def test_service_keys_and_container_names_are_both_addresses(self):
        compose = "services:\n  omniroute:\n    container_name: g2-omniroute\n  n8n:\n    image: n8nio/n8n\nvolumes:\n  omniroute-data:\n"
        self.assertEqual(self.declared(compose), {"omniroute", "n8n", "g2-omniroute"})

    def test_a_comment_inside_the_services_block_does_not_end_it(self):
        compose = "services:\n  n8n:\n  # keep this one\n  grist:\nvolumes:\n"
        self.assertEqual(self.declared(compose), {"n8n", "grist"})

    def test_a_top_level_key_ends_the_services_block(self):
        compose = "services:\n  n8n:\nvolumes:\n  data:\n  also-not-a-service:\n"
        self.assertEqual(self.declared(compose), {"n8n"})

    def test_nothing_is_declared_in_a_non_compose_file(self):
        self.assertEqual(self.declared("OMNIROUTE_URL=http://192.168.1.46:20128\n"), set())


class ExclusionTests(ScanCase):
    def test_port_bindings_are_not_targets(self):
        # Where a port is listened on is not where a service is dialled, and the
        # gateway's own compose publishes exactly these.
        lines = [
            "    ports:\n      - \"127.0.0.1:20128:20128\"\n",
            "    ports:\n      - \"172.17.0.1:20128:20128\"\n",
            "    ports:\n      - \"${OMNIROUTE_PORT:-20128}:20128\"\n",
            "    ports:\n      - \"192.168.1.46:20128:20128\"      # a binding, however wide\n",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(self.violations({"gateway/docker-compose.yml": line}), [])

    def test_comments_are_not_configuration(self):
        self.assertEqual(self.violations({"app/.env": "# was http://192.168.1.46:20128/v1 before the split\n"}), [])

    def test_the_gateway_s_port_used_as_a_value_is_not_a_target(self):
        # `OMNIROUTE_PORT=20128` (ips's own extension) has no host, so nothing is dialled.
        self.assertEqual(self.violations({"ips/.env.example": "OMNIROUTE_PORT=20128\n"}), [])

    def test_other_ports_are_not_this_check_s_business(self):
        self.assertEqual(self.violations({"app/.env": "REDIS_URL=redis://192.168.1.46:16380/0\n"},), [])

    def test_only_config_shaped_files_are_scanned(self):
        _, scanned = self.scan({"app/notes.md": "http://192.168.1.46:20128/v1\n",
                                "app/.env": "X=1\n",
                                "app/docker-compose.yml": "services: {}\n"})
        names = sorted(p.name for p in scanned)
        self.assertEqual(names, [".env", "docker-compose.yml"])

    def test_noise_directories_are_skipped(self):
        _, scanned = self.scan({"app/node_modules/dep/.env": "X=http://192.168.1.46:20128\n",
                                "app/.env": "X=1\n"})
        self.assertEqual([p.name for p in scanned], [".env"])


class ExemptionTests(ScanCase):
    def test_an_exempt_path_is_reported_not_silently_passed(self):
        # The distro control plane runs on the gateway's host and logs in with the
        # management password, so its alias really is the right address.
        found = self.exempt({"5-dev/distro/.env": "GATEWAY_API_URL=http://host.docker.internal:20128\n"})
        self.assertEqual(len(found), 1)
        self.assertIn("runs ON the gateway's host", found[0].exempt)
        self.assertEqual(self.violations({"5-dev/distro/.env": "GATEWAY_API_URL=http://host.docker.internal:20128\n"}), [])

    def test_the_exemption_is_path_specific(self):
        self.assertEqual(len(self.violations({"5-dev/distro/other.env": "GATEWAY_API_URL=http://host.docker.internal:20128\n"})), 1)

    def test_every_exemption_carries_a_reason(self):
        for exemption in chk.EXEMPTIONS:
            with self.subTest(suffix=exemption.suffix):
                self.assertGreater(len(exemption.why), 40)


class LiveCheckTests(unittest.TestCase):
    """`--live` proves the assumption the scan rests on, rather than replacing it."""

    def live(self, ports, bridge="172.17.0.1", returncode=0):
        payload = json.dumps(ports)
        with mock.patch.object(chk.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=returncode, stdout=payload)
            if bridge == "":
                return chk.live_check("omniroute")
            with mock.patch.object(chk, "_bridge_gateway", return_value=bridge):
                return chk.live_check("omniroute")

    def test_loopback_and_the_local_bridge_are_the_assumption_holding(self):
        ok, note = self.live({"20128/tcp": [{"HostIp": "127.0.0.1"}, {"HostIp": "172.17.0.1"}]})
        self.assertTrue(ok)
        self.assertIn("this host only", note)

    def test_a_lan_binding_fails_because_the_premise_is_now_false(self):
        ok, note = self.live({"20128/tcp": [{"HostIp": "127.0.0.1"}, {"HostIp": "192.168.1.46"}]})
        self.assertFalse(ok)
        self.assertIn("on the LAN again", note)
        self.assertIn("192.168.1.46", note)

    def test_every_interface_is_caught(self):
        ok, note = self.live({"20128/tcp": [{"HostIp": ""}]})
        self.assertFalse(ok)
        self.assertIn("all interfaces", note)

    def test_a_missing_container_skips_rather_than_passing_silently(self):
        ok, note = self.live({}, returncode=1)
        self.assertTrue(ok)
        self.assertIn("skipped", note)


class CliTests(unittest.TestCase):
    def run_cli(self, files, *args):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return chk.main(["--root", str(root), *args])

    def test_clean_tree_exits_zero(self):
        self.assertEqual(self.run_cli({"app/.env": "X=http://192.168.1.46:20129/v1\n"}), 0)

    def test_a_violation_exits_one(self):
        self.assertEqual(self.run_cli({"app/.env": "X=http://192.168.1.46:20128/v1\n"}), 1)

    def test_json_output_names_all_three_lists(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = self.run_cli({"5-dev/distro/.env": "GATEWAY_API_URL=http://host.docker.internal:20128\n",
                                 "app/.env": "X=http://192.168.1.46:20128\n",
                                 "gateway/.env": "UPSTREAM=http://127.0.0.1:20128\n"}, "--json")
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(len(report["violations"]), 1)
        self.assertEqual(len(report["exempt"]), 1)
        self.assertEqual(len(report["conditional"]), 1)
        self.assertEqual(report["gateway"]["proxy_port"], 20129)

    def test_no_root_to_scan_is_a_refusal(self):
        with mock.patch.object(chk, "default_roots", return_value=[]):
            self.assertEqual(chk.main([]), 2)


class DefaultRootTests(unittest.TestCase):
    def test_the_estate_is_two_levels_above_the_script(self):
        estate = Path(tempfile.mkdtemp()) / "complete"
        (estate / "ips" / "scripts").mkdir(parents=True)
        for group in ("3-media", "5-dev"):
            (estate / group).mkdir()
        self.assertEqual(chk.default_roots(estate / "ips" / "scripts" / "check-gateway-targets.py"), [estate.resolve()])

    def test_a_lone_checkout_scans_itself(self):
        repo = Path(tempfile.mkdtemp()) / "ips"
        (repo / "scripts").mkdir(parents=True)
        self.assertEqual(chk.default_roots(repo / "scripts" / "check-gateway-targets.py"), [repo.resolve()])


class BindingAndTargetTests(unittest.TestCase):
    def test_bindings_are_recognised_in_their_several_shapes(self):
        for line in ('- "172.17.0.1:20128:20128"', "20128:20128", '- "${X:-20128}:20128"', '- "127.0.0.1:20128:20128/udp"'):
            with self.subTest(line=line):
                self.assertIsNotNone(chk.BINDING.match(line))

    def test_targets_are_not_mistaken_for_bindings(self):
        for line in ("OMNIROUTE_URL=http://192.168.1.46:20128", "host: 192.168.1.46", "url: http://omniroute:20128/v1"):
            with self.subTest(line=line):
                self.assertIsNone(chk.BINDING.match(line))

    def test_a_host_is_read_out_of_a_url_or_a_bare_pair(self):
        for line, host in (("X=http://192.168.1.46:20128/v1", "192.168.1.46"),
                           ("GATEWAY_API_URL=http://host.docker.internal:20128", "host.docker.internal"),
                           ("AI_BASE_URL: http://omniroute:20128/v1", "omniroute")):
            with self.subTest(line=line):
                self.assertEqual([m.group("host") for m in chk.TARGET.finditer(line)], [host])


if __name__ == "__main__":
    unittest.main()
