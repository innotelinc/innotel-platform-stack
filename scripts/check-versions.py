#!/usr/bin/env python3
"""Prove every product in the stack is on the one declared version.

The manifest is `docs/versions.md`; each row links a roadmap, and each roadmap states
its target. This reads all three (VERSION, the manifest, the roadmaps) and fails on any
disagreement, so "all products at 2.0" is a checked fact rather than a table somebody
edited once. Exit 0 clean, 1 mismatch, 2 usage error.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
MANIFEST = ROOT / "docs" / "versions.md"
ROADMAPS = ROOT / "docs" / "roadmaps"

def fail(message: str) -> None:
    print(f"check-versions: {message}", file=sys.stderr)

version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else ""
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    fail(f"VERSION is missing or not X.Y.Z ({version!r})")
    sys.exit(2)
if not MANIFEST.exists():
    fail("docs/versions.md is missing")
    sys.exit(2)

rows = []
for line in MANIFEST.read_text().splitlines():
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    if len(cells) != 5 or cells[0] in ("Product", "") or set(cells[0]) <= {"-"}:
        continue
    rows.append(cells)

if not rows:
    fail("docs/versions.md has no product rows")
    sys.exit(2)

names = {cells[0] for cells in rows}
failures = []
for cells in rows:
    name, _cls, _host, declared, _link = cells
    if declared != version:
        failures.append(f"{name}: manifest says {declared}, VERSION says {version}")
    roadmap = ROADMAPS / f"{name}.md"
    if not roadmap.exists():
        failures.append(f"{name}: no docs/roadmaps/{name}.md")
        continue
    text = roadmap.read_text()
    match = re.search(r"^- \*\*Target version:\*\* (\S+)$", text, re.MULTILINE)
    if match is None:
        failures.append(f"{name}: roadmap has no 'Target version' line")
    elif match.group(1) != version:
        failures.append(f"{name}: roadmap targets {match.group(1)}, VERSION says {version}")

extra = {p.stem for p in ROADMAPS.glob("*.md")} - names - {"README"}
for name in sorted(extra):
    failures.append(f"{name}: roadmap exists but is not in docs/versions.md")

if failures:
    for failure in failures:
        fail(failure)
    sys.exit(1)

print(f"check-versions: {len(rows)} products on {version}")
