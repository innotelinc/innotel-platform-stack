#!/usr/bin/env python3
"""Every command block a document shows must at least parse.

The runbooks in `docs/` are the only way a host change reaches a person: the textfile
move, the identity host, the mail discovery name, the Orion issuer apply. Nothing in CI
reads them, so a typo lives there until it is pasted on a host — and it is pasted at the
one moment the sequence matters, by someone following a document whose whole point is
that order is load-bearing.

So the blocks are parsed, not executed: `bash -n` reads a block and reports syntax errors
without running anything. It is a weak check on purpose and it catches the mistakes that
are invisible in prose — an unclosed `if`, a `\\` continuation that ends the command, a
quote that never closes.

Two normalisations keep it honest rather than pedantic:

* `<placeholder>` tokens become one word. A runbook writes `<container>` where a name
  goes and bash reads `<>` as a redirection, so the block fails for a reason that is
  about the documentation's notation rather than its commands.
* a block is read whole. Runbooks show `for` loops, heredocs and pipelines continued
  across lines, and parsing one line at a time would report failures that are not there.

What this does not claim: that a block is correct, safe, or meant to be pasted. Only that
bash will not stop at it with a syntax error. `docs/unit-apply-checklist.md` is the
reasoning about order; this is the spelling.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The documents a person applies from: the README's examples and everything under
#: `docs/`, including the roadmap notes, because those carry runnable snippets too.
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]

#: A fenced block that declares itself shell. Blocks tagged `text`, `console` or `yaml`
#: are output or configuration and are deliberately not parsed.
BLOCK = re.compile(r"```(?:bash|sh)\n(.*?)```", re.S)

#: `<name>` as a runbook writes it. `<<` (a heredoc) and `2>&1` are untouched by this.
PLACEHOLDER = re.compile(r"<[^<>\n]*>")

#: Below this, the fenced-block pattern has stopped matching and the check above would
#: pass by reading nothing. Measured 2026-11-14: 63 blocks across the docs.
FLOOR = 40


def blocks(path: Path) -> list[str]:
    return BLOCK.findall(path.read_text(encoding="utf-8"))


class RunbookCommandCase(unittest.TestCase):
    def test_every_shell_block_parses(self):
        failures = []
        for doc in DOCS:
            for index, body in enumerate(blocks(doc), 1):
                normalised = PLACEHOLDER.sub("PLACEHOLDER", body)
                done = subprocess.run(
                    ["bash", "-n"], input=normalised, text=True, capture_output=True, check=False
                )
                if done.returncode != 0:
                    diagnostic = (done.stderr.strip().splitlines() or ["no diagnostic"])[-1]
                    failures.append(f"{doc.relative_to(ROOT)} block {index}: {diagnostic}")
        self.assertEqual(failures, [], "a document shows a command bash cannot parse")

    def test_there_are_blocks_to_check(self):
        found = sum(len(blocks(doc)) for doc in DOCS)
        self.assertGreater(found, FLOOR, "the fenced-block pattern found almost nothing")

    def test_the_runbooks_that_change_a_host_are_covered(self):
        # Named rather than counted: these two are the ordered applies for work that cannot
        # be done from this repository, so a fence style change must not quietly drop them
        # out of the check above.
        for name in ("mail-discovery-canonical.md", "orion-issuer-apply.md"):
            self.assertTrue(blocks(ROOT / "docs" / name), name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
