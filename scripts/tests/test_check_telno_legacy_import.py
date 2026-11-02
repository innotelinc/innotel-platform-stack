#!/usr/bin/env python3
"""Unit tests for check-telno-legacy-import.py — the importer's exit status, as a metric.

The check is a thin wrapper, so what is worth locking is the *mapping*, because the
script it wraps answers in three ways that must not blur into two:

* 0 — clean, which becomes a note carrying the counts so the dashboard can say what was
  compared rather than only "ok";
* 1 — drifted or unmappable, which becomes one failure per named record (a count alone
  gives nobody anything to fix);
* anything else — the importer did not run (missing volume, container restarting), which
  must exit 2 and never read as a pass, and must not be published as `last_status 1`.

No test here touches incus or a container: the probe is injected.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-telno-legacy-import.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_telno_legacy_import", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


telno = _load()

CLEAN = telno.Result(
    0,
    stdout="legacy import is complete (devices=2 lines=2 directory=1 settings=17 pruned=0)\n",
    stderr="",
)
DRIFT = telno.Result(
    1,
    stdout="{}\n",
    stderr=(
        "drift   devices: LEGACY-805E0C9D6C8D (would-insert)\n"
        "drift   directory: +14132642700 (would-insert)\n"
        "\n2 record(s) would change, 0 could not be mapped. Run `node scripts/import-legacy.mjs` for the plan.\n"
    ),
)
UNMAPPED = telno.Result(
    1,
    stdout="{}\n",
    stderr="skipped devices: ACME-9 — model dp750 is not in the catalog\n\n0 record(s) would change, 1 could not be mapped.\n",
)
BROKEN = telno.Result(2, stdout="", stderr="importer did not run: unable to open database file\n")


class AuditCase(unittest.TestCase):
    def test_a_clean_run_is_a_note_carrying_the_counts(self):
        report = telno.audit(CLEAN)
        self.assertTrue(report.ok, "nothing failed")
        self.assertEqual(len(report.notes), 1)
        self.assertIn("devices=2", report.notes[0].message)
        self.assertIn("legacy import is complete", report.notes[0].message)

    def test_each_changed_record_is_its_own_failure(self):
        report = telno.audit(DRIFT)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 2)
        self.assertEqual([f.code for f in report.failures], ["drift", "drift"])
        # The record identity survives: it is what someone fixes.
        self.assertIn("LEGACY-805E0C9D6C8D", report.failures[0].message)
        self.assertIn("+14132642700", report.failures[1].message)

    def test_column_alignment_is_flattened_not_carried_into_the_label(self):
        for finding in telno.audit(DRIFT).failures:
            self.assertNotIn("   ", finding.message)

    def test_an_unmappable_record_fails_as_unmapped_not_drift(self):
        report = telno.audit(UNMAPPED)
        self.assertFalse(report.ok)
        self.assertEqual(report.failures[0].code, "unmapped")
        self.assertIn("dp750", report.failures[0].message)

    def test_exit_one_without_a_named_record_still_fails(self):
        report = telno.audit(telno.Result(1, stdout="", stderr="something went sideways\n"))
        self.assertFalse(report.ok)
        self.assertEqual(report.failures[0].code, "no_report")
        self.assertIn("exited 1", report.failures[0].message)

    def test_a_clean_run_never_reports_a_failure(self):
        self.assertEqual(telno.audit(CLEAN).failures, [])


class MainCase(unittest.TestCase):
    def _run(self, result, prom: Path | None = None):
        argv = ["--prom", str(prom)] if prom else []
        with mock.patch.object(telno, "probe_node", return_value=result):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = telno.main(argv)
        body = prom.read_text(encoding="utf-8") if prom and prom.exists() else ""
        return code, body

    def test_clean_exits_zero_and_publishes_status_one(self):
        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory) / "telno.prom"
            code, body = self._run(CLEAN, prom)
        self.assertEqual(code, 0)
        self.assertIn(f'innotel_estate_check_last_status{{check="{telno.CHECK}"}} 1', body)
        # The counts ride along as a note series, so the panel can state them.
        self.assertIn(f'innotel_estate_check_note{{check="{telno.CHECK}"', body)
        self.assertIn("devices=2", body)

    def test_drift_exits_one_and_publishes_status_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory) / "telno.prom"
            code, body = self._run(DRIFT, prom)
        self.assertEqual(code, 1)
        self.assertIn(f'innotel_estate_check_last_status{{check="{telno.CHECK}"}} 0', body)
        self.assertIn(f'innotel_estate_check_failures{{check="{telno.CHECK}"}} 2', body)

    def test_a_clean_run_advances_the_success_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory) / "telno.prom"
            _, body = self._run(CLEAN, prom)
        self.assertIn(f'innotel_estate_check_last_success_timestamp{{check="{telno.CHECK}"}}', body)

    def test_a_node_that_cannot_run_the_check_exits_two(self):
        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory) / "telno.prom"
            code, body = self._run(BROKEN, prom)
        self.assertEqual(code, 2)
        # Not a pass, and not a failure it can name: the run counts as not having
        # happened, which is what `EstateCheckCouldNotRun` alerts on.
        self.assertIn(f'innotel_estate_check_last_status{{check="{telno.CHECK}"}} 0', body)
        self.assertNotIn("_failures{", body)

    def test_a_missing_probe_exits_two(self):
        with mock.patch.object(telno, "probe_node", side_effect=OSError("no route to host")):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(telno.main([]), 2)


class CommandCase(unittest.TestCase):
    def test_the_command_reads_the_node_through_incus(self):
        self.assertEqual(telno.COMMAND[:3], ["incus", "exec", "telno"])
        self.assertIn("node", telno.COMMAND)
        self.assertTrue(telno.COMMAND[-1].endswith("check-legacy-import.mjs"))

    def test_the_probe_goes_over_ssh_to_the_host_that_owns_the_container(self):
        """The edge has no incus trust with i4, so the hop has to be ssh (2026-11-02)."""
        with mock.patch.object(telno.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            telno.probe_node("root@192.168.1.54")
        argv = run.call_args[0][0]
        self.assertIn("root@192.168.1.54", argv)
        self.assertIn("incus exec telno", argv[-1])
        self.assertEqual(run.call_args[1]["check"], False, "a non-zero exit is the finding")

    def test_ssh_255_is_not_a_pass_and_not_drift(self):
        report = telno.Result(telno.SSH_FAILURE, stdout="", stderr="ssh: connect to host i4 port 22: timed out\n")
        with tempfile.TemporaryDirectory() as directory:
            prom = Path(directory) / "telno.prom"
            with mock.patch.object(telno, "probe_node", return_value=report):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = telno.main(["--prom", str(prom)])
            body = prom.read_text(encoding="utf-8")
        self.assertEqual(code, 2)
        self.assertIn(f'innotel_estate_check_last_status{{check="{telno.CHECK}"}} 0', body)
        self.assertNotIn("_failures{", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
