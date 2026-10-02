#!/usr/bin/env python3
"""End-to-end tests for the estate checks' live path — the run, not just the decision.

`test_check_container_addresses.py`, `test_check_container_limits.py` and
`test_check_estate_inventory.py` cover the *decision*: given observations, do the right
findings come out. These cover the path that carries the decision — parsing `--hosts`,
reaching each host, turning what came back into observations, writing the textfile the
alerts read, and the exit code systemd and CI look at. A check whose `audit()` is
covered but whose `main()` is not can still pass silently on a broken estate, and
2026-10-01 was exactly a check-shaped silence: `ontrak` renumbered and nothing failed.

So each check is run end to end here against a **deliberately broken** estate, and
asserted to (a) exit non-zero the check's docstring promises and (b) write a textfile
that says so. It is run clean too, so a check that fails on everything is not what the
green case proves.

`ssh` is a stub earlier on `PATH`, answering from a JSON rules file; nothing here
touches a real host, and no credentials are involved.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

#: A stand-in for `ssh`: argv[-2] is the host, argv[-1] the command. It answers from the
#: JSON named by ESTATE_SSH_STUB — `{"hosts": {"<addr>": {"answers": [[keyword, output]],
#: "fail": bool}}}` — first keyword contained in the command wins. An unknown host, or a
#: host marked `fail`, exits non-zero, which is how an unreachable host reads.
STUB = '''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
if len(argv) < 2:
    sys.exit(255)
host, command = argv[-2], argv[-1]
host = host.split("@", 1)[-1]  # the checks pass `root@…`; the rules are keyed by address
try:
    with open(os.environ["ESTATE_SSH_STUB"], encoding="utf-8") as handle:
        hosts = json.load(handle).get("hosts", {})
except OSError:
    sys.exit(255)
entry = hosts.get(host)
if not entry or entry.get("fail"):
    sys.exit(255)
for keyword, output in entry.get("answers", []):
    if keyword in command:
        sys.stdout.write(output)
        sys.exit(0)
sys.exit(0)
'''

I1 = "192.168.1.51"
I2 = "192.168.1.52"
I3 = "192.168.1.53"
I4 = "192.168.1.54"


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _csv(rows) -> str:
    return "\n".join(rows) + "\n"


class LiveCase(unittest.TestCase):
    """A stub `ssh` on PATH, so a check's `main()` can be run against a fixed estate."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)

        self._bin = self.tmp / "bin"
        self._bin.mkdir()
        stub = self._bin / "ssh"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self._rules = self.tmp / "rules.json"

        self._old_path = os.environ.get("PATH")
        self._old_sshpass = os.environ.pop("SSHPASS", None)
        os.environ["PATH"] = f"{self._bin}{os.pathsep}{self._old_path or ''}"
        os.environ["ESTATE_SSH_STUB"] = str(self._rules)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._old_path is not None:
            os.environ["PATH"] = self._old_path
        if self._old_sshpass is not None:
            os.environ["SSHPASS"] = self._old_sshpass
        os.environ.pop("ESTATE_SSH_STUB", None)

    def run_check(self, module, hosts: dict, rules: dict):
        """Write `rules` and run `module.main()`, returning (exit code, textfile path)."""
        self._rules.write_text(json.dumps({"hosts": rules}), encoding="utf-8")
        prom = self.tmp / f"{module.CHECK}.prom"
        host_arg = ",".join(f"{name}={target}" for name, target in hosts.items())
        code = module.main(["--hosts", host_arg, "--prom", str(prom)])
        return code, prom

    @staticmethod
    def text(prom: Path) -> str:
        return prom.read_text(encoding="utf-8") if prom.exists() else ""


####################################################################################
# check-container-addresses: a container that moved must fail the run, not a log line
####################################################################################

ADDRESS_ROWS = {
    I1: [("mail", "192.168.1.15"), ("monarch", "192.168.1.56"),
         ("ontrak", "192.168.1.21")],
    I2: [("atlas", "192.168.1.90"), ("capstone", "192.168.1.30"), ("dev", "192.168.1.74"),
         ("genesis", "192.168.1.66"), ("rizzaura", "192.168.1.62"), ("www", "192.168.1.80")],
    I3: [("distro", "192.168.1.61"), ("magnate", "192.168.1.57"), ("onyx", "192.168.1.60"),
         ("pi", "192.168.1.70"), ("signara", "192.168.1.44"), ("subscribe", "192.168.1.58")],
    # The edge and three other containers moved i1 → i4 on 2026-10-02; the addresses
    # travel with the pinned rootfs, so only the host key changes here.
    I4: [("proxy", "192.168.1.71"), ("terminal", "192.168.1.22"),
         ("vault", "192.168.1.73"), ("vpn", "192.168.1.43")],
}


def _address_rules(moved: tuple[str, str] | None = None) -> dict:
    rules = {}
    for host, rows in ADDRESS_ROWS.items():
        listing = []
        for name, address in rows:
            if moved and moved == (host, name):
                address = "192.168.1.20"  # the 2026-10-01 renumber
            listing.append(f'{name},RUNNING,"{address} (eth0)"')
        rules[host] = {"answers": [["ns4", _csv(listing)], ["incus exec", "static\n"]]}
    return rules


class AddressLiveCase(LiveCase):
    def test_a_clean_estate_exits_zero_and_publishes_status_one(self):
        chk = _load("check-container-addresses")
        code, prom = self.run_check(chk, chk.HOSTS, _address_rules())
        self.assertEqual(code, 0)
        self.assertIn('innotel_estate_check_last_status{check="container_address"} 1', self.text(prom))

    def test_a_renumbered_container_fails_the_run_and_reaches_the_textfile(self):
        chk = _load("check-container-addresses")
        code, prom = self.run_check(chk, chk.HOSTS, _address_rules(moved=(I1, "ontrak")))
        self.assertEqual(code, 1, "a moved address must fail the check, not print a log line")
        body = self.text(prom)
        self.assertIn('innotel_estate_check_last_status{check="container_address"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="container_address"} ', body)
        self.assertNotIn('innotel_estate_check_failures{check="container_address"} 0', body)

    def test_an_unreachable_host_is_exit_two_not_a_pass(self):
        chk = _load("check-container-addresses")
        rules = _address_rules()
        rules[I1] = {"fail": True}
        code, prom = self.run_check(chk, chk.HOSTS, rules)
        self.assertEqual(code, 2)
        # Exit 2 still publishes 0, so the alert path sees a failure rather than silence
        # (this is the whole point of the promotion the 2026-10-01 outage forced).
        self.assertIn('innotel_estate_check_last_status{check="container_address"} 0', self.text(prom))
        self.assertNotIn("innotel_estate_check_failures", self.text(prom))


####################################################################################
# check-container-limits: a declared-but-unenforced cap must fail the run
####################################################################################

LIMIT_ROWS = [("proxy", "2", "0-1"), ("monarch", "2", "2-3"), ("mail", "1", "0"),
              ("ontrak", "1", "1"), ("terminal", "1", "2"), ("vault", "1", "3"),
              ("vpn", "1", "0")]


def _limit_rules(effective_override: dict | None = None) -> dict:
    effective_override = effective_override or {}
    listing = [
        {"name": name, "status": "Running", "expanded_config": {"limits.cpu": declared}}
        for name, declared, _ in LIMIT_ROWS
    ]
    cpusets = "\n".join(
        f"{name} {effective_override.get(name, spec)}" for name, _, spec in LIMIT_ROWS
    ) + "\n"
    return {
        I1: {
            "answers": [
                ["cpuset.cpus.effective", cpusets],
                ["nproc", "4\n"],
                ["format json", json.dumps(listing)],
            ]
        }
    }


class LimitsLiveCase(LiveCase):
    def test_a_clean_estate_exits_zero(self):
        chk = _load("check-container-limits")
        code, prom = self.run_check(chk, {"i1": f"root@{I1}"}, _limit_rules())
        self.assertEqual(code, 0)
        self.assertIn('innotel_estate_check_last_status{check="container_limits"} 1', self.text(prom))

    def test_a_cap_the_cpuset_does_not_honour_fails_the_run(self):
        # proxy declares 2 CPUs but the cpuset holds one: the fence is a pretence.
        chk = _load("check-container-limits")
        code, prom = self.run_check(chk, {"i1": f"root@{I1}"}, _limit_rules({"proxy": "0"}))
        self.assertEqual(code, 1)
        body = self.text(prom)
        self.assertIn('innotel_estate_check_last_status{check="container_limits"} 0', body)
        self.assertIn('innotel_estate_check_failures{check="container_limits"} 1', body)

    def test_an_unreachable_host_is_exit_two_not_a_pass(self):
        chk = _load("check-container-limits")
        rules = {I1: {"fail": True}}
        code, prom = self.run_check(chk, {"i1": f"root@{I1}"}, rules)
        self.assertEqual(code, 2)
        self.assertIn('innotel_estate_check_last_status{check="container_limits"} 0', self.text(prom))


####################################################################################
# check-estate-inventory: a host or pool the page does not name must fail the run
####################################################################################

INVENTORY_HOSTS = {"i1": f"root@{I1}", "i2": f"root@{I2}", "i3": f"root@{I3}", "i4": f"root@{I4}"}
KNOWN_REMOTES = [I1, I2, I3, I4]


def _inventory_rules(pools=None, remotes=None) -> dict:
    pools = pools or {"i1": ["incus"], "i2": ["main-pool"], "i3": ["tank"], "i4": ["default", "tank"]}
    remotes = remotes or {host: list(KNOWN_REMOTES) for host in INVENTORY_HOSTS}
    rules = {}
    for host, target in INVENTORY_HOSTS.items():
        remote_csv = _csv([f"r{i},{'https://' + addr + ':8443'},incus" for i, addr in enumerate(remotes[host])])
        rules[target.split("@")[-1]] = {
            "answers": [["remote list", remote_csv], ["storage list", _csv(pools[host])]]
        }
    return rules


class InventoryLiveCase(LiveCase):
    def test_a_clean_estate_exits_zero(self):
        chk = _load("check-estate-inventory")
        code, prom = self.run_check(chk, INVENTORY_HOSTS, _inventory_rules())
        self.assertEqual(code, 0)
        self.assertIn('innotel_estate_check_last_status{check="estate_inventory"} 1', self.text(prom))

    def test_an_unknown_host_fails_the_run(self):
        chk = _load("check-estate-inventory")
        remotes = {host: list(KNOWN_REMOTES) for host in INVENTORY_HOSTS}
        remotes["i1"] = [*KNOWN_REMOTES, "192.168.1.99"]
        code, prom = self.run_check(chk, INVENTORY_HOSTS, _inventory_rules(remotes=remotes))
        self.assertEqual(code, 1)
        self.assertIn('innotel_estate_check_last_status{check="estate_inventory"} 0', self.text(prom))

    def test_an_unknown_pool_fails_the_run(self):
        chk = _load("check-estate-inventory")
        pools = {"i1": ["incus"], "i2": ["main-pool", "scratch"], "i3": ["tank"], "i4": ["default", "tank"]}
        code, _ = self.run_check(chk, INVENTORY_HOSTS, _inventory_rules(pools=pools))
        self.assertEqual(code, 1)

    def test_an_unreachable_required_host_is_exit_two_not_a_pass(self):
        chk = _load("check-estate-inventory")
        rules = _inventory_rules()
        rules[I1] = {"fail": True}
        code, prom = self.run_check(chk, INVENTORY_HOSTS, rules)
        self.assertEqual(code, 2)
        self.assertIn('innotel_estate_check_last_status{check="estate_inventory"} 0', self.text(prom))


####################################################################################
# check-address-latency: a dialled address that goes dark or slow must warn/fail
####################################################################################

#: A stand-in for `ping`: argv[-1] is the address, and ESTATE_PING_STUB is a
#: `{address: rtt_ms}` map. An address not named in the map answers a healthy ~7 ms; an
#: address mapped to `null` exits non-zero with no output, which is exactly how a timeout
#: reads to `parse_rtts` — no sample at all. The default keeps a clean case a one-liner
#: while still covering every address the check dials, hosts and services alike.
PING_STUB = '''#!/usr/bin/env python3
import json, os, sys

address = sys.argv[-1]
try:
    with open(os.environ["ESTATE_PING_STUB"], encoding="utf-8") as handle:
        rules = json.load(handle)
except OSError:
    sys.exit(1)
rtt = rules.get(address, 7.0)
if rtt is None:
    sys.exit(1)
sys.stdout.write(f"64 bytes from {address}: icmp_seq=1 ttl=64 time={rtt} ms\\n")
'''


class LatencyLiveCase(LiveCase):
    def setUp(self) -> None:
        super().setUp()
        stub = self._bin / "ping"
        stub.write_text(PING_STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self._ping_rules = self.tmp / "ping.json"
        os.environ["ESTATE_PING_STUB"] = str(self._ping_rules)
        self.addCleanup(os.environ.pop, "ESTATE_PING_STUB", None)

    def run_latency(self, rules: dict):
        chk = _load("check-address-latency")
        self._ping_rules.write_text(json.dumps(rules), encoding="utf-8")
        prom = self.tmp / "address_latency.prom"
        host_arg = ",".join(f"{name}={target}" for name, target in chk.HOSTS.items())
        code = chk.main(["--hosts", host_arg, "--samples", "1", "--idle", "0", "--prom", str(prom)])
        return code, prom

    def test_a_prompt_estate_exits_zero_and_publishes_the_readings(self):
        code, prom = self.run_latency({I1: 7.2, I4: 0.1})  # everything else defaults to 7 ms
        self.assertEqual(code, 0)
        body = self.text(prom)
        self.assertIn('innotel_estate_check_last_status{check="address_latency"} 1', body)
        self.assertIn('kind="host",name="i1",address="192.168.1.51"} 7.200', body)
        # A table address is probed too — that is the reachability half of this check.
        self.assertIn('kind="service",name="atlas",address="192.168.1.90"', body)

    def test_an_address_held_back_by_the_radio_warns(self):
        code, prom = self.run_latency({I1: 400.0})
        self.assertEqual(code, 0, "a slow address is a warning, not a failure")
        body = self.text(prom)
        self.assertIn('innotel_estate_check_warnings{check="address_latency"} 1', body)
        self.assertIn('innotel_estate_check_failures{check="address_latency"} 0', body)

    def test_a_dark_service_address_fails_the_run(self):
        # capstone's address is dialled; nothing answers it. That is the outage one layer
        # below a renumber, and it must fail rather than be invisible.
        code, prom = self.run_latency({"192.168.1.30": None})
        self.assertEqual(code, 1)
        self.assertIn('innotel_estate_check_failures{check="address_latency"} 1', self.text(prom))


class StubSanityCase(LiveCase):
    def test_the_stub_is_what_the_checks_actually_call(self):
        # If the stub were not on PATH first, every case above would be testing the real
        # `ssh` (or exit 2 on a missing one) and would say nothing about the live path.
        self._rules.write_text(json.dumps({"hosts": {I2: {"answers": [["nproc", "4\n"]]}}}), encoding="utf-8")
        result = subprocess.run(["ssh", "-o", "x", f"root@{I1}", "nproc"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 255, "an unknown host must read as unreachable")
        known = subprocess.run(["ssh", "-o", "x", f"root@{I2}", "nproc"], capture_output=True, text=True)
        self.assertEqual(known.stdout, "4\n", "and a known one must answer")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
