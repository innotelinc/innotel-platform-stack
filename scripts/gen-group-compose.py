#!/usr/bin/env python3
"""gen-group-compose.py — build each group compose from its member repos.

WHY GENERATE INSTEAD OF HAND-WRITE
----------------------------------
The estate is five group directories, and each used to carry a *hand-written*
group compose that re-declared what its member repos already declare. Two
hand-kept lists of the same thing drift, and the drift was invisible until
someone asked: `2-voice` was missing 33 of its members' services, `1-primary`
four, `3-media` ten, `5-dev` two — so a host rebuilt from a group file came up
with a stack's shell and none of its services. `docs/stack-migration-gaps.md`
action 1 is to stop re-declaring; action 2 is that the result has to be
reviewable. This is both: the group composes are derived from the repos and
checked in, so a diff shows exactly what a repo change would deploy.

WHAT IS GENERATED, AND FROM WHAT
--------------------------------
    scripts/gen-group-compose.py            # regenerate every group
    scripts/gen-group-compose.py --check    # fail if the checked-in file is stale
    scripts/gen-group-compose.py --group 3-media

Each group's output (`groups/<n>-<group>.yml`) is, in order:

  1. every non-opt-in **and** opt-in service of each member repo's *authoritative*
     compose file(s), carrying `profiles:` through untouched — an opt-in service
     stays opt-in, it just has to be *declared*;
  2. the group's own services and wiring, read verbatim from
     `groups/extras/<group>.yml` (the Consul registration, the mesh, anything a
     group owns rather than a repo — see `GROUP_OWNED` in
     `check-group-compose-drift.py` for the same set from the checker's side).

The source list is `SOURCES` below: repo → the compose files the group deploys.
That choice is the one thing a repo cannot state about itself — `signara` keeps
`docker-compose.dev.yml`, `docker-compose.prod.yml` and an override, and only the
group knows prod is what runs — so it lives here, in one reviewable table. The
drift checker still reads *every* compose file a repo carries; this reads the
ones that get deployed.

PATHS ARE REWRITTEN, NOT INHERITED
----------------------------------
Compose resolves relative paths against the file that declares them, so a
generated file in `groups/` cannot keep a repo's `./data` or `env_file: .env`.
Every path-shaped value (`build.context`, `build.dockerfile`, `env_file`, a bind
mount's source, `extends.file`) is prefixed with the declaring repo's location
relative to the output file. Run the result from anywhere.

WHAT IT REFUSES TO GUESS
------------------------
* **A name two repos both declare** is kept for *both*, each prefixed with its
  repo (`zeus-freepbx` and `capstone-freepbx`) rather than one being silently
  dropped, and every reference to a renamed service is rewritten with it —
  `depends_on`, `network_mode: service:…`, and the service name inside a URL.
  Which of the two should actually run is a deployment decision, and the report
  names it.
* **…except where `DEDUPE` records the decision.** Two copies of one service on
  one host is not a choice, it is a failure to start (they publish the same
  port), so the groups where that is settled name the owning repo in `DEDUPE`,
  the other repo's copy is dropped, and a pin that goes stale is a warning.
* **A repo's `container_name` is never rewritten.** The old hand-written group
  files prefixed them (`g1-cerulean`), which matched no container on any host:
  the hosts run `cerulean`, `jellyfin`, `zeus-portal`, `omniroute` — the repos'
  own names, which is also what every script and doc here names. Only a service
  renamed for a collision gets a new name, since two containers cannot share one.
* **A host-port published twice** is a report line, not a rewrite. That is the
  collision that matters on a single host (`coturn` on 3478, declared by both
  `capstone` and `zeus`), and no generator can choose which stack owns it.

Exit codes:
    0  written (or, with --check, the checked-in files are current)
    1  --check found a stale file, or a group has no generated output yet
    2  cannot run (a group given by --group does not exist)
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

STACK_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = STACK_DIR.parent
OUT_DIR = STACK_DIR / "groups"
EXTRAS_DIR = OUT_DIR / "extras"

# The group's own id in the compose project name: `1-primary` →
# `name: innotel-group1`. Container names are a repo's own and travel verbatim
# (see `Contribution.emit`).
GROUP_NUMBER = re.compile(r"^(\d+)-")

# ── what each group deploys ───────────────────────────────────────────────────
# repo → the compose file(s) the group runs, in load order. Order matters only
# for a name two files of the *same* repo declare: the first wins and the second
# is reported. Keeping a repo's overlays out of the list is deliberate — a
# `.build` variant or a dev override is not what a group host runs.
#
# This is the one thing a repo cannot state about itself, so where the answer was
# ambiguous it was **measured**: every compose file here is the one the running
# containers name in their own `com.docker.compose.project.config_files` label
# (estate hosts `.30`, `.46`, `.50`, `.56`, read 2026-09-18). That is what settled
# `zeus`: it carries a standalone portal compose and a full-stack one, and the
# voice host runs the full stack — `zeus-freepbx`, `zeus-portal`, `pbx-coturn`,
# all three from `docker-compose.full.yml`. Merging both would have declared a
# second portal and a second gateway that no host runs, and two containers cannot
# share a name.
SOURCES: dict[str, dict[str, list[str]]] = {
    "1-primary": {
        "cerulean": ["docker-compose.yml"],
        "npm": ["compose.cerulean.yml", "docker-compose.yml"],
        "magnate": ["docker-compose.yml"],
        "sign": ["docker-compose.yml"],
        # Both: the trust host runs Signara as `docker-compose.prod.yml` *with*
        # its prod override applied — that is the pair its containers report.
        "signara": ["docker-compose.prod.yml", "docker-compose.override.prod.yml"],
        # atheniq is Tutor-managed (`tutor local`), so its compose carries only
        # profile-gated extras; the LMS itself is group-owned (see the extras).
    },
    "2-voice": {
        "capstone": ["docker-compose.yml"],
        # Full stack, not the standalone portal compose: see the measurement note
        # above. `docker-compose.platform.yml` (a second Authentik) and
        # `compose.observability.yml` (SigNoz, an opt-in overlay) are not what the
        # group deploys either.
        "zeus": ["docker-compose.full.yml"],
    },
    "3-media": {
        "monarch": ["docker-compose.yml"],
        "plutus": ["docker-compose.yml"],
    },
    "4-social": {
        "onyx": ["docker-compose.yml"],
        "rizzaura": ["docker-compose.yml"],
    },
    "5-dev": {
        "atlas": ["docker-compose.yml"],
        "distro": ["docker-compose.yml", "compose.vault.yml"],
        "oasis": ["docker-compose.yml"],
        "olympus": ["docker-compose.yml", "compose.gateway-sso.yml", "compose.vault.yml"],
    },
}

# ── the name only one repo may keep ──────────────────────────────────────────
# A service two member repos declare where the group runs **one**. Which copy
# runs is the deployment decision this generator refuses to guess (see the
# docstring), so the decision is recorded here — once, reviewable, with the
# measurement that settled it. The named repo keeps the plain name; every other
# repo's copy is dropped, and the run says so.
#
# A pin that stops being true (the owner no longer declares the name, or only
# one repo does) is a warning, not a silent no-op.
# Empty today: the one case that needed it — OmniRoute, which both capstone and
# zeus's *standalone* compose declare, each publishing host 20128 — is settled by
# `SOURCES` instead (the voice host runs zeus's full stack, which declares no
# gateway, and capstone's copy is the one running as `omniroute`). Kept as a table
# because the next one will want it, with the stale-pin warning below as its guard.
DEDUPE: dict[str, dict[str, str]] = {}

# A URL carrying a service name as its host: `redis://redis:6379`.
URL_HOST = re.compile(r"(?<=//)([A-Za-z0-9][A-Za-z0-9_.-]*)(?=[:/])")
# `depends_on: [a, b]` / `network_mode: service:a` / `container_name: x`.
DEPENDS_INLINE = re.compile(r"^(\s*depends_on:\s*)\[(.*)\]\s*$")
SERVICE_REF = re.compile(r"^(\s*-\s*)([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$")
NETWORK_MODE = re.compile(r"^(\s*network_mode:\s*service:)([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$")
CONTAINER_NAME = re.compile(r"^(\s*container_name:\s*)\S+\s*$")
TOP_KEY = re.compile(r"^([A-Za-z0-9_.-]+):")
CHILD_KEY = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):")


class CannotRun(Exception):
    """The input does not describe a group — exit 2, not a drift report."""


# ── reading a compose file as blocks ──────────────────────────────────────────

class Compose:
    """One compose file: its top-level sections, and its service bodies.

    Read by indentation rather than with a YAML library: every file here is
    hand-written and comment-dense, comments have to survive into the generated
    file (they are most of why it is readable), and this repo ships no YAML
    dependency. A service body ends at the next `  key:` line or a top-level key.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = ""                      # top-level `name:` (project name)
        self.services: dict[str, list[str]] = {}
        self.sections: dict[str, list[str]] = {}   # other top-level sections
        self._read()

    def _read(self) -> None:
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        section: str | None = None
        service: str | None = None
        for line in lines:
            top = TOP_KEY.match(line)
            if top:
                key = top.group(1)
                if key == "services":
                    section, service = "services", None
                    continue
                if key == "name":
                    self.name = line.split(":", 1)[1].strip()
                    section, service = None, None
                    continue
                section = key
                service = None
                self.sections.setdefault(key, []).append(line)
                continue
            if section == "services":
                child = CHILD_KEY.match(line)
                if child:
                    service = child.group(1)
                    self.services[service] = []
                    continue
                if service is not None:
                    self.services[service].append(line)
                continue
            if section is not None:
                self.sections[section].append(line)


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


# ── one source's contribution ─────────────────────────────────────────────────

class Contribution:
    """A member repo's services, renamed and with its paths made portable."""

    def __init__(self, group: str, repo: str, compose: Compose) -> None:
        self.group = group
        self.repo = repo
        self.compose = compose
        self.renamed: dict[str, str] = {}
        # Names this repo declares that the group's DEDUPE table gives to another
        # repo — declared here, not emitted, and reported as dropped.
        self.dropped: set[str] = set()
        # YAML anchor alias this repo's `x-*` sections define → the alias the
        # generated file uses. Empty unless two repos reuse the same alias.
        self.aliases: dict[str, str] = {}
        self.anchor_sections: list[tuple[str, str]] = []   # (alias, x-key)

    @property
    def rel(self) -> str:
        """The repo's directory relative to the generated file's directory."""
        return f"../../{self.group}/{self.repo}"

    def emit(self, name: str, new_name: str, body: list[str]) -> list[str]:
        """One service body, with the repo's own names and paths corrected."""
        def repl(line: str) -> str:
            for alias, emitted in self.aliases.items():
                if alias != emitted:
                    line = re.sub(rf"\*{re.escape(alias)}(?![\w-])", f"*{emitted}", line)
            match = CONTAINER_NAME.match(line)
            if match:
                # The repo's own container name travels verbatim: it is what the
                # host actually runs, and what every script, doc and Consul
                # registration here names (`cerulean`, `jellyfin`, `zeus-portal`,
                # `omniroute`). The old hand-written group files invented a
                # `g<N>-` prefix instead, which matched no container anywhere —
                # recorded in `docs/stack-migration-gaps.md` under the gateway's
                # host split. Only a service this generator had to rename (a name
                # two repos both declare) gets a new name, because two containers
                # cannot share one.
                if new_name != name:
                    return match.group(1) + new_name
                return line
            match = NETWORK_MODE.match(line)
            if match:
                return match.group(1) + self.renamed.get(match.group(2), match.group(2))
            match = DEPENDS_INLINE.match(line)
            if match:
                items = [self.renamed.get(i.strip(), i.strip())
                         for i in match.group(2).split(",") if i.strip()]
                return f"{match.group(1)}[{', '.join(items)}]"
            match = SERVICE_REF.match(line)
            if match and self._in_depends_on:
                return match.group(1) + self.renamed.get(match.group(2), match.group(2))
            line = URL_HOST.sub(lambda m: self.renamed.get(m.group(1), m.group(1)), line)
            return fix_paths(line, self.rel, self._section)

        out: list[str] = []
        self._in_depends_on = False
        self._section = ""
        for line in body:
            stripped = line.strip()
            # The body's own keys sit at four spaces, and which one a line is
            # under decides whether a path in it is the host's or the container's
            # (`command: ./manage.py` is not a host path).
            key = re.match(r"^ {4}([A-Za-z0-9_.-]+):", line)
            if key:
                self._section = key.group(1)
                self._in_depends_on = self._section == "depends_on"
            out.append(repl(line))
        return out

    def renamed_section(self, name: str) -> str:
        return self.renamed.get(name, name)


def number_of(group: str) -> str:
    match = GROUP_NUMBER.match(group)
    if not match:
        raise CannotRun(f"group directory {group!r} does not start with a number")
    return match.group(1)


# A value that is a *path* rather than a scalar: `./x`, `../x`, `x/y.conf`,
# `.env.prod`. Deliberately strict — `5`, `true` and `30s` are values a
# healthcheck carries, and rewriting one of those into a path is the mistake this
# pattern exists to avoid.
PATHISH = re.compile(r"^([\"']?)(\.{1,2}/[A-Za-z0-9_][A-Za-z0-9_./-]*"
                     r"|\.?[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+"
                     r"|\.[A-Za-z0-9_][A-Za-z0-9_.-]*)\1$")

# Compose resolves these keys' values against the *file* that declares them, so
# in `groups/` they have to be rewritten: `build: .` built the group's own
# directory before this was handled — 30-odd services across the estate — and
# `context: .` did the same inside a `build:` mapping.
FILE_RELATIVE_KEYS = ("build", "context", "env_file", "extends", "file", "device")
# …and these against `build.context` instead, so a value here travels untouched:
# `dockerfile: apps/api/Dockerfile` means "inside the context", and prefixing it
# with the repo's path double-resolves it, which is how the first version of this
# function broke every build that named a Dockerfile in a subdirectory.
CONTEXT_RELATIVE_KEYS = ("dockerfile",)
# Sections whose values belong to the container, not the host: `./manage.py` in a
# command is inside the image, `working_dir` is a container path, and an
# `environment:` entry is a variable. Nothing here is a host path, so a path-shaped
# value in one is left exactly as the repo wrote it.
CONTAINER_SECTIONS = ("command", "entrypoint", "healthcheck", "working_dir",
                      "environment", "labels", "user", "hostname", "ulimits")
# The keys an `env_file:`/`secrets:`/`volumes:` list item can carry its path under.
LISTED_PATH_KEYS = ("path", "file", "source", "device", "context")


def under_repo(rel: str, token: str) -> str:
    """`rel` itself for `.`, `rel/..` for `..` — the repo dir, spelled out."""
    return rel if token == "." else f"{rel}/.."


def fix_paths(line: str, rel: str, section: str = "") -> str:
    """Rewrite the relative paths in one line to the declaring repo's location.

    Compose resolves a relative host path against the file that declares it, so
    the same line means something different in `groups/` than it did in the repo:
    a bind mount (`- ./data:/data`), a build context (`build: .`), an `env_file`
    (`env_file: .env`, or `- .env` in its list), a config/secret's `file:`. Every
    one of those is rewritten to the repo; a `dockerfile:` is not (it is relative
    to its context), and neither is anything inside `command:`, `entrypoint:` or
    `environment:` (that is the container's filesystem and its own words).
    """
    stripped = line.strip()
    if stripped.startswith("#") or "://" in line:
        return line
    if section in CONTAINER_SECTIONS:
        return line

    def collapse(text: str) -> str:
        return text.replace(f"{rel}/./", f"{rel}/")

    # A bind mount or a list item holding an explicit relative path:
    # `- ./x:/y`, `- ../x:/y`, `./x/y.conf:/etc/y.conf:ro`.
    for token in re.findall(r"(?:^|[\s:-])(\.{1,2}/[^:\s\"']+)", stripped):
        line = line.replace(token, f"{rel}/{token}", 1)

    # A key and its value: `build: .`, `context: ./apps/x`, `env_file: .env`. A
    # key compose does not resolve against this file keeps its value as it was.
    pair = re.match(r"^(\s*)([A-Za-z0-9_.-]+):\s*(\S+?)\s*$", line)
    if pair and pair.group(2) in FILE_RELATIVE_KEYS:
        key, value = pair.group(2), pair.group(3)
        if value in (".", ".."):
            return collapse(line.replace(
                pair.group(0), f"{pair.group(1)}{key}: {under_repo(rel, value)}", 1))
        if not value.startswith(("/", "$", "<", "~")) and PATHISH.match(value):
            return collapse(line.replace(
                pair.group(0), f"{pair.group(1)}{key}: {rel}/{value}", 1))
        return collapse(line)

    # A list item: `- .`/`- ..` (a bind mount of the repo itself), `- .env`
    # (an `env_file:` entry), or `- path: .env` (the mapping form of one). The
    # key lives on another line for the first two, so the value has to carry the
    # decision — a dotfile or a relative path, which is what PATHISH is for.
    # `- .:/app` / `- ..:/src`: a bind mount of the repo directory itself, where
    # the value is not a path but a mount source with a target after it.
    bare_mount = re.match(r"^(\s*-\s*)(\.{1,2})(?=\s*:)", line)
    if bare_mount:
        return collapse(line.replace(
            bare_mount.group(0),
            bare_mount.group(1) + under_repo(rel, bare_mount.group(2)), 1))

    item = re.match(r"^(\s*-\s*)(?:(?P<key>" + "|".join(LISTED_PATH_KEYS)
                       + r"):\s*)?(?P<value>\S+?)\s*$", line)
    if item:
        value = item.group("value")
        # `- path: .env` is the mapping form, and the key has to survive the
        # rewrite: dropping it leaves `required: false` under a scalar, which
        # YAML refuses (`mapping values are not allowed in this context`).
        prefix = f"{item.group('key')}: " if item.group("key") else ""
        if value in (".", ".."):
            return collapse(line.replace(
                item.group(0),
                f"{item.group(1)}{prefix}{under_repo(rel, value)}", 1))
        if not value.startswith(("/", "$", "<", "~", "./", "../")) \
                and PATHISH.match(value):
            return collapse(line.replace(
                item.group(0), f"{item.group(1)}{prefix}{rel}/{value}", 1))

    return collapse(line)


# ── generation ────────────────────────────────────────────────────────────────

def generate(group: str) -> tuple[str, list[str], list[str]]:
    """The group's compose text, plus the warnings to print about it."""
    number = number_of(group)
    members = SOURCES.get(group)
    if not members:
        raise CannotRun(f"no SOURCES entry for {group} — add one when the group exists")
    warnings: list[str] = []
    notes: list[str] = []

    contributions: list[Contribution] = []
    for repo, files in members.items():
        repo_dir = ROOT_DIR / group / repo
        base: Compose | None = None
        for file in files:
            path = repo_dir / file
            if not path.exists():
                warnings.append(f"{group}/{repo}/{file} does not exist")
                continue
            compose = Compose(path)
            if not compose.services:
                continue
            if base is None:
                base = compose
                continue
            # A member repo deployed as *several* files is an override pair —
            # `docker compose -f docker-compose.prod.yml -f
            # docker-compose.override.prod.yml up -d` — so the later file's
            # services merge into the earlier ones the way compose merges them.
            # Dropping the later file instead (what this did first) described a
            # stack nobody runs: Signara's override is what fixes the API
            # healthcheck and gives minio its public port.
            for name, body in compose.services.items():
                if name not in base.services:
                    base.services[name] = body
                    continue
                base.services[name], changed = merge_service(base.services[name], body)
                if changed:
                    notes.append(f"{repo}: {name} in {file} merges into the earlier "
                                 f"declaration ({', '.join(changed)})")
            # A section the base file does not carry comes along — the later file
            # can define these too (`x-*` anchors, a network, a volume). One
            # already present is not merged: the first file's copy stays, so an
            # anchor is never defined twice.
            for key, section in compose.sections.items():
                base.sections.setdefault(key, section)
        if base is not None:
            contributions.append(Contribution(group, repo, base))

    # Which names more than one repo declares — those get their repo's prefix.
    owners: dict[str, list[Contribution]] = {}
    for contribution in contributions:
        for name in contribution.compose.services:
            owners.setdefault(name, []).append(contribution)
    for name, claimants in sorted(owners.items()):
        if len(claimants) < 2:
            continue
        owner = DEDUPE.get(group, {}).get(name)
        if owner:
            # One copy runs; the rest are dropped rather than prefixed, because
            # keeping them would put two services on the same host port.
            keepers = [c for c in claimants if c.repo == owner]
            for contribution in claimants:
                if contribution not in keepers:
                    contribution.dropped.add(name)
            notes.append(f"name '{name}' is declared by "
                         + " and ".join(c.repo for c in claimants)
                         + f" — the group runs {owner}'s copy only (DEDUPE: one "
                         + f"{name} per host); "
                         + ", ".join(c.repo for c in claimants if c is not keepers[0])
                         + " is dropped")
            continue
        for contribution in claimants:
            contribution.renamed[name] = f"{contribution.repo}-{name}"
        notes.append(f"name '{name}' is declared by "
                     + " and ".join(c.repo for c in claimants)
                     + " — both are kept, prefixed per repo")

    # A DEDUPE pin that stopped being true: the name is no longer declared twice,
    # or the owner does not declare it at all. Both mean the entry is lying.
    for name, owner in sorted(DEDUPE.get(group, {}).items()):
        claimants = owners.get(name, [])
        if len(claimants) < 2:
            warnings.append(f"DEDUPE: '{name}' is pinned to {owner}'s copy, but "
                            f"{'no' if not claimants else 'only one'} member repo "
                            f"declares it — the entry is stale")
        elif owner not in {c.repo for c in claimants}:
            warnings.append(f"DEDUPE: '{name}' is pinned to {owner}, which does not "
                            f"declare it — the entry is wrong")

    extras = read_extras(group)
    extra_services = extras["services"]

    # The extras are hand-written, and every relative path in them is relative to
    # `groups/` — the directory the generated file is read from, one level below
    # the repos. A path written as the *old* group file had it (`build: ./oasis`,
    # relative to the group dir) resolves to nothing here, and Docker would make
    # a directory out of it instead of failing, so it is checked rather than
    # trusted.
    for name, body in extra_services.items():
        for line in body:
            found = re.match(r"^\s*(?:-\s*|(?:context|dockerfile|env_file|file|device):\s*)(\.{1,2}/[^:\s#]+)", line)
            if not found:
                continue
            target = (OUT_DIR / found.group(1)).resolve()
            if not target.exists():
                warnings.append(f"extras/{group}.yml: {name} points at "
                                f"'{found.group(1)}', which does not exist from "
                                f"groups/ — a path in the extras is relative to "
                                f"that directory, not to the group dir")

    # A group-own service wins the plain name: the repo's copy takes its prefix.
    for name, claimants in owners.items():
        if name in extra_services:
            for contribution in claimants:
                contribution.renamed[name] = f"{contribution.repo}-{name}"
            notes.append(f"'{name}' is a group-owned service — the repo copy is "
                         f"prefixed")

    for name in extra_services:
        if name in owners:
            warnings.append(f"extras/{group}.yml declares '{name}', which a member "
                            f"repo already declares — a group-own copy of a repo "
                            f"service is the duplication this generator removes")

    lines: list[str] = [
        "# ══════════════════════════════════════════════════════════════════════════",
        f"# Group {number} — {group.split('-', 1)[1].upper()}",
        "# GENERATED — do not edit. `ips/scripts/gen-group-compose.py` builds this",
        "# file from its member repos; edit those, or the group's own wiring in",
        "# `ips/groups/extras/" + group + ".yml`, and regenerate.",
        "# `scripts/gen-group-compose.py --check` runs in CI.",
        "# ══════════════════════════════════════════════════════════════════════════",
        "#",
        "# Sources:",
    ]
    for repo, files in members.items():
        lines.append(f"#   {group}/{repo}/" + ", ".join(files))
    lines.append("")
    lines.append(f"name: innotel-group{number}")
    lines.append("")

    # ── x-* anchors ──
    # Services reference the anchors their own file defines (`logging: *logging-default`),
    # and an anchor only exists in the file that declares it, so every `x-*`
    # section has to travel with its services. Two repos that pick the same alias
    # (`sso-gateway`) get their repo's prefix, on the alias and on every use.
    # One entry per `x-*` section, and the aliases it defines — a section can
    # define more than one (`x-sso-gateway` also defines `sso-gateway-env`), and
    # it must still be emitted once.
    anchor_sections: list[tuple[Contribution, str, list[str], list[str]]] = []
    anchor_users: dict[str, list[Contribution]] = {}
    for contribution in contributions:
        for key, body in contribution.compose.sections.items():
            if not key.startswith("x-"):
                continue
            aliases = re.findall(r"&([A-Za-z0-9_.-]+)", "\n".join(body))
            if not aliases:
                continue
            anchor_sections.append((contribution, key, list(body), aliases))
            for alias in aliases:
                anchor_users.setdefault(alias, []).append(contribution)

    # An alias two repos both use — or one repo defines twice with different
    # bodies — gets the repo's prefix, on the definition and on every use.
    renamed_alias: dict[tuple[str, str], str] = {}
    for alias, users in anchor_users.items():
        bodies = {tuple(body) for _, _, body, aliases in anchor_sections
                  if alias in aliases}
        if len(users) > 1 or len(bodies) > 1:
            for contribution in users:
                renamed_alias[(contribution.repo, alias)] = f"{contribution.repo}-{alias}"
            notes.append(f"anchor '*{alias}' is defined by "
                         + " and ".join(sorted({c.repo for c in users}))
                         + " — each is renamed with its repo")

    for contribution, _key, body, aliases in anchor_sections:
        for alias in aliases:
            emitted = renamed_alias.get((contribution.repo, alias))
            contribution.aliases[alias] = emitted or alias
            if emitted:
                body = [re.sub(rf"&{re.escape(alias)}(?![\w-])", f"&{emitted}", line)
                        for line in body]
        lines.extend(body)
        lines.append("")

    # ── services ──
    lines.append("services:")
    claimed: dict[str, str] = {}          # emitted name -> where it came from
    for contribution in contributions:
        for name, body in contribution.compose.services.items():
            if name in contribution.dropped:
                continue
            new_name = contribution.renamed.get(name, name)
            if new_name in claimed:
                notes.append(f"{contribution.repo}:{name} would emit '{new_name}', "
                             f"already taken by {claimed[new_name]} — skipped")
                continue
            claimed[new_name] = f"{contribution.repo}:{name}"
            profile = profile_of(body)
            suffix = f"   # from {contribution.repo} (profiles: {profile})" if profile \
                else f"   # from {contribution.repo}"
            lines.append(f"  {new_name}:{suffix}")
            lines.extend(contribution.emit(name, new_name, body))
            lines.append("")

    emitted_names = set(claimed)
    for name, body in extra_services.items():
        if name in claimed:
            notes.append(f"extras:{name} collides with {claimed[name]} — skipped")
            continue
        lines.append(f"  {name}:   # group-owned (extras/{group}.yml)")
        lines.extend(body)
        lines.append("")
        emitted_names.add(name)

    # A group-own service that depends on a repo service has to name it as the
    # generated file does (`oasis-mail` depends on oasis's `postgres`, which keeps
    # its plain name because nothing else in the group claims it).
    for name, body in extra_services.items():
        for target in depends_on_of(body):
            if target not in emitted_names:
                warnings.append(f"extras/{group}.yml: {name} depends on '{target}', "
                                f"which this group does not emit — the extras need "
                                f"the name as the repos declare it")

    # ── volumes, networks, other top-level sections ──
    volumes: dict[str, list[str]] = dict(extras["volumes"])
    networks: dict[str, list[str]] = dict(extras["networks"])
    for contribution in contributions:
        for key, target in (("volumes", volumes), ("networks", networks)):
            for name, body in parse_children(contribution.compose.sections.get(key, [])):
                if name in target:
                    continue
                target[name] = body

    if volumes:
        lines.append("volumes:")
        for name, body in volumes.items():
            if body and any(line.strip() for line in body):
                lines.append(f"  {name}:")
                lines.extend(body)
            else:
                lines.append(f"  {name}:")
        lines.append("")

    lines.append("networks:")
    for name, body in (networks or {"default": [], "innotel-mesh-net": []}).items():
        lines.append(f"  {name}:")
        lines.extend(body)

    text = "\n".join(lines).rstrip() + "\n"
    port_warnings, port_notes = port_collisions(text)
    warnings.extend(port_warnings)
    notes.extend(port_notes)
    return text, warnings, notes


def depends_on_of(body: list[str]) -> list[str]:
    """The service names a service body's `depends_on` lists (not mapping form)."""
    names: list[str] = []
    inside = False
    for line in body:
        if re.match(r"^\s+depends_on:\s*$", line):
            inside = True
            continue
        if inside:
            item = re.match(r"^\s+-\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$", line)
            if item:
                names.append(item.group(1))
                continue
            if re.match(r"^\s{4}[A-Za-z]", line):
                inside = False
    return names


def anchor_name(section: list[str]) -> str:
    """The alias a `x-name: &alias` section defines, or '' when it defines none."""
    match = re.match(r"^[A-Za-z0-9_.-]+:\s*&([A-Za-z0-9_.-]+)\s*$", section[0]) if section else None
    return match.group(1) if match else ""


def profile_of(body: list[str]) -> str:
    for line in body:
        match = re.match(r"^\s{4}profiles:\s*\[(.*?)\]\s*$", line)
        if match:
            return match.group(1).strip()
    return ""


def parse_children(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Two-space-indented children of a top-level section (`volumes:`/`networks:`)."""
    children: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        child = CHILD_KEY.match(line)
        if child:
            current = []
            children.append((child.group(1), current))
            continue
        if current is not None and line.strip():
            current.append(line)
    return children


def read_extras(group: str) -> dict[str, dict[str, list[str]]]:
    """The group's own wiring, or nothing when it has none yet."""
    path = EXTRAS_DIR / f"{group}.yml"
    if not path.exists():
        return {"services": {}, "volumes": {}, "networks": {}}
    compose = Compose(path)
    return {
        "services": compose.services,
        "volumes": dict(parse_children(compose.sections.get("volumes", []))),
        "networks": dict(parse_children(compose.sections.get("networks", []))),
    }


# One entry of a `ports:` list, as written: `"3478:3478/udp"`,
# `"10.0.0.1:3478:3478"`, `"${PORT:-5060}:${PORT:-5060}"`.
PORT_ITEM = re.compile(r'^\s*-\s*"?(?P<entry>[^"#]+?)"?\s*$')
# A port as written, literal or as a variable's default: `5060`, `${PORT:-5060}`.
PORT_TOKEN = re.compile(r"^(?:\$\{[A-Za-z0-9_]+:-)?(\d{2,5})\}?$")
# A bind address as written, literal or as a variable's default.
IP_TOKEN = re.compile(r"^(?:\$\{[A-Za-z0-9_]+:-)?((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z][\w.-]*)\}?$")
# A `ports:` spec's parts, one per colon — but not the colons inside a variable's
# default (`${FREEPBX_SIP_PORT:-5060}` is one part, not two).
PORT_PARTS = re.compile(r"\$\{[^}]*\}|[^:]+")


def port_token(token: str) -> str | None:
    """The port a token names, or None when it is not one this can compare."""
    match = PORT_TOKEN.match(token.strip())
    return match.group(1) if match else None


def published_on(entry: str) -> tuple[str, str, str] | None:
    """What a `ports:` entry publishes: (host address, port, protocol), or None.

    None for an entry a collision cannot be decided from: the short form
    (`"8080"`) takes a random host port, which is free by construction, and a
    range (`"8000-8010"`) binds several. The address is `0.0.0.0` when the entry
    names none, so a wildcard publish reads as colliding with an address-bound
    one — `"80:80"` against `"127.0.0.1:80:80"` is one host port to Docker —
    while two different bound addresses (`127.0.0.1:20128` and
    `172.17.0.1:20128`, both correct on one host for the same container) do not.
    """
    spec, _, proto = entry.partition("/")
    proto = proto.strip().lower() or "tcp"
    parts = [part.strip().strip("'\"") for part in PORT_PARTS.findall(spec)]
    if len(parts) < 2:
        return None
    host = port_token(parts[-2])
    if host is None:
        return None
    if len(parts) >= 3:
        bound = IP_TOKEN.match(parts[0])
        address = bound.group(1) if bound else "0.0.0.0"
    else:
        address = "0.0.0.0"
    return address, host, proto


# Keys compose *appends* to when a later file declares them again — its own
# merge rule for lists. An override that adds a published port adds it, and one
# that adds a profile adds it.
APPEND_KEYS = ("ports", "expose", "volumes", "environment", "env_file", "depends_on",
               "networks", "dns", "dns_search", "extra_hosts", "labels", "profiles",
               "cap_add", "cap_drop", "security_opt", "devices", "tmpfs", "command")


def blocks_of(body: list[str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """A service body split into its four-space children, plus any leading lines."""
    blocks: list[tuple[str, list[str]]] = []
    leading: list[str] = []
    for line in body:
        key = re.match(r"^ {4}([A-Za-z0-9_.-]+):", line)
        if key:
            blocks.append((key.group(1), [line]))
        elif blocks:
            blocks[-1][1].append(line)
        else:
            leading.append(line)
    return blocks, leading


def merge_service(earlier: list[str], later: list[str]) -> tuple[list[str], list[str]]:
    """Compose's merge for one service declared by two files: later file wins.

    A key in `APPEND_KEYS` is a list, and compose appends to it, so the later
    file's items are added under the earlier key line. Everything else replaces
    the earlier block — which is what an override means when it restates a
    `healthcheck:`. A mapping-valued key that both files carry is merged key by
    key instead (a duplicate YAML key would not even parse), so an override can
    add one variable without losing the rest.
    """
    blocks, leading = blocks_of(earlier)
    position = {key: index for index, (key, _) in enumerate(blocks)}
    changed: list[str] = []
    for key, lines in blocks_of(later)[0]:
        if key not in position:
            position[key] = len(blocks)
            blocks.append((key, lines))
            changed.append(key)
            continue
        index = position[key]
        if key in APPEND_KEYS:
            blocks[index] = (key, extend_block(blocks[index][1], lines))
        else:
            blocks[index] = (key, lines)
        changed.append(key)
    merged: list[str] = list(leading)
    for _key, lines in blocks:
        merged.extend(lines)
    return merged, changed


def extend_block(earlier: list[str], later: list[str]) -> list[str]:
    """Append a later file's items to an earlier block, key by key for a map.

    `ports:` and `profiles:` are lists: the items are appended. An `environment:`
    is usually a map, and appending its keys would leave two of the same name in
    one YAML mapping, which compose refuses to parse — so a matching key is
    replaced and a new one is appended.
    """
    if is_mapping(later):
        kept = list(earlier)
        where = {}
        for index, line in enumerate(kept):
            key = re.match(r"^\s+([A-Za-z0-9_.-]+):", line)
            if key:
                where[key.group(1)] = index
        for line in later[1:]:
            key = re.match(r"^\s+([A-Za-z0-9_.-]+):", line)
            if key and key.group(1) in where:
                kept[where[key.group(1)]] = line
            else:
                kept.append(line)
        return kept
    return earlier + [line for line in later[1:] if line.strip()]


def is_mapping(block: list[str]) -> bool:
    """Is this block a mapping (`FOO: bar`) rather than a list (`- FOO=bar`)?"""
    for line in block[1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        return bool(re.match(r"^\s+-\s", line)) is False
    return False


def port_collisions(text: str) -> tuple[list[str], list[str]]:
    """Host ports two services publish in the generated file — the merge's risk.

    Returns the findings that are real on a host running the file's always-on
    services, and the same for services behind a profile, which is a note: a
    profile has to be enabled before the port exists.

    Neither repo is wrong on its own; on one host only one of them can bind
    (`coturn` on 3478 is declared by both capstone and zeus, and both publish
    it). Which one owns it is a deployment decision the repos cannot make, so
    this reports the pair instead of picking.

    Read from the generated text, which is why it catches the shape the repos
    hide: a variable's default (`"${FREEPBX_SIP_PORT:-5060}:5060"`) publishes
    5060 on a clean host, and two stacks defaulting to one port is exactly what
    nobody notices until the second `up`.
    """
    # A profile-gated service is opt-in, so a port it publishes only exists on a
    # host that enabled that profile: the finding is worth stating and not worth
    # failing on.
    gated = {match.group(1) for match in re.finditer(
        r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):.*?(?=^  [A-Za-z0-9]|\Z)", text, re.M | re.S)
        if re.search(r"^    profiles:", match.group(0), re.M)}

    binds: list[tuple[str, str, str, str]] = []      # service, address, port, proto
    service = ""
    in_ports = False
    for line in text.splitlines():
        child = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):", line)
        if child:
            service = child.group(1)
            in_ports = False
        if re.match(r"^\s+ports:\s*$", line):
            in_ports = True
            continue
        if in_ports and re.match(r"^\s{4}[A-Za-z]", line) and not line.strip().startswith("#"):
            in_ports = False
        if not in_ports:
            continue
        item = PORT_ITEM.match(line)
        if not item:
            continue
        published = published_on(item.group("entry"))
        if published:
            binds.append((service, *published))

    # One finding per port, not per pair: four services on 3000 is one line
    # naming four, and the clusters are worked out the way Docker sees them —
    # a wildcard publish merges with everything on that port, two specific
    # addresses do not collide with each other.
    by_port: dict[tuple[str, str], dict[str, set[str]]] = {}
    for service, address, port, proto in binds:
        by_port.setdefault((port, proto), {}).setdefault(address, set()).add(service)

    findings: set[str] = set()
    notes: set[str] = set()
    for (port, proto), by_address in sorted(by_port.items()):
        wildcard = by_address.pop("0.0.0.0", set())
        clusters = [("0.0.0.0", wildcard)] if wildcard else []
        clusters.extend((address, wildcard | services)
                        for address, services in sorted(by_address.items()))
        for address, services in clusters:
            if len(services) < 2:
                continue
            shown = port if proto == "tcp" else f"{port}/{proto}"
            where = "0.0.0.0 (every address)" if address == "0.0.0.0" else address
            message = (f"host port {shown} is published by "
                       + ", ".join(sorted(services))
                       + f" — {len(services)} services, binding {where}; at most "
                       "one of them can on a host that runs them all")
            if services & gated:
                notes.add(message + " (opt-in: only with the profiles that "
                                    "declare " + ", ".join(sorted(services & gated)) + ")")
                continue
            findings.add(message)
    return sorted(findings), sorted(notes)


# ── writing ───────────────────────────────────────────────────────────────────

def output_path(group: str) -> Path:
    return OUT_DIR / f"{group}.yml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--group", action="append", default=[],
                        help="one group by directory name (repeatable); default all")
    parser.add_argument("--check", action="store_true",
                        help="do not write — fail if a checked-in file is stale")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args()

    # In a lone `ips` checkout there are no group directories beside it, so
    # there is nothing to read and nothing to compare — say so and pass, the way
    # `check-group-compose-drift.py` does, which is what makes both usable as CI
    # steps in this repo. An explicitly named `--group` that is absent is still an
    # error: that is a typo, not a standalone checkout.
    present = [group for group in (args.group or sorted(SOURCES))
               if (ROOT_DIR / group).is_dir()]
    if not present and not args.group:
        print("gen-group-compose: this checkout stands alone — no group directories "
              "beside it to read, nothing to generate")
        return 0
    groups = present
    for group in args.group:
        if group not in present:
            raise CannotRun(f"no group directory {group} under {ROOT_DIR}")

    stale = 0
    for group in groups:
        try:
            text, warnings, notes = generate(group)
        except CannotRun as err:
            print(f"{group}: cannot generate — {err}", file=sys.stderr)
            return 2

        path = output_path(group)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        body = text.split("\nvolumes:", 1)[0].split("\nnetworks:", 1)[0]
        services = len(re.findall(r"^  [A-Za-z0-9][A-Za-z0-9_.-]*:", body, re.M))

        if args.check:
            if current == text:
                status = "current"
            else:
                status = "STALE"
                stale += 1
                diff = list(difflib.unified_diff(
                    current.splitlines(), text.splitlines(),
                    fromfile=str(path), tofile=f"{path} (regenerated)", lineterm=""))
                if not args.quiet:
                    print("\n".join(diff[:60]))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            status = "written"

        if not args.quiet:
            print(f"{group:12s} {status:8s} {path.relative_to(STACK_DIR)} "
                  f"({services} service blocks)")
        for note in notes:
            print(f"    note: {note}")
        for warning in warnings:
            print(f"    WARN: {warning}")

    if args.check and stale:
        print(f"\n{stale} group file(s) are not what the repos say — "
              f"run scripts/gen-group-compose.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CannotRun as error:
        # A `--group` that does not exist is a typo, not a drift report: exit 2,
        # the same code `check-group-compose-drift.py` uses for it.
        print(f"gen-group-compose: {error}", file=sys.stderr)
        sys.exit(2)
