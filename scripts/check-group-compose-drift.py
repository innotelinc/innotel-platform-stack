#!/usr/bin/env python3
"""Fail when a group compose does not declare what its member repos run.

WHY THIS EXISTS
---------------
The estate is five group directories, and each carries a *hand-written* group
compose (`2-voice/docker-compose.yml` and friends) that re-declares what the
member repos already declare. Two hand-kept lists of the same thing drift, and
the drift is invisible until someone asks:

  * `.30` ran Capstone's stack from `2-voice/capstone/docker-compose.yml` while
    the group file declared a different, smaller set — so a host rebuilt from the
    group file came up with Capstone's shell and none of its services;
  * `3-media` was missing `homarr`, `requestrr`, `clipbucket`, `monarch-recs` and
    the media gateways the host actually runs;
  * `5-dev` still declared `chef` long after Atlas retired it;
  * and the same class of gap is why `migrate-stack.py` had to learn to record
    *declared* services: a service with no container has nothing to pack.

`docs/stack-migration-gaps.md` records all of that as action 7 — "re-check the
group composes against the repos … kept open as a recurring check, not a
one-off". This is that check, so the next one does not have to be found by hand.

THE RULES
---------
1. **A member repo's service must be declared by its group compose**, or a host
   built from the group file comes up incomplete. Profile-gated services are
   exempt (rule 3).
2. **A group service no member repo declares is reported too.** Some are the
   group's own (the Consul registration, and the mesh wiring) and are named in
   `GROUP_OWNED`; the rest are either stale entries or a repo that moved on.
3. **`profiles:` means opt-in, on both sides.** A profile-gated service does not
   have to be in the group file — Monarch's compose keeps `transmission`,
   `deluge` and `autobrr` behind profiles on purpose — so it is listed and not
   failed. The point of the list is that the *count* is visible.
4. **A rename is reported, not decided.** The group files prefix what they
   re-declare (`plutus-backend` for plutus's `backend`) and rebundle it
   (`zeus-freepbx` for zeus's `pbx`), so a name-by-name comparison pairs a
   missing service with an extra one and calls it drift. Where a group name and
   a repo name differ only by a dash-separated prefix or segment, the pair is
   printed as *renamed* and does not fail the check; what is left over is a real
   finding. Without that split the output is thirty lines of rename noise around
   the three services that are genuinely absent, and nobody reads it.
5. **Anchors are not services.** `x-*` keys are compose extensions
   (`x-common`, `x-sso-gateway`) and are ignored on both sides.

Only the `services:` keys are read; nothing here starts, configures or reaches a
container, so it runs anywhere, including CI and on a host with no daemon.

USAGE
-----
    ./scripts/check-group-compose-drift.py                  # the estate this lives in
    ./scripts/check-group-compose-drift.py --root /path     # repeatable
    ./scripts/check-group-compose-drift.py --group 3-media  # one group
    ./scripts/check-group-compose-drift.py --json

In a lone checkout (`ips` by itself) there is no group above it, so the default
root has nothing to compare and the run says so and passes — which is what makes
this usable as a CI step in this repo. An *explicitly passed* `--root` that
contains no group compose is an error instead, because that is a typo, not a
standalone checkout.

Exit codes:
    0  no drift between the group composes and their member repos
    1  at least one group is missing a member repo's service (or declares a
       service no repo has, outside the group-owned set)
    2  cannot run (an explicit --root holding no group compose)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Keys the group file owns outright: they are the group's wiring, not a member
# repo's service, so they are expected to appear in no repo and are not drift.
# Each entry is a claim that has to stay true, like the exemption table in
# check-gateway-targets.py.
GROUP_OWNED = {
    # Watchtower: the group runs one auto-updater for every stack on the host.
    "watchtower",
    # 5-dev builds the oasis repo as one mail image (`build: ./oasis`, SMTP +
    # IMAP + web) rather than deploying its per-service compose, so the name has
    # no counterpart in the repo and is not drift.
    "oasis-mail",
}

# Families the group declares that belong to a project the member repos do not
# carry a compose for. `tutor-*` is the LMS core: atheniq's own compose header
# says Tutor/Open edX "runs from Tutor's own compose project (`tutor local`)",
# so there is no repo file to compare against and a rebuilt group host really
# does start them.
GROUP_OWNED_PREFIXES = (
    # The mesh registration every group compose appends, named for its group.
    "consul-reg-g",
    # See above: Tutor/Open edX, deployed by `tutor local`, not from a repo here.
    "tutor-",
)

# Every compose-shaped file a member repo may carry, not just the default one:
# a repo's services are spread over overlays and variants by design (`npm`
# keeps the edge in compose.cerulean.yml, zeus the full stack in
# docker-compose.full.yml, signara prod in docker-compose.prod.yml), and reading
# only `docker-compose.yml` reported those as services no repo declares.
COMPOSE_GLOB = "*compose*.y*ml"

# A top-level `services:` line.
SERVICES = re.compile(r"^services:\s*$")
# A service key: two spaces of indentation, then a name.
SERVICE_KEY = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$")
# `profiles: ["legacy"]`, `profiles:` + a list, or `profiles: [standalone]`.
PROFILES_INLINE = re.compile(r"^\s{4}profiles:\s*\[(.*?)\]\s*$")
PROFILES_BLOCK = re.compile(r"^\s{4}profiles:\s*$")


@dataclass
class Compose:
    """One compose file's services, split by whether they are opt-in."""

    path: Path
    services: dict[str, bool] = field(default_factory=dict)  # name -> profile-gated

    @property
    def always_on(self) -> set[str]:
        return {name for name, gated in self.services.items() if not gated}

    @property
    def opt_in(self) -> set[str]:
        return {name for name, gated in self.services.items() if gated}


def parse_services(path: Path) -> Compose:
    """The services a compose file declares, and which of them are profile-gated.

    Read by indentation rather than by parsing YAML: the files here are
    hand-written, comments are dense, and a YAML dependency would be a new one in
    a repo that ships none. A service body ends at the next `  key:` line.
    """
    compose = Compose(path=path)
    in_services = False
    current: str | None = None
    in_profile_block = False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return compose

    for raw in lines:
        code = raw.split("#", 1)[0].rstrip()
        if SERVICES.match(code):
            in_services = True
            current = None
            continue
        if in_services and code and re.match(r"^\S", code):
            break  # volumes:, networks: — the services block is over
        if not in_services:
            continue
        key = SERVICE_KEY.match(code)
        if key:
            current = key.group(1)
            in_profile_block = False
            if not current.startswith("x-"):
                compose.services.setdefault(current, False)
            continue
        if current is None or current.startswith("x-"):
            continue
        inline = PROFILES_INLINE.match(code)
        if inline:
            compose.services[current] = bool(inline.group(1).strip())
            in_profile_block = False
            continue
        if PROFILES_BLOCK.match(code):
            compose.services[current] = True
            in_profile_block = True
            continue
        if in_profile_block:
            # A `profiles:` list body: `      - legacy`.
            if re.match(r"^\s{6,}-\s*\S", code):
                compose.services[current] = True
                continue
            if code.strip():
                in_profile_block = False
    return compose


def composes_in(directory: Path) -> list[Path]:
    """Every compose file in this directory, in a stable order."""
    return sorted(p for p in directory.glob(COMPOSE_GLOB) if p.is_file())


def compose_in(directory: Path) -> Path | None:
    """The group compose of a group directory — the default name, if present."""
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    found = composes_in(directory)
    return found[0] if found else None


def rename_score(repo: str, missing: str, extra: str) -> int:
    """How sure is the guess? A name the group built from the repo's own name
    (`plutus` + `backend`) beats a generic prefix/suffix match."""
    if extra.startswith(repo + "-"):
        return 2
    return 1


def looks_renamed(missing: str, extra: str) -> bool:
    """Is the group's `extra` the repo's `missing`, wearing a prefix or suffix?

    A guess on purpose, and only ever used to *report* — a guessed pair does not
    fail the check, and an unpaired name does. See rule 4 in the module
    docstring for why the noise has to be separated from the findings.
    """
    if not missing or missing == extra:
        return False
    return (
        extra.endswith("-" + missing)
        or extra.startswith(missing + "-")
        or missing in extra.split("-")
        or extra in missing.split("-")
    )


@dataclass
class Finding:
    group: str
    kind: str  # "missing" | "extra"
    service: str
    where: str  # the repo, or the group file

    @property
    def is_violation(self) -> bool:
        return not (self.kind == "extra"
                    and (self.service in GROUP_OWNED
                         or self.service.startswith(GROUP_OWNED_PREFIXES)))


@dataclass
class GroupReport:
    group: str
    group_file: Path
    members: dict[str, Compose] = field(default_factory=dict)
    group_owned: set[str] = field(default_factory=set)
    opt_in: dict[str, str] = field(default_factory=dict)  # service -> where
    findings: list[Finding] = field(default_factory=list)
    # (repo service, group service) pairs that differ by a prefix or segment.
    renamed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.is_violation]

    def repo_services(self) -> dict[str, str]:
        """service -> the repo that declares it (first one wins, reported)."""
        owned: dict[str, str] = {}
        for repo, compose in sorted(self.members.items()):
            for name in sorted(compose.always_on):
                owned.setdefault(name, repo)
        return owned


def compare(group_dir: Path) -> GroupReport:
    group_file = compose_in(group_dir)
    assert group_file is not None
    report = GroupReport(group=group_dir.name, group_file=group_file)

    for child in sorted(group_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        found = composes_in(child)
        if found:
            # One entry per repo, unioning its compose files: the repo's services
            # are what it can run, whichever file declares each one.
            member = parse_services(found[0])
            for extra_file in found[1:]:
                other = parse_services(extra_file)
                for name, gated in other.services.items():
                    member.services[name] = member.services.get(name, False) or gated
            member.path = found[0]
            report.members[child.name] = member

    group = parse_services(group_file)
    for name in group.opt_in:
        report.opt_in[name] = f"{group_dir.name}/{group_file.name}"

    declared = group.always_on
    for name, repo in sorted(report.repo_services().items()):
        if name not in declared:
            report.findings.append(Finding(group_dir.name, "missing", name, repo))

    repo_names = set(report.repo_services())
    for name in sorted(declared - repo_names):
        report.findings.append(Finding(group_dir.name, "extra", name,
                                       f"{group_dir.name}/{group_file.name}"))

    # Pair the two lists into renames before anything is called drift: a group
    # file that disagrees on a name is still declaring the service.
    # A group service that a member repo declares behind a profile is opted into
    # on both sides, not an extra: `vault` and `authentik-*` live under profiles
    # in cerulean's compose and the group declares them plainly.
    opt_in_names: set[str] = set()
    for compose in report.members.values():
        opt_in_names |= compose.opt_in
    extras = [f for f in report.findings
              if f.kind == "extra"
              and f.service not in GROUP_OWNED
              and not f.service.startswith(GROUP_OWNED_PREFIXES)]
    paired: list[Finding] = []
    remaining: dict[str, Finding] = {f.service: f for f in extras}
    for finding in [f for f in report.findings if f.kind == "missing"]:
        # Highest score first, so a repo's own service takes the name built from
        # that repo's name — `postgres` (repo: signara) must claim
        # `signara-postgres` before another repo's `postgres` does.
        candidates = sorted(
            ((rename_score(finding.where, finding.service, name), name)
             for name in remaining if looks_renamed(finding.service, name)),
            reverse=True,
        )
        if candidates:
            name = candidates[0][1]
            report.renamed.append((finding.service, name))
            paired.extend([finding, remaining[name]])
            del remaining[name]
    for finding in paired:
        report.findings.remove(finding)
    for finding in list(report.findings):
        if finding.kind != "extra":
            continue
        name = finding.service
        # What is left after pairing, and still has no always-on counterpart in
        # any repo: it is the group's own if a repo declares the same service
        # behind a profile (cerulean's `vault`, signara's `minio` as
        # `signara-minio`), or if it is a name this check knows the group owns.
        gated = name in opt_in_names or any(looks_renamed(g, name) for g in opt_in_names)
        if name in GROUP_OWNED or name.startswith(GROUP_OWNED_PREFIXES) or gated:
            report.group_owned.add(name)
            if gated:
                report.opt_in.setdefault(
                    name, f"{group_dir.name}/{group_file.name} (a member repo gates it)")
            report.findings.remove(finding)

    # Profile-gated on either side is opt-in: listed so the count is visible,
    # never failed (rule 3).
    for repo, compose in sorted(report.members.items()):
        for name in compose.opt_in:
            report.opt_in[name] = repo
    return report


def find_groups(root: Path) -> list[Path]:
    """Root children that look like a group: `N-name` with a group compose."""
    groups = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not re.match(r"^\d+-", child.name):
            continue
        if compose_in(child) is not None:
            groups.append(child)
    return groups


def default_root(script: Path) -> Path | None:
    """The estate this script lives in, or None when it stands alone.

    `check-gateway-targets.py` uses the same convention: two levels up from
    `ips/` is the estate (`1-primary/` … `5-dev/`).
    """
    estate = script.resolve().parent.parent.parent
    if any(re.match(r"^\d+-", child.name) for child in estate.iterdir() if child.is_dir()):
        return estate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", action="append", default=[],
                        help="estate root to inspect (repeatable)")
    parser.add_argument("--group", action="append", default=[],
                        help="limit to one group directory name (repeatable)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    explicit = bool(args.root)
    roots = [Path(r).resolve() for r in args.root]
    if not roots:
        fallback = default_root(Path(__file__))
        if fallback is None:
            print("group-compose drift: this checkout stands alone — no group above "
                  "it to compare, nothing to do")
            return 0
        roots = [fallback]

    missing_roots = [r for r in roots if not r.is_dir()]
    if missing_roots:
        print("group-compose drift: no such root: "
              + ", ".join(str(r) for r in missing_roots), file=sys.stderr)
        return 2

    reports: list[GroupReport] = []
    for root in roots:
        for group_dir in find_groups(root):
            if args.group and group_dir.name not in args.group:
                continue
            reports.append(compare(group_dir))

    if not reports:
        if explicit:
            print("group-compose drift: no group compose under "
                  + ", ".join(str(r) for r in roots), file=sys.stderr)
            return 2
        print("group-compose drift: no group compose to compare — nothing to do")
        return 0

    violations = [f for report in reports for f in report.violations]

    if args.json:
        print(json.dumps({
            "roots": [str(r) for r in roots],
            "groups": [
                {
                    "group": report.group,
                    "file": str(report.group_file),
                    "members": sorted(report.members),
                    "group_owned": sorted(report.group_owned),
                    "opt_in": dict(sorted(report.opt_in.items())),
                    "renamed": [list(pair) for pair in report.renamed],
                    "findings": [f.__dict__ for f in report.findings],
                }
                for report in reports
            ],
            "violations": len(violations),
        }, indent=2))
        return 1 if violations else 0

    for report in reports:
        print(f"\n{report.group}  ({report.group_file})")
        print(f"  members    {', '.join(sorted(report.members)) or '(none)'}")
        for report_member, compose in sorted(report.members.items()):
            print(f"    {report_member:<16} {len(compose.always_on):>3} service(s)"
                  + (f", {len(compose.opt_in)} opt-in" if compose.opt_in else ""))
        print(f"  group file {len(parse_services(report.group_file).always_on):>3} service(s)")
        missing = [f for f in report.findings if f.kind == "missing"]
        stale = [f for f in report.findings
                 if f.kind == "extra" and f.is_violation]
        group_owned = sorted(report.group_owned)
        if missing:
            print(f"  MISSING ({len(missing)}) — declared by a member repo, absent "
                  f"here, so a host built from this file comes up incomplete")
            for finding in missing:
                print(f"    {finding.service:<24} (repo: {finding.where})")
        if stale:
            print(f"  EXTRA ({len(stale)}) — declared here, in no member repo")
            for finding in stale:
                print(f"    {finding.service:<24} (no repo declares it)")
        if report.renamed:
            print(f"  renamed ({len(report.renamed)}) — the same service under the "
                  "group's own name, reported and not failed")
            for repo_name, group_name in report.renamed:
                print(f"    {repo_name:<24} -> {group_name}")
        if group_owned:
            print(f"  group's own ({len(group_owned)}): " + ", ".join(group_owned))
        if report.opt_in:
            print(f"  opt-in (profiles), not compared: "
                  + ", ".join(sorted(report.opt_in)))

    if violations:
        print(f"\nfailed: {len(violations)} drift finding(s) between the group composes "
              "and their member repos")
        return 1
    print("\nok: every group compose declares what its member repos run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
