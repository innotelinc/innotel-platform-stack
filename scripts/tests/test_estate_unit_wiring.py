#!/usr/bin/env python3
"""A unit that names another unit must name one that exists.

`systemd/` is not loaded by systemd anywhere in CI — the pieces are installed by hand on
several hosts (`docs/unit-apply-checklist.md`), so a dependency on a misspelling is found
on the host, at the moment something has already failed.

One of those mistakes is written down in `estate-notify@.service` because it was made:
`OnFailure=estate-notify@%n.service` expands to `estate-notify@media-orphans.service.service`,
which systemd will still start — as a unit that does not exist — so the notification is sent
nowhere and the failure looks like a failure nobody reported. `%n` already carries the type
suffix; the template adds it back. That is asserted here as its own case, because the generic
existence check below cannot see it: the template instance does exist.

Everything else is checked by parsing the directives and asking whether the name is one of
this repository's units or one of the units it deliberately depends on but does not ship.
That second list is short and every entry is a claim about where the unit comes from.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "systemd"

#: Directives whose value is a unit name. `Wants`/`Requires`/`After`/`Before` are the
#: ordering and pull-in edges; `OnFailure` is the notification path; `PartOf` and
#: `Conflicts` decide what stops with what; `Unit` is how a timer names its service.
DIRECTIVES = ("After", "Before", "Wants", "Requires", "OnFailure", "PartOf", "Conflicts", "Unit")

#: Units this repository uses but does not ship. Each is installed by something else on
#: the host, which is why it cannot be resolved against `systemd/` — and why adding one
#: here is a statement about where it comes from, not a way to silence a finding.
EXTERNAL = {
    # systemd's own targets: the boot order every unit here joins.
    "network.target",
    "network-online.target",
    "local-fs.target",
    "multi-user.target",
    # The early boot ordering `zswap-enable.service` joins: it must run before anything
    # can swap, so it hooks `sysinit.target` and orders itself ahead of `swap.target`.
    "sysinit.target",
    "swap.target",
    # The container runtime the estate's hosts run (`docs/incus-ceiling-check.md`).
    "incus.service",
    # Inside the `mail` container these run in front of Stalwart, which is the container
    # image's own unit, not one of ours (`docs/mail-sso-directory-2026-11-06.md`).
    "stalwart.service",
    # The host's wifi stack, which `wifi-powersave-off.service` modifies.
    "wpa_supplicant.service",
}


def shipped() -> dict[str, Path]:
    """Every unit file this repository installs, keyed by name."""
    return {
        path.name: path
        for path in sorted(UNITS.iterdir())
        if path.suffix in (".service", ".timer", ".socket", ".target")
    }


def references(path: Path) -> list[tuple[str, str, str]]:
    """`(directive, as written, expanded)` for every unit name a file mentions.

    `%n` is the referring unit's own name, type suffix included; `%i` is its instance, which
    is what `estate-notify@%n` relies on. Nothing else is expanded: a specifier this file
    did not anticipate should surface as an unresolved name rather than be guessed at.
    """
    own = path.name
    instance = own.split("@", 1)[1].split(".", 1)[0] if "@" in own else ""
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        directive, separator, value = line.partition("=")
        if not separator or directive.strip() not in DIRECTIVES or not value.strip():
            continue
        for token in value.split():
            found.append((directive.strip(), token, token.replace("%n", own).replace("%i", instance)))
    return found


def resolve(name: str, available: set[str]) -> str | None:
    """None when the name resolves, otherwise why it does not."""
    if name in EXTERNAL or name in available:
        return None
    if "@" in name:
        template, _, instance = name.partition("@")
        suffix = name.rsplit(".", 1)[-1]
        if instance and f"{template}@.{suffix}" in available:
            return None
        return f"{name} needs the template {template}@.{suffix}, which is not here"
    return f"{name} is neither a unit in systemd/ nor a declared external unit"


class UnitWiringCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = shipped()
        cls.available = set(cls.units)

    def test_the_parser_found_the_units(self):
        # A directory-listing change or a suffix filter that stops matching would make every
        # assertion below pass over an empty set.
        self.assertGreater(len(self.units), 15)
        self.assertIn("estate-checks.service", self.units)

    def test_every_referenced_unit_exists_or_is_declared_external(self):
        failures = []
        for name, path in self.units.items():
            for directive, written, expanded in references(path):
                why = resolve(expanded, self.available)
                if why is not None:
                    failures.append(f"{name}: {directive}={written} — {why}")
        self.assertEqual(failures, [], "a unit names something that does not exist")

    def test_no_unit_writes_the_double_suffix(self):
        # The specific mistake `estate-notify@.service` documents: `%n` already ends in
        # `.service`, so appending it again yields `…@media-orphans.service.service`.
        offenders = []
        for name, path in self.units.items():
            for directive, written, expanded in references(path):
                if directive == "OnFailure" and written.endswith("%n.service"):
                    offenders.append(f"{name}: OnFailure={written} → {expanded}")
        self.assertEqual(offenders, [], "OnFailure must be `estate-notify@%n`, not `%n.service`")

    def test_every_unit_that_opts_into_notify_has_the_template(self):
        opt_in = {
            name
            for name, path in self.units.items()
            if any(directive == "OnFailure" for directive, _, _ in references(path))
        }
        self.assertTrue(opt_in, "no unit opts into notifications; the parser is probably wrong")
        for name in sorted(opt_in):
            for directive, written, expanded in references(self.units[name]):
                if directive != "OnFailure":
                    continue
                self.assertIsNone(resolve(expanded, self.available), f"{name}: {written}")

    def test_timers_point_at_units_that_exist(self):
        # A timer with no `Unit=` binds to the service of the same name; one with a `Unit=`
        # that names something absent is a schedule that starts nothing.
        for name, path in self.units.items():
            if not name.endswith(".timer"):
                continue
            if not any(directive == "Unit" for directive, _, _ in references(path)):
                self.assertIn(
                    name.replace(".timer", ".service"),
                    self.available,
                    f"{name} has no Unit= and no service of its own name",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
