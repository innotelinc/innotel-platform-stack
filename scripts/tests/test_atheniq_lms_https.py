#!/usr/bin/env python3
"""Unit tests for atheniq-lms-https.py.

The failure this exists for is invisible in a rendered file: `http://` inside a
settings module looks like a URL, not like a mixed-content block, and the page that
breaks is one whose own status is 200. So the fixtures here are the three conditional
forms tutor actually ships, the second pass that has to change nothing, and a rendered
file that must be reported.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

SCRIPT = SCRIPTS / "atheniq-lms-https.py"


def _load():
    spec = importlib.util.spec_from_file_location("atheniq_lms_https", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


https = _load()


class RewriteSchemeTest(unittest.TestCase):
    def test_a_bare_scheme_conditional_becomes_https(self) -> None:
        text = 'CORS_ORIGIN_WHITELIST.append("{% if ENABLE_HTTPS %}https{% else %}http{% endif %}://{{ LMS_HOST }}")'
        new, changes = https.rewrite_scheme(text)
        self.assertEqual(changes, 1)
        self.assertIn('append("https://{{ LMS_HOST }}")', new)
        self.assertNotIn("ENABLE_HTTPS", new)

    def test_a_conditional_with_slashes_becomes_https(self) -> None:
        text = 'LMS_ROOT_URL = "{% if ENABLE_HTTPS %}https://{% else %}http://{% endif %}{{ LMS_HOST }}"'
        new, changes = https.rewrite_scheme(text)
        self.assertEqual(changes, 1)
        self.assertEqual(new, 'LMS_ROOT_URL = "https://{{ LMS_HOST }}"')

    def test_the_inline_expression_form_is_rewritten_too(self) -> None:
        # This is the form that left STUDIO_BASE_URL on http after a sweep of the
        # other two, so it gets its own case rather than riding in the fixtures above.
        text = '"STUDIO_BASE_URL": "{{ "https" if ENABLE_HTTPS else "http" }}://{{ CMS_HOST }}",'
        new, changes = https.rewrite_scheme(text)
        self.assertEqual(changes, 1)
        self.assertEqual(new, '"STUDIO_BASE_URL": "https://{{ CMS_HOST }}",')

    def test_every_occurrence_is_counted(self) -> None:
        text = 'a = "{% if ENABLE_HTTPS %}https{% else %}http{% endif %}"\nb = "{% if ENABLE_HTTPS %}https{% else %}http{% endif %}"'
        _new, changes = https.rewrite_scheme(text)
        self.assertEqual(changes, 2)

    def test_a_second_pass_changes_nothing(self) -> None:
        text = 'u = "{% if ENABLE_HTTPS %}https://{% else %}http://{% endif %}{{ LMS_HOST }}"'
        once, _ = https.rewrite_scheme(text)
        twice, changes = https.rewrite_scheme(once)
        self.assertEqual(changes, 0)
        self.assertEqual(twice, once)

    def test_plain_http_that_is_not_a_platform_url_is_left_alone(self) -> None:
        text = 'INTERNAL = "http://localhost:8000"\nSCHEMA = "http://www.w3.org/2001/XMLSchema"'
        new, changes = https.rewrite_scheme(text)
        self.assertEqual(changes, 0)
        self.assertEqual(new, text)


class ProxySettingsTest(unittest.TestCase):
    def test_the_block_is_appended_once(self) -> None:
        text = 'MFE_CONFIG = {"LMS_BASE_URL": "https://{{ LMS_HOST }}"}\n'
        new, changes = https.ensure_proxy_settings(text)
        self.assertEqual(changes, 1)
        self.assertIn(https.PROXY_MARKER, new)
        self.assertIn("CSRF_TRUSTED_ORIGINS", new)
        self.assertTrue(new.startswith(text.rstrip("\n")))

    def test_a_second_call_does_not_append_again(self) -> None:
        once, _ = https.ensure_proxy_settings("x = 1\n")
        twice, changes = https.ensure_proxy_settings(once)
        self.assertEqual(changes, 0)
        self.assertEqual(twice.count(https.PROXY_MARKER), 1)

    def test_settings_that_already_trust_a_proxy_are_not_doubled(self) -> None:
        text = 'SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")\n'
        new, changes = https.ensure_proxy_settings(text)
        self.assertEqual(changes, 0)
        self.assertEqual(new, text)


class StaleUrlsTest(unittest.TestCase):
    def test_an_http_platform_url_is_reported(self) -> None:
        text = 'LMS_BASE_URL = "http://next.atheniq.courses"\n'
        self.assertEqual(https.stale_urls(text), ["http://next.atheniq.courses"])

    def test_https_urls_are_not_reported(self) -> None:
        self.assertEqual(https.stale_urls('u = "https://next.atheniq.courses"'), [])

    def test_local_http_is_not_a_mixed_content_block(self) -> None:
        text = 'a = "http://localhost:8000"\nb = "http://127.0.0.1:80"\nc = "http://www.w3.org/2001/XMLSchema"'
        self.assertEqual(https.stale_urls(text), [])

    def test_a_container_inside_the_estate_is_not_mixed_content(self) -> None:
        # The rendered file really says `http://meilisearch`: a service the LMS talks to
        # over the estate network, never a URL a browser loads. Reporting it would make
        # `--check` fail on a correct file, which is how a check stops being read.
        text = 'MEILISEARCH_URL = "http://meilisearch:7700"\nREDIS = "http://redis:6379"'
        self.assertEqual(https.stale_urls(text), [])

    def test_each_url_is_reported_once(self) -> None:
        text = 'a = "http://next.atheniq.courses"\nb = "http://next.atheniq.courses"\n'
        self.assertEqual(https.stale_urls(text), ["http://next.atheniq.courses"])


class ApplyTest(unittest.TestCase):
    def _tree(self, root: Path) -> Path:
        site_packages = root / "site-packages"
        templates = site_packages / "tutor" / "templates" / "apps"
        templates.mkdir(parents=True)
        patches = site_packages / "tutormfe" / "patches"
        patches.mkdir(parents=True)
        (templates / "production.py").write_text(
            'LMS_ROOT_URL = "{% if ENABLE_HTTPS %}https://{% else %}http://{% endif %}{{ LMS_HOST }}"\n'
        )
        (patches / "openedx-lms-production-settings").write_text(
            '"MFE_CONFIG": {"LMS_BASE_URL": "{{ "https" if ENABLE_HTTPS else "http" }}://{{ LMS_HOST }}"}\n'
        )
        # Outside the two roots: not something the script may touch.
        (site_packages / "tutor" / "other.py").write_text('u = "{% if ENABLE_HTTPS %}https{% else %}http{% endif %}"\n')
        return site_packages

    def test_both_roots_are_rewritten_and_nothing_else_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site_packages = self._tree(Path(tmp))
            touched = https.apply(site_packages)
            self.assertEqual(len(touched), 2)
            rendered = (site_packages / "tutor/templates/apps/production.py").read_text()
            self.assertIn('"https://{{ LMS_HOST }}"', rendered)
            patch = (site_packages / https.LMS_PATCH).read_text()
            self.assertIn("https://{{ LMS_HOST }}", patch)
            self.assertIn(https.PROXY_MARKER, patch)
            self.assertIn("ENABLE_HTTPS", (site_packages / "tutor/other.py").read_text())

    def test_a_second_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site_packages = self._tree(Path(tmp))
            https.apply(site_packages)
            self.assertEqual(https.apply(site_packages), [])

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site_packages = self._tree(Path(tmp))
            before = (site_packages / "tutor/templates/apps/production.py").read_text()
            touched = https.apply(site_packages, dry_run=True)
            self.assertEqual(len(touched), 2)
            self.assertEqual((site_packages / "tutor/templates/apps/production.py").read_text(), before)

    def test_a_directory_that_is_not_tutor_packages_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                https.apply(Path(tmp))


class CheckCommandTest(unittest.TestCase):
    def test_a_rendered_http_url_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "production.py"
            settings.write_text('LMS_BASE_URL = "http://next.atheniq.courses"\n')
            self.assertEqual(https.check([settings]), 1)

    def test_a_rendered_https_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "production.py"
            settings.write_text('LMS_BASE_URL = "https://next.atheniq.courses"\nINTERNAL = "http://localhost:8000"\n')
            self.assertEqual(https.check([settings]), 0)


if __name__ == "__main__":
    unittest.main()
