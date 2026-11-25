#!/usr/bin/env python3
"""The music-on-hold reconciler edits files inside a docker volume, so the decisions it
makes are what is worth holding still.

Two shapes matter and neither is visible in a run against a healthy PBX: a class file that
came back from the image (missing, or with one class's directory pointing nowhere) and a
`musiconhold.conf` whose include is gone or ahead of FreePBX's generated one. The cases
below are those shapes, written as the text Asterisk actually holds, plus the one thing
the job must not do — invent a `musiconhold.conf` that is not there.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

RECONCILER = Path(__file__).resolve().parents[1] / "ensure-pbx-moh.py"

CLASS_FILE = """[default]
mode=files
sort=alpha
directory=/var/lib/asterisk/moh/default

[jazz]
mode=files
sort=alpha
directory=/var/lib/asterisk/moh/jazz
"""

#: The image's stock file with the console's include already last: the state a healthy
#: volume holds and the one the job must leave untouched.
MUSICONHOLD = """;
; Music on hold class definitions
;
#include musiconhold_custom.conf
#include musiconhold_additional.conf
#include musiconhold_console.conf
"""

CLASSES_CLI = """Class: default
\tMode: files
\tDirectory: /var/lib/asterisk/moh/default
Class: jazz
\tMode: files
\tDirectory: /var/lib/asterisk/moh/jazz
"""


def load():
    spec = importlib.util.spec_from_file_location("ensure_pbx_moh", RECONCILER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before executing: `@dataclass` looks its module up in `sys.modules`, and
    # a module that is only exec'd is invisible to it on 3.14.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReconcileCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.moh = load()

    def test_a_missing_class_file_renders_both_classes(self):
        # The recreated-volume case: the file is gone and nothing defines the console's
        # classes. Both have to come back with the directories the mount holds.
        plan = self.moh.reconcile_classes(None)
        self.assertEqual(self.moh.class_directories(plan),
                         {"default": "/var/lib/asterisk/moh/default",
                          "jazz": "/var/lib/asterisk/moh/jazz"})

    def test_a_complete_file_is_left_alone(self):
        # Rewriting an identical file every ten minutes would bump its mtime and reload
        # the module for nothing; "already right" has to be a real no-op.
        self.assertIsNone(self.moh.reconcile_classes(CLASS_FILE))

    def test_a_missing_class_is_appended_and_other_sections_kept(self):
        text = ("[default]\nmode=files\ndirectory=/var/lib/asterisk/moh/default\n\n"
                "[rock]\nmode=files\ndirectory=/var/lib/asterisk/moh/rock\n")
        plan = self.moh.reconcile_classes(text)
        self.assertIn("[jazz]", plan)
        self.assertIn("directory=/var/lib/asterisk/moh/jazz", plan)
        # The estate has no `rock` class, but a section somebody added is theirs: the job
        # corrects the two it owns and appends, it does not replace the file wholesale.
        self.assertIn("[rock]", plan)
        self.assertIn("directory=/var/lib/asterisk/moh/rock", plan)

    def test_a_wrong_directory_is_rewritten_in_place(self):
        # A class that exists but reads elsewhere plays silence *and* looks defined, so
        # this is the repair that matters most — and only that one line should move.
        text = CLASS_FILE.replace("/var/lib/asterisk/moh/jazz", "/var/lib/asterisk/moh")
        plan = self.moh.reconcile_classes(text)
        self.assertNotIn("directory=/var/lib/asterisk/moh\n", plan)
        self.assertEqual(self.moh.class_directories(plan)["jazz"],
                         "/var/lib/asterisk/moh/jazz")
        self.assertIn("mode=files\nsort=alpha", plan)

    def test_a_class_with_no_directory_line_gets_one(self):
        plan = self.moh.reconcile_classes("[default]\nmode=files\n")
        self.assertEqual(self.moh.class_directories(plan)["default"],
                         "/var/lib/asterisk/moh/default")

    def test_an_include_before_freepbx_is_moved_to_the_end(self):
        # Asterisk lets a later definition win, so the position is the assertion: the
        # console's `[default]` must be read after FreePBX's generated one.
        text = ("#include musiconhold_custom.conf\n"
                "#include musiconhold_console.conf\n"
                "#include musiconhold_additional.conf\n")
        plan = self.moh.reconcile_includes(text)
        self.assertEqual(plan.splitlines()[-1], "#include musiconhold_console.conf")
        self.assertLess(plan.index("musiconhold_additional.conf"),
                        plan.index("#include musiconhold_console.conf"))

    def test_a_missing_include_is_appended(self):
        text = "#include musiconhold_additional.conf\n"
        plan = self.moh.reconcile_includes(text)
        self.assertEqual(plan.splitlines(),
                         ["#include musiconhold_additional.conf",
                          "#include musiconhold_console.conf"])

    def test_repeated_repairs_do_not_multiply_the_include(self):
        # The job runs every ten minutes; a repair that appended every time would grow
        # the file it is meant to keep small and make it disagree with the runbook.
        text = ("#include musiconhold_console.conf\n"
                "#include musiconhold_additional.conf\n"
                "#include /etc/asterisk/musiconhold_console.conf\n")
        plan = self.moh.reconcile_includes(text)
        self.assertEqual(plan.count("musiconhold_console.conf"), 1)

    def test_a_file_that_already_ends_with_the_include_is_left_alone(self):
        self.assertIsNone(self.moh.reconcile_includes(MUSICONHOLD))

    def test_a_commented_mention_does_not_count_as_the_include(self):
        # The stock file's comments name files; a `;` line must not make the job think the
        # include is present when Asterisk will not read it.
        text = (";#include musiconhold_console.conf\n"
                "#include musiconhold_additional.conf\n")
        plan = self.moh.reconcile_includes(text)
        self.assertIn(";#include musiconhold_console.conf", plan)
        self.assertEqual(plan.splitlines()[-1], "#include musiconhold_console.conf")

    def test_a_missing_musiconhold_conf_is_not_invented(self):
        # The file belongs to FreePBX and carries its `custom`/`additional` includes;
        # writing one from this repository's idea of them would replace its config with
        # ours. `main` reports that as unrepairable instead.
        self.assertIsNone(self.moh.reconcile_includes(None))

    def test_the_classes_are_read_back_the_way_the_check_reads_them(self):
        self.assertEqual(self.moh.parse_cli_classes(CLASSES_CLI),
                         {"default": "/var/lib/asterisk/moh/default",
                          "jazz": "/var/lib/asterisk/moh/jazz"})
        self.assertEqual(self.moh.parse_cli_classes("No classes defined.\n"), {})


if __name__ == "__main__":
    unittest.main()
