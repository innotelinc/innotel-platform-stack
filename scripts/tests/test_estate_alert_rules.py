#!/usr/bin/env python3
"""The alert rules and the checks they watch must name the same checks.

`EstateCheckStale` and `EstateCheckNotPinned` key on a series, so they can only speak
about a check whose textfile reached node-exporter. `EstateCheckAbsent` is the rule that
catches the other case — a check that never reported at all — and it can only do that
because it lists each known check by name, one `absent(...)` branch each. That list is
the one place the rule set and the checks can silently drift: add a fourth check with no
branch, and its absence is invisible again, which is the failure the whole rule exists to
close (`docs/container-placement.md` §2026-10-01).

So this reads the checks' `CHECK` constants and the rule file, and fails if the two sets
are not the same. It parses the YAML as text on purpose — the point is the presence of a
branch per check, and that is a string, not a structure.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
RULES = ROOT / "extensions/monitoring/prometheus/rules/estate-checks.yml"

sys.path.insert(0, str(SCRIPTS))

#: The scripts that publish the shared metric family, each exporting `CHECK`.
CHECK_SCRIPTS = [
    "check-container-addresses",
    "check-container-limits",
    "check-estate-inventory",
    "check-address-latency",
    "check-host-disk",
    "check-alert-delivery",
    "check-mail-auth",
    "check-mail-relay",
    "check-group-compose-freshness",
    "check-authentik-privilege",
    "check-sso-callbacks",
    "check-dmarc-alignment",
    # The edge's own two invariant checks and the mount table. They were added to the
    # timer on 2026-10-29 without branches here, which is exactly the silence this test
    # exists to catch: a textfile that stops arriving would have alerted on nothing.
    "check-edge-vhost-targets",
    # The pin: a pinned door's target must hold in NPM's *row*, not only in the file NPM
    # regenerates from it, and a name that must stay a redirect must not gain a proxy
    # host. It arrived with the rule branch, because a check whose absence is silent is
    # the failure this list exists to catch.
    "check-edge-vhost-pins",
    "check-edge-sso-door",
    "check-mount-stacks",
    # The mail server's traffic, which is the only source the mail dashboard has.
    "check-mail-traffic",
    # NPM's host list against the vhost files it wrote: a deleted row whose file still
    # proxies, and a row whose port no longer matches the file nginx serves.
    "check-npm-config-drift",
    # Written on the edge on 2026-10-27 after a `docker exec` sat for 18 h 20 m and took
    # i4 to load 13, then left there unowned for six days: the script lived in
    # /opt/innotel/estate-checks and published nothing, so it was the same silence this
    # list exists to catch — a check that exists is not a check that runs.
    "check-stuck-processes",
    # Publishing since before this list was complete, which is why their absence was
    # the silence the rule exists for: a textfile that stops arriving alerts on nothing.
    "check-deploy-door",
    "check-mail-sso",
    "check-stalwart-oidc-cache",
    # The runbook's own preflight, published every half hour (2026-11-14). The check above
    # reads the copy; this one reads the eleven facts the apply depends on — the units, the
    # leaf's SAN against the upstream the proxy actually uses, the anchor, the DNAT rules,
    # whether the provider answers from inside the container, both users' read access and
    # the bearer probe. It arrived with a branch, because a check whose absence is silent
    # is the failure this list exists to catch.
    "check-stalwart-oidc-preflight",
    "check-telno-legacy-import",
    # The mailbox -> identity mirror. It is the one job here that retries every fifteen
    # minutes and is deliberately not wired to `OnFailure=` (96 messages a day for one
    # standing cause), so its verdict has to be a series or it is a journal line the next
    # run overwrites — which is why it arrived with a branch rather than after one.
    "check-mail-identities",
    # The vault pair: one asks whether the path is the vault, the other whether the disk
    # under it is still trustworthy. Both arrived with a branch, because a check whose
    # textfile stops arriving must not read as a quiet green.
    "check-vault-mount",
    "check-vault-disk",
    # The Orion NAS door: the proxy's issuer, that issuer's discovery and the redirect the
    # door emits are three statements of one fact, and its failure mode is a 500 at the
    # callback that a bogus code produces too. It arrived with a branch.
    "check-orion-signin",
    # And the one that was missing for two months, found by the test below the first time it
    # ran: `check-media-doors.py` has published `media_doors` into the family since the media
    # estate collapsed onto two names, and had no branch — so the check could have stopped
    # reporting entirely and `EstateCheckAbsent` would not have named it. The list being
    # typed by hand is what let that happen; the test is what stops the next one.
    "check-media-doors",
]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _block(text: str, alert: str) -> str:
    """The text of the `- alert: <alert>` rule, up to the next `- alert:` or the end."""
    start = re.search(rf"^      - alert: {re.escape(alert)}\n", text, re.MULTILINE)
    if start is None:
        raise AssertionError(f"no rule named {alert} in {RULES.name}")
    tail = text[start.end():]
    end = re.search(r"^      - alert: ", tail, re.MULTILINE)
    return tail[: end.start()] if end else tail


class RuleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checks = {_load(name).CHECK for name in CHECK_SCRIPTS}
        cls.text = RULES.read_text(encoding="utf-8")

    def test_there_is_an_absent_rule(self):
        self.assertIn("absent(", _block(self.text, "EstateCheckAbsent"))

    def test_every_check_has_an_absent_branch(self):
        block = _block(self.text, "EstateCheckAbsent")
        named = set(re.findall(r'check="([^"]+)"', block))
        self.assertEqual(
            named,
            self.checks,
            "every check must be named in EstateCheckAbsent, or its absence is silent again",
        )

    def test_no_absent_branch_names_a_check_that_does_not_exist(self):
        # The other direction: a removed check leaves a branch that can never fire.
        named = set(re.findall(r'check="([^"]+)"', _block(self.text, "EstateCheckAbsent")))
        self.assertEqual(named - self.checks, set())

    def test_every_script_that_publishes_the_family_is_listed_here(self):
        # The tests above are exact about the two things CHECK_SCRIPTS feeds them — the
        # `CHECK` constants and the rule's branches — and blind to the list itself, which
        # is typed by hand. So a new `check-*.py` could publish a series that nothing can
        # miss, which is the same silence one level up. Membership in the family is not a
        # matter of remembering: `estate_check.write_prom` is what makes a textfile into
        # an `innotel_estate_check_*` series, so importing it is the declaration.
        publishers = {
            path.stem
            for path in sorted(SCRIPTS.glob("check-*.py"))
            # `check-edge-liveness.py` publishes on its own channel by design (it is meant
            # to run on a host that is not the edge, so it cannot depend on the edge's
            # textfile directory); it names no `write_prom` call, which is what keeps this
            # assertion honest rather than special-cased.
            if "write_prom" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(
            sorted(publishers - set(CHECK_SCRIPTS)),
            [],
            "a script that publishes innotel_estate_check needs a line in CHECK_SCRIPTS, "
            "and therefore a branch in EstateCheckAbsent",
        )

    def test_no_two_scripts_claim_one_check_name(self):
        # Everything here is keyed on the label value, so two scripts that declare the same
        # `CHECK` are one series: whichever wrote its textfile last would supply both
        # verdicts, and one door going dark would read as the other one's green. A
        # copy-paste is how this happens, and nothing else in the estate would notice it.
        names = [_load(name).CHECK for name in CHECK_SCRIPTS]
        shared = sorted({name for name in names if names.count(name) > 1})
        self.assertEqual(shared, [], "two checks publishing one series")

    def test_every_listed_check_is_run_by_the_unit(self):
        # And the last direction: a name in this list with no ExecStart passes the branch
        # test while the check never runs, so its branch can never fire either. The unit
        # is the authority for what runs; the dashboard's reporting count is derived from
        # the same file and has its own test.
        unit = (ROOT / "systemd" / "estate-checks.service").read_text(encoding="utf-8")
        run = set(re.findall(r"estate-checks/(check-[a-z0-9-]+)\.py", unit))
        self.assertEqual(
            sorted(set(CHECK_SCRIPTS) - run),
            [],
            "every check in CHECK_SCRIPTS must have an ExecStart, or its absence is silent",
        )

    def test_each_branch_keeps_the_check_label(self):
        # Without label_replace the whole rule fires as one anonymous series, and the
        # annotation cannot say which check went missing.
        block = _block(self.text, "EstateCheckAbsent")
        self.assertEqual(block.count("label_replace("), len(self.checks))
        # The summary is what a reader sees first, so it has to carry the name too.
        self.assertIn("{{ $labels.check }}", block)

    def test_the_other_rules_reference_the_shared_family(self):
        for alert in ("EstateCheckFailing", "EstateCheckCouldNotRun", "EstateCheckStale", "EstateCheckNotPinned"):
            self.assertIn("innotel_estate_check", _block(self.text, alert), alert)

    def test_the_stale_window_is_a_whole_day(self):
        # The checks run every 30 minutes, so 24h is the point at which "not one clean run
        # for a whole day" is unambiguous. Locked here so a later edit cannot quietly
        # widen the window back to a day-and-change.
        block = _block(self.text, "EstateCheckStale")
        self.assertRegex(block, r">\s*24\s*\*\s*60\s*\*\s*60")
        self.assertIn("24 hours", block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
