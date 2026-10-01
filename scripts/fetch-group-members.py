#!/usr/bin/env python3
"""fetch-group-members.py — clone each group's member repos for the compose check.

WHY THIS EXISTS
---------------
`gen-group-compose.py --check` fails when a checked-in group compose is stale
against its member repos, but it can only say so where those repos are. CI checks
out this repo alone, so the check had no group directories to read, reported "this
checkout stands alone … nothing to generate", and passed — a guard that looks like
coverage and can never fail (recorded in `docs/stack-migration-gaps.md`). This
clones every repo `gen-group-compose.py`'s `SOURCES` names into the group layout
the generator reads, so `--check` does the real work in CI:

    root="$(mktemp -d)"
    scripts/fetch-group-members.py "$root"
    scripts/gen-group-compose.py --root "$root" --check

It is a *separate* step so the check keeps its shape: the generator still reads a
plain estate layout and knows nothing about git or GitHub.

THE REPO LIST IS NOT REPEATED
-----------------------------
Which repos a group has is `SOURCES`, so this imports it rather than keeping a
second list that would drift from the first. The default branch is what the check
compares against, and it is the branch a group host runs: `git clone` without
`--branch` takes the remote's default, which is every repo's checked-out branch in
the estate (`main`, `master`, and `npm` on `develop`).

    scripts/fetch-group-members.py <root> [--depth N]
    scripts/fetch-group-members.py <root> --repo genesis      # one repo, for a poke

Environment:
    INNOTEL_ORG       GitHub org the repos live in (default: innotelinc)
    INNOTEL_GIT_BASE  URL prefix to clone from (default: https://github.com/<org>)
                      — set it to an internal mirror on a host with no egress.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

STACK_DIR = Path(__file__).resolve().parent.parent
GEN = STACK_DIR / "scripts" / "gen-group-compose.py"


def _load_sources() -> dict[str, dict[str, list[str]]]:
    """`SOURCES` from gen-group-compose.py, without executing its `main`."""
    spec = importlib.util.spec_from_file_location("gen_group_compose", GEN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SOURCES


def members(sources: dict[str, dict[str, list[str]]]) -> list[tuple[str, str]]:
    """Every (group, repo) the groups deploy, in a stable order."""
    return sorted((group, repo) for group, repos in sources.items() for repo in repos)


def clone_url(repo: str) -> str:
    base = os.environ.get("INNOTEL_GIT_BASE") or (
        f"https://github.com/{os.environ.get('INNOTEL_ORG', 'innotelinc')}")
    return f"{base.rstrip('/')}/{repo}.git"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="directory to clone the group layout into")
    parser.add_argument("--depth", type=int, default=1,
                        help="clone depth (default 1 — only the checkout is read)")
    parser.add_argument("--repo", action="append", default=[],
                        help="fetch only this repo (repeatable); default all")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    wanted = members(_load_sources())
    if args.repo:
        wanted = [(g, r) for g, r in wanted if r in set(args.repo)]
        missing = set(args.repo) - {r for _, r in wanted}
        if missing:
            print(f"fetch-group-members: no SOURCES entry for {', '.join(sorted(missing))}",
                  file=sys.stderr)
            return 2

    failures: list[str] = []
    for group, repo in wanted:
        target = root / group / repo
        if (target / ".git").is_dir() and any(target.iterdir()):
            # Already fetched — leave it. Re-cloning here would put every re-run
            # back to one branch's tip and throw away a local checkout.
            print(f"  have   {group}/{repo}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        command = ["git", "clone", "--quiet"]
        if args.depth:
            command += ["--depth", str(args.depth)]
        command += [clone_url(repo), str(target)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  cloned {group}/{repo}")
            continue
        failures.append(f"{group}/{repo}: {clone_url(repo)} — "
                        f"{result.stderr.strip() or 'git clone failed'}")
        print(f"  FAILED {group}/{repo}", file=sys.stderr)

    if failures:
        print(f"\nfetch-group-members: {len(failures)} repo(s) could not be fetched — "
              f"the group check cannot run against a partial estate:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(f"fetch-group-members: {len(wanted)} member repo(s) under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
