#!/usr/bin/env python3
"""Unit tests for sync-subscribe-pages.py — who owns the pages directory.

One property, and it is the one that failed in production: a service that stops
being rendered must stop being served. Olympus's page outlived its repo, its edge
names and its entry in the generator, because the script only ever wrote pages and
the check only ever compared what it would write — never what was already there.

The decision is a function of a directory and a set of names, so it is tested
without a stack, a service checkout or a network.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

SYNC = SCRIPTS / "sync-subscribe-pages.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_subscribe_pages", SYNC)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync = _load()


class ExpectedPagesTest(unittest.TestCase):
    def test_owns_one_page_per_service_plus_the_directory(self) -> None:
        self.assertEqual(
            sync.expected_pages(["genie", "magnate"]),
            {"genie.html", "magnate.html", "index.html"},
        )


class StalePageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="subscribe-pages-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for item in self.dir.glob("*"):
            item.unlink()
        self.dir.rmdir()

    def test_a_retired_services_page_is_stale(self) -> None:
        (self.dir / "genie.html").write_text("live")
        (self.dir / "olympus.html").write_text("retired")

        stale = sync.stale_pages(self.dir, sync.expected_pages(["genie"]))

        self.assertEqual(stale, ["olympus.html"], "a page nobody generates is drift")

    def test_a_page_this_run_wrote_is_kept(self) -> None:
        (self.dir / "genie.html").write_text("live")
        (self.dir / "index.html").write_text("directory")

        self.assertEqual(sync.stale_pages(self.dir, sync.expected_pages(["genie"])), [])

    def test_a_backup_beside_a_page_is_not_a_page(self) -> None:
        # People leave these; the portal's nginx picks pages by Host header and serves
        # nothing called `magnate.html.bak-…`, so removing one would be a surprise.
        (self.dir / "magnate.html").write_text("live")
        (self.dir / "magnate.html.bak-20261003-032802").write_text("older")
        (self.dir / "README.md").write_text("notes")

        self.assertEqual(sync.stale_pages(self.dir, sync.expected_pages(["magnate"])), [])

    def test_a_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(sync.stale_pages(self.dir / "nowhere", sync.expected_pages([])), [])


if __name__ == "__main__":
    unittest.main()
