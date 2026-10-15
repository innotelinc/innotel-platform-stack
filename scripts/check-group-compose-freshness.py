#!/usr/bin/env python3
"""Alert when a checked-in group compose falls behind its member repos.

WHY THIS EXISTS
---------------
The files in `groups/` are generated *and tracked*: `gen-group-compose.py` builds each
one from its member repos, and the file — not the repo it was built from — is what a
rebuilt group host starts from. So the file can fall behind the repos in two ways:

* a member repo adds or retires a service and nobody regenerates, and the file a
  rebuild reads declares a set no repo has;
* somebody regenerates and does not commit, so the working tree looks right and the
  pushed file is behind. On 2026-10-15 exactly that was found in this repo: a local
  `--check` passed against a modified working tree while `main` still named Authentik
  `2026.8.3` and still declared signara's retired `grafana`/`loki` services. It was
  visible only by comparing a clean clone against the repos.

CI does run that comparison (`fetch-group-members.py` + `gen-group-compose.py --check`,
the job *"Group composes are current with their member repos"*), but only when **this**
repo changes — and the inputs live in the member repos. A repo that moves on is
therefore invisible here until the next push, and the first symptom is a rebuild that
starts the wrong set of services. This is the same computation on a timer, publishing
into the estate's own alerting: `systemd/estate-checks.{service,timer}` on the Cerulean
edge writes a textfile into node-exporter's directory, and
`extensions/monitoring/prometheus/rules/estate-checks.yml` alerts on it through the
shared `innotel_estate_check` family.

WHAT IT COMPARES, AND WHERE THE TWO SIDES COME FROM
---------------------------------------------------
Both sides have to be the same commit, and the host this runs on is not the repo it
checks: the edge holds a copy of `scripts/` and nothing else, so there is no checked-in
`groups/` to read there. This check therefore clones the stack repo itself, at its
default branch, into a scratch directory, fetches each group's member repos beside it,
and compares:

    groups/<group>.yml  as pushed          (the file a rebuild starts from)
    generate(<group>)   from the members   (what the repos say today)

Both sides are about the repos and never about a working copy on the host running the
check — which is the point: a dirty tree cannot make this check pass, and it does not
matter which year's copy of the scripts is deployed.

    ./scripts/check-group-compose-freshness.py
    ./scripts/check-group-compose-freshness.py --prom /path/to/textfile
    ./scripts/check-group-compose-freshness.py --stack-dir ~/ips --root ~/estate  # no network

Environment:
    INNOTEL_ORG       GitHub org the repos live in (default: innotelinc)
    INNOTEL_GIT_BASE  URL prefix to clone from (default: https://github.com/<org>)
                      — set it to an internal mirror on a host with no egress.

THE RULES
---------
1. **A group file that differs from its regeneration is a failure**, and the finding
   names the group and the services that moved, because "regenerate and commit" is only
   actionable once you know which group and what changed.
2. **A group with no checked-in file is a failure, not a skip.** Nothing to compare is
   the state a rebuilt host starts nothing from.
3. **Not being able to compare is not a pass.** A repo that will not clone, or a member
   repo the generator cannot read, exits 2 and publishes status 0 — the `EstateCheckCouldNotRun`
   path — because "no data" is how drift stays invisible.

Exit codes: 0 = every group file is what the repos say, 1 = one is not, 2 = the check
could not run.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import METRIC, Audit, Finding, write_prom  # noqa: E402

CHECK = "group_compose_freshness"

#: The repo whose checked-in group composes are compared. Its default branch is what a
#: group host is rebuilt from, so that is the side of the comparison that matters.
STACK_REPO = "innotel-platform-stack"

#: A service block: a two-space-indented key in the `services:` tree. The generator
#: counts service blocks the same way, so the two never disagree about what a service is.
_SERVICE = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):", re.MULTILINE)


def git_base() -> str:
    """Where the repos are cloned from — the same two knobs `fetch-group-members.py` reads."""
    return os.environ.get("INNOTEL_GIT_BASE") or (
        f"https://github.com/{os.environ.get('INNOTEL_ORG', 'innotelinc')}"
    )


def clone_url(repo: str) -> str:
    return f"{git_base().rstrip('/')}/{repo}.git"


class CannotRun(RuntimeError):
    """The comparison could not be made — exit 2, never silently a pass."""


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def service_names(text: str) -> list[str]:
    """The service blocks a compose declares, in file order.

    Only the `services:` tree is read — from that key to the next column-0 key — so the
    generated file's anchors (`x-common-keys`, `x-sso-env`, `x-sso-gateway`, which sit
    above `services:` with two-space-indented keys of their own) cannot look like a
    service. Naming a real service is the whole use of this: it is what a finding says
    changed.
    """
    tree: list[str] = []
    inside = False
    for line in text.splitlines():
        if not inside:
            inside = line.startswith("services:")
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            break  # the next top-level key ends the tree
        tree.append(line)
    return _SERVICE.findall("\n".join(tree))


# --------------------------------------------------------------------------------------
# The two sides, as data — pure, so what counts as "behind" is the thing under test
# --------------------------------------------------------------------------------------


@dataclass
class GroupState:
    """One group, as the checked-in file and the member repos each say it is."""

    group: str
    path: Path
    tracked: str
    regenerated: str

    @property
    def current(self) -> bool:
        return self.tracked == self.regenerated

    @property
    def added(self) -> list[str]:
        """Services the repos declare that the checked-in file does not."""
        have = set(service_names(self.tracked))
        return [name for name in service_names(self.regenerated) if name not in have]

    @property
    def retired(self) -> list[str]:
        """Services the checked-in file still declares that the repos no longer do."""
        say = set(service_names(self.regenerated))
        return [name for name in service_names(self.tracked) if name not in say]


def read_states(generator, root: Path) -> list[GroupState]:
    """Read every group's checked-in file beside its regeneration, from `root`."""
    states: list[GroupState] = []
    for group in sorted(generator.SOURCES):
        path = generator.output_path(group)
        tracked = path.read_text(encoding="utf-8") if path.is_file() else ""
        regenerated, _warnings, _notes = generator.generate(group, root)
        states.append(GroupState(group=group, path=path, tracked=tracked, regenerated=regenerated))
    return states


def _sample(names: list[str], limit: int = 4) -> str:
    shown = ", ".join(names[:limit])
    return shown if len(names) <= limit else f"{shown} (+{len(names) - limit} more)"


def audit(states: list[GroupState]) -> Audit:
    """Read the states as findings: a file the repos have moved past is a failure."""
    out = Audit()
    for state in states:
        if state.current:
            continue
        where = state.path.name
        if not state.tracked:
            out.findings.append(
                Finding(
                    "missing",
                    "fail",
                    f"{state.group}: no checked-in {where} — a host rebuilt from `groups/` "
                    f"starts none of this group's {len(service_names(state.regenerated))} service(s)",
                )
            )
            continue
        changes = []
        if state.added:
            changes.append(f"the repos add {_sample(state.added)}")
        if state.retired:
            changes.append(f"the repos retire {_sample(state.retired)}")
        # A change that moves no service name is the common case for a regeneration — a
        # profile, an image tag, an env default — and saying so is what tells the reader
        # whether to expect a service count to move with the file.
        detail = (
            "; ".join(changes)
            if changes
            else "every service name matches — the difference is inside a body (a profile, an image, an env default)"
        )
        out.findings.append(
            Finding(
                "behind",
                "fail",
                f"{state.group}: {where} is not what the repos say — {detail}; "
                f"run scripts/gen-group-compose.py and commit the result",
            )
        )
    return out


def freshness_series(states: list[GroupState]) -> str:
    """The per-group gauge, so which group is behind is chartable and not only the verdict."""
    stale = sum(1 for state in states if not state.current)
    lines = [
        f"# HELP {METRIC}_group_compose_stale_groups Group composes that are not what their member repos say",
        f"# TYPE {METRIC}_group_compose_stale_groups gauge",
        f'{METRIC}_group_compose_stale_groups{{check="{CHECK}"}} {stale}',
        f"# HELP {METRIC}_group_compose_group_stale 1 when a group's checked-in compose is behind its member repos",
        f"# TYPE {METRIC}_group_compose_group_stale gauge",
    ]
    for state in sorted(states, key=lambda s: s.group):
        lines.append(
            f'{METRIC}_group_compose_group_stale{{check="{CHECK}",group="{state.group}"}} '
            f"{0 if state.current else 1}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# The estate, as it is — clones on both sides
# --------------------------------------------------------------------------------------


def clone(repo: str, dest: Path) -> None:
    """A shallow clone of `repo`'s default branch — the branch a group host runs."""
    result = _run(["git", "clone", "--quiet", "--depth", "1", clone_url(repo), str(dest)])
    if result.returncode != 0:
        raise CannotRun(f"could not clone {clone_url(repo)} — {result.stderr.strip() or 'git clone failed'}")


def load_generator(stack_dir: Path):
    """`gen-group-compose.py` from the stack checkout, without executing its `main`."""
    path = stack_dir / "scripts" / "gen-group-compose.py"
    if not path.is_file():
        raise CannotRun(f"no {path} — {stack_dir} is not a stack-repo checkout")
    spec = importlib.util.spec_from_file_location("group_compose_freshness_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fetch_members(stack_dir: Path, root: Path) -> None:
    """Clone every repo `SOURCES` names into `root` — the CI job's own step, reused."""
    script = stack_dir / "scripts" / "fetch-group-members.py"
    result = _run([sys.executable, str(script), str(root)])
    if result.returncode != 0:
        detail = [line for line in result.stderr.strip().splitlines() if line.strip()]
        raise CannotRun(
            "a member repo could not be fetched, and a partial estate is not an answer: "
            + (detail[-1] if detail else "fetch-group-members.py failed")
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    parser.add_argument("--stack-dir", metavar="DIR",
                        help="compare this stack-repo checkout instead of a fresh clone of it")
    parser.add_argument("--root", metavar="DIR",
                        help="use member repos already under this directory instead of fetching them")
    parser.add_argument("--work-dir", metavar="DIR", help="scratch directory (default: a temporary one)")
    parser.add_argument("--keep", action="store_true", help="leave the scratch directory in place")
    args = parser.parse_args(argv)

    def emit(result: Audit | None, ran: bool, extra: str | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=extra)
            except OSError as error:
                print(f"check-group-compose-freshness: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("git") is None:
        print(
            "check-group-compose-freshness: no git on PATH — both sides of the comparison come "
            "from the remote (the checked-in files and the member repos)",
            file=sys.stderr,
        )
        emit(None, ran=False)
        return 2

    work = Path(args.work_dir).resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="group-compose-freshness-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        stack = Path(args.stack_dir).resolve() if args.stack_dir else work / STACK_REPO
        root = Path(args.root).resolve() if args.root else work / "estate"
        try:
            if not args.stack_dir:
                clone(STACK_REPO, stack)
            generator = load_generator(stack)
            if not args.root:
                fetch_members(stack, root)
            states = read_states(generator, root)
        except CannotRun as error:
            print(f"check-group-compose-freshness: {error}", file=sys.stderr)
            emit(None, ran=False)
            return 2
        except Exception as error:  # noqa: BLE001 — a generation that fails is not a pass either
            print(f"check-group-compose-freshness: could not compare the group composes: {error}", file=sys.stderr)
            emit(None, ran=False)
            return 2

        result = audit(states)
        emit(result, ran=True, extra=freshness_series(states))
        for level in ("fail", "warn", "note"):
            for finding in [f for f in result.findings if f.level == level]:
                print(f"{level.upper():4} {finding.message}")
        behind = [state.group for state in states if not state.current]
        print(f"\ncheck-group-compose-freshness: {len(behind)} of {len(states)} group file(s) behind the repos"
              + (f": {', '.join(behind)}" if behind else ""))
        return 0 if result.ok else 1
    finally:
        if not args.keep and not args.work_dir:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
