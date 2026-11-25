#!/usr/bin/env python3
"""The music-on-hold check reads two CLIs and a JSON document, so the parsing is the part
worth holding still.

The outputs below are copied from the PBX on 2026-11-25, after the mounts and the class
definitions were put in place — the same day the console stopped warning that
`/var/lib/asterisk/moh` did not hold the class audio. A parser written against a guess
would pass these assertions and then read the live estate as empty, which is the shape of
"0 classes, nothing wrong" this check exists to prevent.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / "check-pbx-moh.py"

CLASSES = """Class: default
\tMode: files
\tDirectory: /var/lib/asterisk/moh/default
Class: jazz
\tMode: files
\tDirectory: /var/lib/asterisk/moh/jazz
"""

FILES = """Class: default
\tFile: /var/lib/asterisk/moh/default/default-mwvx0al7
Class: jazz
\tFile: /var/lib/asterisk/moh/jazz/jazz-mwvx0anb
"""

COUNTS = """DIR /var/lib/asterisk/moh
COUNT /var/lib/asterisk/moh/default|1
COUNT /var/lib/asterisk/moh/jazz|0
"""

#: The image's own `/etc/asterisk/musiconhold.conf`, copied from the PBX. The console's
#: include is the line this check adds, and its position after FreePBX's generated file
#: is what lets the console's `[default]` win.
MUSICONHOLD = """;
; Music on hold class definitions
; This is using the new 1.2 config file format, and will not work with 1.0
; based Asterisk systems
;
#include musiconhold_custom.conf
#include musiconhold_additional.conf
#include musiconhold_console.conf
"""


def load():
    spec = importlib.util.spec_from_file_location("check_pbx_moh", CHECK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before executing: `@dataclass` looks its module up in `sys.modules`, and
    # a module that is only exec'd is invisible to it on 3.14 (the edge's Python is older,
    # which is exactly why this must be pinned here rather than found on the host).
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ParseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.moh = load()

    def test_the_class_list_keeps_the_declared_order_and_directories(self):
        # Order is the console's own (the include is read after FreePBX's), and it is what
        # the passing lines print; sorting it would make the journal disagree with the CLI.
        self.assertEqual([(c.name, c.directory) for c in self.moh.parse_classes(CLASSES)],
                         [("default", "/var/lib/asterisk/moh/default"),
                          ("jazz", "/var/lib/asterisk/moh/jazz")])

    def test_a_class_with_no_directory_is_still_reported(self):
        # Dropping it would report "2 classes" over a class whose audio path is unknown —
        # the one case where silence is most likely and least visible.
        parsed = self.moh.parse_classes("Class: broken\n\tMode: files\n")
        self.assertEqual([(c.name, c.directory) for c in parsed], [("broken", "")])

    def test_an_empty_answer_is_no_classes_rather_than_a_crash(self):
        self.assertEqual(self.moh.parse_classes("No classes defined.\n"), [])

    def test_the_file_list_groups_by_class(self):
        self.assertEqual(self.moh.parse_files(FILES), {
            "default": ["/var/lib/asterisk/moh/default/default-mwvx0al7"],
            "jazz": ["/var/lib/asterisk/moh/jazz/jazz-mwvx0anb"],
        })

    def test_a_class_with_no_files_is_an_empty_list_not_a_missing_key(self):
        # `moh show files` prints the class and then nothing, which has to read as "this
        # class has no file" rather than as "this class does not exist".
        self.assertEqual(self.moh.parse_files("Class: jazz\n"), {"jazz": []})

    def test_the_count_lines_are_read_per_directory(self):
        self.assertEqual(self.moh.parse_counts(COUNTS), {
            "/var/lib/asterisk/moh/default": 1,
            "/var/lib/asterisk/moh/jazz": 0,
        })

    def test_the_publish_directory_comes_from_the_console_itself(self):
        # `MOH_PUBLISH_DIR` is the console's setting, so reading it from the container is
        # what makes this check follow the console rather than a copy of its default.
        self.assertEqual(self.moh.parse_dir(COUNTS), "/var/lib/asterisk/moh")
        self.assertEqual(self.moh.parse_dir("something else\n"), "")

    def test_a_class_name_is_the_directory_under_the_mount(self):
        self.assertEqual(self.moh.class_name("/var/lib/asterisk/moh/jazz"), "jazz")
        self.assertEqual(self.moh.class_name("/data/media/moh/default/"), "default")

    def test_the_mount_is_matched_exactly_not_by_prefix(self):
        # `/var/lib/asterisk/moh/default` mounted somewhere else must not satisfy the
        # mount assertion: the directory the config names is the subdirectory.
        found = [{"Destination": "/var/lib/asterisk/moh/default"},
                 {"Destination": "/var/lib/asterisk/moh/"}]
        self.assertEqual(self.moh.mount_at(found, "/var/lib/asterisk/moh")["Destination"],
                         "/var/lib/asterisk/moh/")
        self.assertIsNone(self.moh.mount_at(found[:1], "/var/lib/asterisk/moh"))

    def test_the_includes_are_read_in_the_order_asterisk_reads_them(self):
        self.assertEqual(self.moh.parse_includes(MUSICONHOLD),
                         ["musiconhold_custom.conf", "musiconhold_additional.conf",
                          "musiconhold_console.conf"])

    def test_an_include_may_be_absolute_and_still_means_the_same_file(self):
        self.assertEqual(
            self.moh.parse_includes('#include "/etc/asterisk/musiconhold_console.conf"\n'),
            ["musiconhold_console.conf"])

    def test_a_commented_mention_of_the_include_is_not_an_include(self):
        # The file is full of `;` comments, and a note that names the file must not satisfy
        # the assertion — that is exactly how a missing include would hide.
        self.assertEqual(self.moh.parse_includes(";#include musiconhold_console.conf\n"), [])

    def test_the_console_file_must_come_after_freepbx_generated_one(self):
        self.assertEqual(self.moh.include_problem(self.moh.parse_includes(MUSICONHOLD)), "")
        self.assertIn("does not include", self.moh.include_problem(
            ["musiconhold_custom.conf", "musiconhold_additional.conf"]))
        self.assertIn("before musiconhold_additional.conf", self.moh.include_problem(
            ["musiconhold_console.conf", "musiconhold_additional.conf"]))

    def test_a_missing_musiconhold_conf_is_its_own_finding(self):
        # None is a missing file, not an empty one: without it Asterisk has no classes at
        # all, which is worse than a class pointing at the wrong directory.
        self.assertIn("does not exist", self.moh.include_problem(None))

    def test_the_per_class_counts_carry_both_sides_the_panel_charts(self):
        classes = self.moh.parse_classes(CLASSES)
        series = self.moh.moh_series(classes, {"/var/lib/asterisk/moh/default": 1,
                                                "/var/lib/asterisk/moh/jazz": 2},
                                     {"default": 1, "jazz": 0})
        self.assertIn('# TYPE innotel_estate_check_moh_files gauge', series)
        self.assertIn('innotel_estate_check_moh_files{check="pbx_moh",class="default",'
                      'source="asterisk"} 1', series)
        self.assertIn('innotel_estate_check_moh_files{check="pbx_moh",class="jazz",'
                      'source="asterisk"} 2', series)
        self.assertIn('innotel_estate_check_moh_files{check="pbx_moh",class="jazz",'
                      'source="console"} 0', series)

    def test_a_published_class_with_no_asterisk_class_is_still_charted(self):
        # The drift case: the console says it published `hold`, Asterisk has no class. The
        # panel has to show the file that plays nowhere, not silently leave the row out.
        series = self.moh.moh_series([], {}, {"hold": 3})
        self.assertIn('class="hold",source="console"} 3', series)

    def test_the_check_publishes_under_its_own_name(self):
        self.assertEqual(self.moh.CHECK, "pbx_moh")

    def test_the_console_path_is_the_one_the_console_generates(self):
        # The generated config names `/data/media/moh/<class>`; the mount at
        # `/var/lib/asterisk/moh` is what Asterisk reads. Both are in the check because
        # losing the second one silently changes what a regenerated config means.
        self.assertEqual(self.moh.MOH_MOUNT, "/var/lib/asterisk/moh")
        self.assertEqual(self.moh.CONSOLE_PATH, "/data/media/moh")

    def test_the_include_paths_are_the_ones_the_pbx_uses(self):
        self.assertEqual(self.moh.INCLUDE_FILE, "/etc/asterisk/musiconhold.conf")
        self.assertEqual(self.moh.CONSOLE_INCLUDE, "musiconhold_console.conf")
        self.assertEqual(self.moh.FREEPBX_INCLUDE, "musiconhold_additional.conf")


if __name__ == "__main__":
    unittest.main()
