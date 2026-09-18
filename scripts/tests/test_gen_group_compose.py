#!/usr/bin/env python3
"""Unit tests for gen-group-compose.py — the group-compose generator.

These cover the parts that are easy to get subtly wrong and impossible to see in
a 900-line generated file: where a relative path has to land, whose container
name survives, how a service two repos both declare is resolved, and how the
merge of a repo's own override pair is done.

The cases are the estate's real shapes — `build: .` (30-odd services), Signara's
`docker-compose.prod.yml` + `docker-compose.override.prod.yml` pair, zeus's
standalone-versus-full compose split — each reduced to the two lines that matter.
Nothing here reads a repo or needs Docker, so it runs in CI on a lone checkout.

    python3 -m unittest discover -s scripts/tests -t scripts/tests
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

GEN = Path(__file__).resolve().parents[1] / "gen-group-compose.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_group_compose", GEN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load()

REL = "../../1-primary/signara"


class PathTests(unittest.TestCase):
    """Every relative host path has to resolve from `groups/`, and no other."""

    def test_a_build_context_of_dot_is_the_repo(self):
        """`build: .` is one of the estate's most common lines, and in `groups/`
        it silently built the group's own directory."""
        self.assertEqual(gen.fix_paths("    build: .", REL, "build"),
                         f"    build: {REL}")
        self.assertEqual(gen.fix_paths("      context: .", REL, "build"),
                         f"      context: {REL}")

    def test_a_relative_context_keeps_its_subdirectory(self):
        self.assertEqual(gen.fix_paths("      context: ./apps/api", REL, "build"),
                         f"      context: {REL}/apps/api")

    def test_a_dockerfile_is_relative_to_its_context_and_untouched(self):
        """`dockerfile: apps/api/Dockerfile` means "inside the context" — prefixing
        it with the repo's path double-resolves it."""
        line = "      dockerfile: apps/api/Dockerfile"
        self.assertEqual(gen.fix_paths(line, REL, "build"), line)

    def test_an_env_file_travels_in_both_of_its_shapes(self):
        self.assertEqual(gen.fix_paths("    env_file: .env", REL, "env_file"),
                         f"    env_file: {REL}/.env")
        self.assertEqual(gen.fix_paths("      - .env", REL, "env_file"),
                         f"      - {REL}/.env")
        # The mapping form must keep its key: without it `required: false` sits
        # under a scalar and compose refuses the file.
        self.assertEqual(gen.fix_paths("      - path: .env", REL, "env_file"),
                         f"      - path: {REL}/.env")

    def test_a_bind_mount_of_the_repo_directory_itself(self):
        self.assertEqual(gen.fix_paths("      - .:/app", REL, "volumes"),
                         f"      - {REL}:/app")
        self.assertEqual(gen.fix_paths("      - ./data:/data", REL, "volumes"),
                         f"      - {REL}/data:/data")

    def test_a_container_path_under_command_is_left_alone(self):
        """`command: ./manage.py …` runs inside the image, so it is not a host path."""
        line = "    command: ./manage.py lms runserver 0.0.0.0:8000"
        self.assertEqual(gen.fix_paths(line, REL, "command"), line)
        self.assertEqual(gen.fix_paths("      - FOO=./bar", REL, "environment"),
                         "      - FOO=./bar")

    def test_a_port_and_a_scalar_are_not_paths(self):
        for line in ("      - 3478:3478/udp", "    restart: unless-stopped",
                     "    mem_limit: 1g", "      - '8000-8010:8000-8010'"):
            self.assertEqual(gen.fix_paths(line, REL, "ports"), line)


class MergeTests(unittest.TestCase):
    """A repo deployed as two files is merged the way compose merges them."""

    def test_a_list_key_appends_and_a_mapping_key_is_replaced(self):
        base = ["    image: example:1", "    ports:", '      - "80:80"',
                "    healthcheck:", "      test: ['CMD', 'wget', 'localhost']"]
        override = ["    ports:", '      - "9002:9000"',
                    "    healthcheck:", "      test: ['CMD', 'wget', '127.0.0.1']"]
        merged, changed = gen.merge_service(base, override)
        self.assertEqual(changed, ["ports", "healthcheck"])
        self.assertIn('      - "80:80"', merged)          # appended, not lost
        self.assertIn('      - "9002:9000"', merged)
        self.assertEqual(sum(line.count("healthcheck:") for line in merged), 1)

    def test_a_mapping_valued_key_takes_the_later_value_key_by_key(self):
        """Appending an `environment:` would put two `FOO:` in one YAML mapping,
        which compose will not parse."""
        merged, _ = gen.merge_service(
            ["    environment:", "      FOO: one", "      BAR: keep"],
            ["    environment:", "      FOO: two"])
        text = "\n".join(merged)
        self.assertIn("FOO: two", text)
        self.assertNotIn("FOO: one", text)
        self.assertIn("BAR: keep", text)

    def test_a_new_service_in_the_later_file_is_added(self):
        merged, changed = gen.merge_service(["    image: x"], ["    restart: always"])
        self.assertEqual(changed, ["restart"])
        self.assertIn("    restart: always", merged)


class NamingTests(unittest.TestCase):
    """Container names are the repos' own; only a collision renames one."""

    def test_the_repos_container_name_travels_verbatim(self):
        """The hosts run `cerulean`, `jellyfin`, `zeus-portal` — not the `g<N>-`
        names the hand-written group files invented, which matched nothing."""
        compose = gen.Compose(Path(__file__).resolve())   # path is never read here
        compose.services = {"cerulean": ["    container_name: cerulean"]}
        contribution = gen.Contribution("1-primary", "cerulean", compose)
        self.assertEqual(
            contribution.emit("cerulean", "cerulean", ["    container_name: cerulean"]),
            ["    container_name: cerulean"])

    def test_a_service_renamed_for_a_collision_renames_its_container(self):
        compose = gen.Compose(Path(__file__).resolve())
        compose.services = {"postgres": ["    container_name: postgres"]}
        contribution = gen.Contribution("3-media", "monarch", compose)
        self.assertEqual(
            contribution.emit("postgres", "monarch-postgres",
                              ["    container_name: postgres"]),
            ["    container_name: monarch-postgres"])

    def test_references_to_a_renamed_service_are_rewritten(self):
        """Three shapes carry a service name: `depends_on`, `network_mode:
        service:…`, and a URL host (`redis://redis:6379`)."""
        compose = gen.Compose(Path(__file__).resolve())
        contribution = gen.Contribution("3-media", "plutus", compose)
        contribution.renamed = {"redis": "plutus-redis"}
        self.assertEqual(contribution.renamed_section("redis"), "plutus-redis")
        self.assertEqual(
            gen.URL_HOST.sub(lambda m: contribution.renamed.get(m.group(1), m.group(1)),
                             "      REDIS_URL: redis://redis:6379"),
            "      REDIS_URL: redis://plutus-redis:6379")


class PortTests(unittest.TestCase):
    """The merge's real risk: two services, one host port."""

    def test_a_variable_default_counts_as_a_published_port(self):
        self.assertEqual(gen.published_on("${FREEPBX_SIP_PORT:-5060}:5060"),
                         ("0.0.0.0", "5060", "tcp"))
        self.assertEqual(gen.published_on("${PORT:-20128}:${PORT:-20128}"),
                         ("0.0.0.0", "20128", "tcp"))

    def test_an_address_and_a_protocol_are_part_of_the_key(self):
        self.assertEqual(gen.published_on("127.0.0.1:20128:20128"),
                         ("127.0.0.1", "20128", "tcp"))
        self.assertEqual(gen.published_on("3478:3478/udp"), ("0.0.0.0", "3478", "udp"))

    def test_what_publishes_nothing_is_not_a_finding(self):
        for entry in ("8080", "8000-8010:8000-8010"):
            self.assertIsNone(gen.published_on(entry))

    def test_one_line_per_port_not_per_pair(self):
        text = ("services:\n"
                "  a:\n    ports:\n      - \"3000:3000\"\n"
                "  b:\n    ports:\n      - \"3000:3000\"\n"
                "  c:\n    ports:\n      - \"3000:3000\"\n")
        warnings, notes = gen.port_collisions(text)
        self.assertEqual(len(warnings), 1)
        self.assertIn("a, b, c", warnings[0])
        self.assertEqual(notes, [])

    def test_an_opt_in_service_is_a_note_not_a_warning(self):
        """A port behind a profile only exists on a host that enabled it."""
        text = ("services:\n"
                "  a:\n    ports:\n      - \"3478:3478/udp\"\n"
                "  b:\n    profiles: ['voice']\n    ports:\n      - \"3478:3478/udp\"\n")
        warnings, notes = gen.port_collisions(text)
        self.assertEqual(warnings, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("opt-in", notes[0])

    def test_different_bound_addresses_do_not_collide(self):
        text = ("services:\n"
                "  a:\n    ports:\n      - \"127.0.0.1:20128:20128\"\n"
                "  b:\n    ports:\n      - \"172.17.0.1:20128:20128\"\n")
        warnings, notes = gen.port_collisions(text)
        self.assertEqual((warnings, notes), ([], []))


class ConfigTests(unittest.TestCase):
    """The tables that say what a group deploys, and the guard on stale ones."""

    def test_every_group_has_sources_and_the_files_are_named(self):
        for group, members in gen.SOURCES.items():
            self.assertTrue(members, group)
            for repo, files in members.items():
                self.assertTrue(files, f"{group}/{repo}")
                for file in files:
                    self.assertTrue(file.endswith((".yml", ".yaml")),
                                    f"{group}/{repo}/{file}")

    def test_a_group_owns_one_output_file(self):
        self.assertEqual(gen.output_path("3-media").name, "3-media.yml")
        self.assertEqual(gen.output_path("3-media").parent, gen.OUT_DIR)

    def test_a_dedupe_pin_that_names_no_repo_is_a_warning(self):
        """The pin is a claim about the repos; a stale one has to be visible."""
        for group, pins in gen.DEDUPE.items():
            for name, owner in pins.items():
                self.assertIn(group, gen.SOURCES,
                              f"DEDUPE names group {group}, which has no SOURCES")
                self.assertIn(name, gen.SOURCES[group].get(owner, []),
                              f"DEDUPE pins '{name}' to {owner}, which SOURCES does "
                              f"not list services for")

    def test_the_number_of_a_group_directory_is_its_project_number(self):
        self.assertEqual(gen.number_of("5-dev"), "5")
        with self.assertRaises(gen.CannotRun):
            gen.number_of("dev")


if __name__ == "__main__":
    unittest.main()
