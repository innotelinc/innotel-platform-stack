#!/usr/bin/env python3
"""Unit tests for check-host-disk.py — the numbers, and where the lines are drawn.

The page has said "i2 is the one to watch" for a while and nobody watched it between
surveys; this turns that note into a rule. What has to hold is that the reading is
`df -P`'s and not a guess, and that the two lines mean what the docstring says: 80 % warns,
90 % fails, and a host whose `df` output cannot be read is a finding rather than a pass.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-host-disk.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_host_disk", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


disk = _load()

DF_LINE = "/dev/mapper/ubuntu--vg-ubuntu--lv 160000000 76000000 75600000 51% /\n"


class ParseCase(unittest.TestCase):
    def test_a_df_p_line_reads_percent_and_bytes(self):
        parsed = disk.parse_df(DF_LINE)
        self.assertIsNotNone(parsed)
        percent, avail, total = parsed
        self.assertEqual(percent, 51.0)
        self.assertEqual(avail, 75600000 * 1024)
        self.assertEqual(total, 160000000 * 1024)

    def test_header_only_is_no_reading(self):
        self.assertIsNone(disk.parse_df("Filesystem 1024-blocks Used Available Capacity Mounted on\n"))

    def test_garbage_is_no_reading(self):
        self.assertIsNone(disk.parse_df("df: /nope: No such file or directory\n"))


class AuditCase(unittest.TestCase):
    def _usage(self, percent, host="i2", mount="/"):
        return disk.Usage(host=host, mount=mount, percent=percent, avail_bytes=10 * 1024**3, total_bytes=100 * 1024**3)

    def test_a_roomy_host_is_not_a_finding(self):
        result = disk.audit([self._usage(48.0)])
        self.assertEqual(result.findings, [])
        self.assertTrue(result.ok)

    def test_eighty_percent_warns(self):
        result = disk.audit([self._usage(80.0)])
        self.assertEqual([f.code for f in result.findings], ["tight"])
        self.assertEqual(result.findings[0].level, "warn")

    def test_ninety_percent_fails(self):
        result = disk.audit([self._usage(93.0)])
        self.assertEqual([f.code for f in result.findings], ["full"])
        self.assertFalse(result.ok)

    def test_the_boundary_is_inclusive(self):
        self.assertEqual([f.code for f in disk.audit([self._usage(disk.WARN_PERCENT)]).findings], ["tight"])
        self.assertEqual([f.code for f in disk.audit([self._usage(disk.FAIL_PERCENT)]).findings], ["full"])

    def test_an_unreadable_host_is_a_failure_not_silence(self):
        result = disk.audit([disk.Usage(host="i2", mount="/")])
        self.assertEqual([f.code for f in result.findings], ["no_reading"])
        self.assertIn("i2", result.findings[0].message)


class SeriesCase(unittest.TestCase):
    def test_the_reading_is_published_per_host(self):
        usages = [
            disk.Usage(host="i2", mount="/", percent=48.0, avail_bytes=12 * 1024**3, total_bytes=24 * 1024**3),
            disk.Usage(host="i3", mount="/"),  # no reading
        ]
        body = disk.disk_series(usages)
        self.assertIn('innotel_estate_check_host_disk_percent{check="host_disk",host="i2",mount="/"} 48.000', body)
        self.assertIn('innotel_estate_check_host_disk_avail_bytes{check="host_disk",host="i2",mount="/"} ', body)
        # A host with no reading has no number to publish; the finding carries it instead.
        self.assertNotIn('host="i3"', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
