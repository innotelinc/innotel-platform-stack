#!/usr/bin/env python3
"""Unit tests for check-scan-share.py — the rules, and how the password travels.

The share's filesystem half was verified by hand on 2026-11-19; what these cover is the
half only smbd can answer: that a stanza still says `read only = Yes` with one writer, that
the writer's probe lands with the declared masks, and that a reader is *refused* a write.
Also that the credential never reaches `argv`: the probe is streamed on stdin, and a check
that leaked a password into a process table would be worse than the silence it replaces.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

CHECK = SCRIPTS / "check-scan-share.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_scan_share", CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load()

CLEAN = {
    "cfg_read_only": "Yes",
    "cfg_write_list": "scans",
    "cfg_valid_users": "scans household dhunter",
    "cfg_force_group": "orion-nas",
    "cfg_path": "/srv/storage/Users/scans",
    "writer_list": "  .   D  0  Thu Nov 19 21:54:10 2026\n  scan.pdf  A  1777304  Thu Nov 19 20:40:43 2026",
    "writer_list_rc": "0",
    "writer_put": "putting file /tmp/estate-probe.txt as \\estate-check-probe.txt",
    "writer_put_rc": "0",
    "disk": "-rw-rw---- 1 scans orion-nas 13 Nov 19 21:54 "
    "/srv/storage/Users/scans/estate-check-probe.txt",
    "disk_after": "absent",
    "reader_list_rc": "0",
    "reader_get_rc": "0",
    "reader_bytes": "same",
    "reader_put": 'NT_STATUS_ACCESS_DENIED opening remote file \\estate-reader-write-attempt.txt',
    "reader_put_rc": "1",
    "reader_put_left": "absent",
}


class ProbeTransportCase(unittest.TestCase):
    def test_a_refused_smb_command_is_reported_as_a_failure(self):
        """`smb()` must return smbclient's status, not the `printf` that feeds it.

        Measured 2026-11-19, on the estate: `${PIPESTATUS[0]}` is the password-writing
        `printf`, which succeeds whenever there is a password to write — so the reader's
        refused write (`NT_STATUS_ACCESS_DENIED`, no file on disk) came back as exit 0 and
        the check failed a share that was doing exactly the right thing. A stub
        `smbclient` on PATH leaves the status as the only thing under test.
        """
        script = scan.build_probe("pw", "reader", "reader-pw")
        function = re.search(r"^smb\(\) \{.*?^\}", script, re.S | re.M)
        self.assertIsNotNone(function, "build_probe no longer defines a readable `smb()`")

        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "smbclient"
            stub.write_text(
                "#!/bin/sh\necho 'NT_STATUS_ACCESS_DENIED opening remote file'\nexit 1\n"
            )
            stub.chmod(0o755)
            harness = (
                f"PATH={tmp}:$PATH\n"
                + function.group(0)
                + "\nsmb user pw ls\necho rc=$?\n"
            )
            done = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)

        self.assertIn("NT_STATUS_ACCESS_DENIED", done.stdout)
        self.assertIn("rc=1", done.stdout, f"smbclient's refusal was reported as success:\n{done.stdout}")


class AuditCase(unittest.TestCase):
    def reading(self, **overrides):
        reading = dict(CLEAN)
        reading.update(overrides)
        return reading

    def codes(self, audit):
        return {finding.code for finding in audit.findings}

    def test_a_clean_reader_reading_passes_with_nothing_stated(self):
        result = scan.audit(self.reading(), writer="scans", readers=["household"])
        self.assertTrue(result.ok)
        self.assertEqual(result.findings, [])

    def test_no_reader_credential_is_a_note_not_a_failure(self):
        result = scan.audit(self.reading(), writer="scans", readers=[])
        self.assertTrue(result.ok)
        self.assertEqual([f.code for f in result.notes], ["reader_unproven"])
        self.assertIn("unproven", result.notes[0].message)

    def test_read_only_dropped_is_a_failure(self):
        result = scan.audit(self.reading(cfg_read_only="No"), writer="scans", readers=["household"])
        self.assertEqual(self.codes(result), {"share_not_read_only"})

    def test_a_write_list_without_the_writer_is_a_failure(self):
        result = scan.audit(self.reading(cfg_write_list="household"), writer="scans", readers=["household"])
        self.assertIn("writer_not_write_listed", self.codes(result))

    def test_a_reader_missing_from_valid_users_is_a_failure(self):
        result = scan.audit(self.reading(cfg_valid_users="scans dhunter"), writer="scans", readers=["household"])
        self.assertIn("account_not_valid", self.codes(result))

    def test_a_reader_that_can_write_is_the_regression_this_exists_for(self):
        result = scan.audit(
            self.reading(reader_put_rc="0", reader_put="putting file … as \\estate-reader-write-attempt.txt"),
            writer="scans",
            readers=["household"],
        )
        self.assertFalse(result.ok)
        self.assertIn("reader_can_write", self.codes(result))
        self.assertIn("household", result.failures[0].message)

    def test_a_reader_write_that_lands_on_disk_fails_even_if_smbclient_said_no(self):
        result = scan.audit(self.reading(reader_put_left="present"), writer="scans", readers=["dhunter"])
        self.assertIn("reader_can_write", self.codes(result))

    def test_a_reader_that_lists_but_cannot_read_is_a_failure(self):
        result = scan.audit(
            self.reading(reader_get_rc="1", reader_bytes="different"), writer="scans", readers=["household"]
        )
        self.assertIn("reader_cannot_read", self.codes(result))

    def test_a_writer_that_cannot_write_names_what_samba_said(self):
        result = scan.audit(
            self.reading(writer_put_rc="1", writer_put="NT_STATUS_ACCESS_DENIED opening remote file"),
            writer="scans",
            readers=["household"],
        )
        self.assertIn("writer_cannot_write", self.codes(result))
        self.assertIn("ACCESS_DENIED", result.failures[0].message)

    def test_the_probe_has_to_carry_the_declared_masks(self):
        result = scan.audit(
            self.reading(disk="-rw-rw-rw- 1 scans orion-nas 13 Nov 19 21:54 /srv/storage/Users/scans/x"),
            writer="scans",
            readers=["household"],
        )
        self.assertIn("probe_mode", self.codes(result))

    def test_the_probe_has_to_land_in_the_forced_group(self):
        result = scan.audit(
            self.reading(disk="-rw-rw---- 1 scans scans 13 Nov 19 21:54 /srv/storage/Users/scans/x"),
            writer="scans",
            readers=["household"],
        )
        self.assertEqual(self.codes(result), {"probe_group"})

    def test_a_probe_left_behind_is_a_failure(self):
        result = scan.audit(self.reading(disk_after="present"), writer="scans", readers=["household"])
        self.assertEqual(self.codes(result), {"probe_left_behind"})

    def test_a_share_serving_the_wrong_folder_is_a_failure(self):
        result = scan.audit(self.reading(cfg_path="/srv/storage"), writer="scans", readers=["household"])
        self.assertEqual(self.codes(result), {"wrong_path"})

    def test_a_probe_absent_from_disk_is_a_failure(self):
        result = scan.audit(self.reading(disk="ls: cannot access: No such file or directory"), writer="scans", readers=["household"])
        self.assertEqual(self.codes(result), {"probe_not_on_disk"})

    def test_a_failed_ls_is_not_read_as_a_mode_and_group(self):
        result = scan.audit(self.reading(disk="ls: cannot access '/srv/storage/Users/scans/x': No such file"), writer="scans", readers=["household"])
        self.assertNotIn("probe_group", self.codes(result))
        self.assertNotIn("probe_mode", self.codes(result))


class ParseCase(unittest.TestCase):
    def test_markers_split_blocks_and_inline_values(self):
        text = "@@cfg_read_only\nYes\n@@writer_put_rc=0\n@@disk\n-rw-rw---- 1 scans orion-nas 13 x\n@@disk_after\nabsent\n"
        parsed = scan.parse(text)
        self.assertEqual(parsed["cfg_read_only"], "Yes")
        self.assertEqual(parsed["writer_put_rc"], "0")
        self.assertEqual(parsed["disk"], "-rw-rw---- 1 scans orion-nas 13 x")
        self.assertEqual(parsed["disk_after"], "absent")

    def test_a_marker_with_no_body_is_empty_not_missing(self):
        self.assertEqual(scan.parse("@@reader_put\n"), {"reader_put": ""})


class PasswordTransportCase(unittest.TestCase):
    def test_the_password_is_shell_quoted_once_into_the_script(self):
        script = scan.build_probe("p'w x", "household", "r'w")
        self.assertIn("PW='p'\"'\"'w x'", script)
        self.assertIn("RW='r'\"'\"'w'", script)
        self.assertIn("RU=household", script)

    def test_the_script_asks_for_the_reader_only_when_one_is_named(self):
        self.assertIn('if [ -n "$RU" ]', scan.build_probe("x"))

    def test_the_probe_reaches_the_container_on_stdin_never_argv(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            return mock.Mock(returncode=0, stdout="@@disk_after\nabsent\n", stderr="")

        with mock.patch.object(scan.subprocess, "run", fake_run):
            scan.run_probe("set -u\nPW='secret'\n")

        self.assertEqual(captured["input"], "set -u\nPW='secret'\n")
        self.assertFalse(any("secret" in part for part in captured["argv"]))
        self.assertEqual(captured["argv"][-1], "incus exec nas -- bash -s")

    def test_an_unreadable_host_is_host_unreadable_not_a_pass(self):
        with mock.patch.object(
            scan.subprocess,
            "run",
            return_value=mock.Mock(returncode=255, stdout="", stderr="Permission denied (publickey)."),
        ):
            with self.assertRaises(scan.HostUnreadable):
                scan.run_probe("true")


class MainCase(unittest.TestCase):
    def test_no_password_is_exit_2_and_publishes_a_failed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            prom = str(Path(directory) / "scan-share.prom")
            with mock.patch.dict(scan.os.environ, {}, clear=True):
                code = scan.main(["--prom", prom])
            self.assertEqual(code, 2)
            body = Path(prom).read_text(encoding="utf-8")
        self.assertIn('innotel_estate_check_last_status{check="scan_share"} 0', body)
        # No `failures` series when nothing ran: the shared contract writes the counts only
        # for a result, and a zero here would read as "ran and found nothing".
        self.assertNotIn("innotel_estate_check_failures{check=\"scan_share\"}", body)

    def test_half_a_reader_credential_is_exit_2_rather_than_a_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            prom = str(Path(directory) / "scan-share.prom")
            environment = {"SCAN_SHARE_PASSWORD": "x", "SCAN_SHARE_READ_USER": "household"}
            with mock.patch.dict(scan.os.environ, environment, clear=True):
                self.assertEqual(scan.main(["--prom", prom]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
