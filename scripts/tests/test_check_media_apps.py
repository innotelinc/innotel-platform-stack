#!/usr/bin/env python3
"""Unit tests for check-media-apps.py — what counts as a dead request path.

The check exists because 2026-11-17 was invisible from every direction: the request UI
answered, its door answered, the library sync ran, and requests still failed because
Radarr's `config.xml` had been truncated by an OOM kill and the container read `Up` with a
dead process inside it. The two boundaries that matter are locked here:

  * **`Up` is not `running`** — the state must come from `inspect`, and `exited`,
    `restarting`, `created`, `dead` and a missing container are all failures; and
  * **a restart count is a warning, not a failure** — it is cumulative for the
    container's life, so paging on it would leave an app red for a restart that healed.

Both are pure functions, so these assert the boundary rather than a live container list.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-media-apps.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_media_apps", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


media = _load()


def _line(name, state="running", restarts=0, running="true"):
    return f"/{name}|{state}|{restarts}|{running}"


def _all_running(**overrides):
    return {
        name: media.AppState(name=name, state=overrides.get(name, "running"), restarts=0)
        for name, _reason in media.EXPECTED
    }


class ParseCase(unittest.TestCase):
    def test_inspect_lines_become_states(self):
        observed = media.parse("\n".join([_line("radarr"), _line("sonarr", "exited", 4, "false")]))
        self.assertEqual(observed["radarr"].state, "running")
        self.assertTrue(observed["radarr"].running)
        self.assertEqual(observed["sonarr"].restarts, 4)
        self.assertFalse(observed["sonarr"].running)

    def test_the_leading_slash_of_dot_name_is_stripped(self):
        self.assertIn("radarr", media.parse(_line("radarr")))

    def test_a_missing_container_is_not_a_running_one(self):
        observed = media.parse("radarr|missing|0|false")
        self.assertTrue(observed["radarr"].missing)
        self.assertFalse(observed["radarr"].running)

    def test_noise_is_skipped_rather_than_guessed_at(self):
        observed = media.parse("Error: No such object: nope\n\n" + _line("jellyfin"))
        self.assertEqual(sorted(observed), ["jellyfin"])

    def test_a_non_numeric_restart_count_does_not_lose_the_app(self):
        observed = media.parse("/radarr|running|x|true")
        self.assertEqual(observed["radarr"].restarts, 0)


class AuditCase(unittest.TestCase):
    def test_every_app_running_is_clean(self):
        self.assertTrue(media.audit(_all_running()).ok)

    def test_an_exited_app_is_a_failure_naming_the_refresh_path(self):
        # The 2026-11-17 state exactly: the container is there and reads Up.
        result = media.audit(_all_running(radarr="exited"))
        self.assertFalse(result.ok)
        self.assertEqual(result.failures[0].code, "not_running")
        self.assertIn("radarr", result.failures[0].message)
        self.assertIn("not running", result.failures[0].message)

    def test_a_restarting_app_is_a_failure(self):
        self.assertFalse(media.audit(_all_running(sonarr="restarting")).ok)

    def test_a_container_that_is_gone_is_a_failure(self):
        observed = _all_running()
        del observed["sabnzbd"]
        result = media.audit(observed)
        self.assertFalse(result.ok)
        self.assertIn("sabnzbd", result.failures[0].message)

    def test_a_missing_container_reads_as_missing_not_as_running(self):
        observed = _all_running()
        observed["prowlarr"] = media.AppState(name="prowlarr", state="missing", restarts=0)
        self.assertFalse(media.audit(observed).ok)

    def test_restarts_are_a_warning_not_a_failure(self):
        observed = _all_running()
        observed["radarr"] = media.AppState(name="radarr", state="running", restarts=4)
        result = media.audit(observed)
        self.assertTrue(result.ok, "a running app must not be a failure for a past restart")
        self.assertEqual([f.code for f in result.warnings], ["restarted"])

    def test_an_app_outside_the_expected_list_is_ignored(self):
        observed = _all_running()
        observed["flaresolverr"] = media.AppState(name="flaresolverr", state="exited", restarts=1)
        self.assertTrue(media.audit(observed).ok)


class CommandCase(unittest.TestCase):
    def test_every_expected_app_is_inspected(self):
        line = media.command()
        for name, _reason in media.EXPECTED:
            self.assertIn(name, line)

    def test_inspect_is_used_because_ps_cannot_see_a_dead_process(self):
        line = media.command()
        self.assertIn("docker inspect", line)
        self.assertIn("RestartCount", line)

    def test_the_container_script_is_single_quoted_so_the_host_shell_cannot_expand_it(self):
        # The first cut passed the loop through a double-quoted `sh -c`, so the host
        # shell expanded `$c` to nothing: docker inspect was called with no argument,
        # every app read as missing, and the check paged six failures of its own.
        line = media.command()
        self.assertIn("sh -c '", line)
        self.assertIn('"$c"', line)

    def test_it_runs_inside_the_container_that_holds_them(self):
        self.assertIn(f"incus exec {media.CONTAINER}", media.command())


class MetricCase(unittest.TestCase):
    def test_running_apps_are_published_per_app(self):
        text = media.prometheus_extra(_all_running(radarr="exited"))
        self.assertIn('innotel_estate_media_app_running{app="radarr"} 0', text)
        self.assertIn('innotel_estate_media_app_running{app="jellyfin"} 1', text)

    def test_restarts_are_published_per_app(self):
        observed = _all_running()
        observed["radarr"] = media.AppState(name="radarr", state="running", restarts=3)
        self.assertIn('innotel_estate_media_app_restarts{app="radarr"} 3', media.prometheus_extra(observed))


class ConfigCase(unittest.TestCase):
    """A truncated settings file is the failure that started this, so it is its own boundary.

    `docker ps` says `Up` while the process sits at `Press enter to exit...`, so the only
    reading that catches this state is the file itself. `0` (truncated) and `None` (not
    there, or not readable) stay distinct because the repair differs.
    """

    def _sizes(self, **overrides):
        sizes = {name: 1024 for name, _reason in media.EXPECTED}
        sizes.update(overrides)
        return sizes

    def test_sizes_come_from_two_field_lines(self):
        text = "\n".join([_line("radarr"), "radarr|1089", "sonarr|0"])
        self.assertEqual(media.parse_configs(text), {"radarr": 1089, "sonarr": 0})

    def test_a_missing_file_is_none_rather_than_zero(self):
        self.assertIsNone(media.parse_configs("radarr|missing")["radarr"])

    def test_state_lines_are_not_read_as_configs(self):
        self.assertEqual(media.parse_configs(_line("radarr")), {})

    def test_an_empty_config_is_a_failure_that_names_the_backup(self):
        result = media.audit(_all_running(), self._sizes(radarr=0))
        self.assertFalse(result.ok)
        self.assertTrue(
            any("zero-byte" in f.message and "Backups/scheduled" in f.message for f in result.failures),
            [f.message for f in result.failures],
        )

    def test_an_unreadable_config_is_a_failure(self):
        result = media.audit(_all_running(), self._sizes(jellyseerr=None))
        self.assertTrue(
            any("no readable" in f.message for f in result.failures),
            [f.message for f in result.failures],
        )

    def test_a_healthy_config_is_clean(self):
        result = media.audit(_all_running(), self._sizes())
        self.assertTrue(result.ok, [f.message for f in result.findings])

    def test_without_config_data_the_running_check_still_stands(self):
        self.assertTrue(media.audit(_all_running(), None).ok)

    def test_the_probe_names_each_apps_own_path(self):
        line = media.command()
        for name, path in media.CONFIG_FILES:
            self.assertIn(f"{name}:{path}", line)
        self.assertIn("stat -c %s", line)

    def test_jellyseerr_is_not_probed_at_radarrs_path(self):
        self.assertEqual(dict(media.CONFIG_FILES)["jellyseerr"], "/app/config/settings.json")

    def test_config_bytes_are_published_with_minus_one_for_unreadable(self):
        text = media.prometheus_extra(_all_running(), self._sizes(radarr=0, jellyfin=None))
        self.assertIn('innotel_estate_media_app_config_bytes{app="radarr"} 0', text)
        self.assertIn('innotel_estate_media_app_config_bytes{app="jellyfin"} -1', text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
