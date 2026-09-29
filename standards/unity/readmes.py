#!/usr/bin/env python3
"""Put the Unity theme badge (and a landing-page link) on every estate README.

The estate's READMEs are not generated — each one is written by hand and carries
whatever badges its author cared about. So this does not rewrite them. It finds the
badge block, appends the one badge that is now true of all of them, and leaves every
other line exactly where it was. Run it twice and the second run is a no-op.

    python3 readmes.py /usr/src/projects/complete [--dry-run]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPOS = [
    "1-primary/cerulean",
    "1-primary/magnate",
    "1-primary/npm",
    "1-primary/ontrak-sync",
    "1-primary/signara",
    "1-primary/verifier",
    "2-voice/capstone",
    "2-voice/zeus",
    "3-media/monarch",
    "3-media/plutus",
    "4-social/onyx",
    "4-social/rizzaura",
    "4-social/zapit",
    "5-dev/atlas",
    "5-dev/distro",
    "5-dev/oasis",
    "5-dev/olympus",
    "ips",
]

THEME_DOC = "https://github.com/innotelinc/innotel-platform-stack/blob/main/standards/unity/README.md"
MARKER = "theme-Unity"

# A badge is one line that is entirely a linked image. Anchoring on the whole line
# rather than on `shields.io` matters: several READMEs embed a badge mid-sentence, and
# inserting after such a line would split a paragraph in half.
BADGE_LINE = re.compile(r"^\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)\s*$")
PAGES_LINE = re.compile(r"^\[?!?\[?.*innotelinc\.github\.io/")


def badge(theme_doc: str) -> str:
    return f"[![Theme: Unity](https://img.shields.io/badge/{MARKER}-6366f1)]({theme_doc})"


def transform(text: str, slug: str, name: str, theme_doc: str, dry: bool) -> tuple[str, list[str]]:
    notes: list[str] = []
    lines = text.splitlines()

    if MARKER in text:
        return text, ["badge already present"]

    # Where does the badge block end? The last consecutive badge line in the first
    # chunk of the file — deep enough to cover a centred header, shallow enough that a
    # badge quoted further down in prose is not mistaken for the header block.
    last = None
    for index, line in enumerate(lines[:60]):
        if BADGE_LINE.match(line):
            last = index
    if last is None:
        return text, ["no badge block — needs a hand"]

    lines.insert(last + 1, badge(theme_doc))
    notes.append("badge added")

    if not any(PAGES_LINE.match(line) for line in lines[:80]):
        lines.insert(last + 2, f"**[Landing page →](https://innotelinc.github.io/{slug}/)**")
        notes.append("landing link added")

    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--dry-run", action="store_true")
    # A repo that lives outside the platform tree (OnTrak is its own clone) still gets
    # the badge; only the slug needs saying, because a checkout directory is not always
    # named after its repository.
    parser.add_argument("--repo", help="a single repository directory")
    parser.add_argument("--slug", help="its GitHub repository name")
    args = parser.parse_args()

    if args.repo:
        if not args.slug:
            print("--repo needs --slug", file=sys.stderr)
            return 2
        directory = pathlib.Path(args.repo).resolve()
        readme = directory / "README.md"
        if not readme.is_file():
            print(f"no README.md in {directory}", file=sys.stderr)
            return 1
        text = readme.read_text()
        new_text, notes = transform(text, args.slug, directory.name, THEME_DOC, args.dry_run)
        touched = new_text != text
        print(f"{'~' if touched else '='} {args.slug:<26} " + "; ".join(notes))
        if touched and not args.dry_run:
            readme.write_text(new_text)
        return 0

    root = pathlib.Path(args.root).resolve()
    changed = 0
    for repo in REPOS:
        directory = root / repo
        readme = directory / "README.md"
        if not readme.is_file():
            print(f"? {repo:<26} no README.md")
            continue
        name = directory.name
        # The Pages site is per-repository, so the slug is the directory name — except
        # for `ips`, whose repository is `innotel-platform-stack`. Getting this wrong
        # yields a link that looks plausible and 404s.
        slug = "innotel-platform-stack" if name == "ips" else name
        text = readme.read_text()
        new_text, notes = transform(text, slug, name, THEME_DOC, args.dry_run)
        touched = new_text != text
        changed += 1 if touched else 0
        print(f"{'~' if touched else '='} {name:<26} " + "; ".join(notes))
        if touched and not args.dry_run:
            readme.write_text(new_text)

    print(f"\n{changed} of {len(REPOS)} README(s) {'would change' if args.dry_run else 'changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
