#!/usr/bin/env python3
"""Resolve the admin-page conflicts in the 1.18.0 rebase.

Every conflict on these four pages is upstream restructuring the same feature the
fork had customised (AdminPageHeader, Badge, Flash, the shared pager classes; the
push-relay and feature-gate sections now exist upstream too). So the upstream side
is the one to keep — it is the version that compiles against 1.18.0's components —
and the fork's actual change in those hunks was the Loomix wording, which is
re-applied afterwards.

Keeps every non-conflicted hunk the 3-way merge already applied.
"""
import re
import sys
from pathlib import Path

FILES = [
    "app/(main)/admin/_pages/marketplace.tsx",
    "app/(main)/admin/_pages/policy.tsx",
    "app/(main)/admin/_pages/telemetry.tsx",
    "app/(main)/admin/_pages/version.tsx",
]

BLOCK = re.compile(
    r"<<<<<<< ours\n(?P<ours>.*?)\n?=======\n(?P<theirs>.*?)\n?>>>>>>> theirs\n",
    re.S,
)

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/bulwark-src-build-1.18.0")

total = 0
for rel in FILES:
    p = ROOT / rel
    if not p.exists():
        print(f"missing: {rel}")
        continue
    src = p.read_text()
    n = len(BLOCK.findall(src))
    if not n:
        print(f"{rel}: no conflicts")
        continue
    out = BLOCK.sub(lambda m: m.group("ours") + "\n", src)
    out = out.replace("Bulwark", "Loomix")
    p.write_text(out)
    total += n
    print(f"{rel}: resolved {n}")

print(f"total conflicts resolved: {total}")
