#!/usr/bin/env python3
"""Fail when a container's declared limit is not the one in force — or is about to run out.

WHY THIS EXISTS
---------------
A `limits.cpu` that is written down but not in force is worse than none: the
placement page reads as though a runaway is fenced, and the runaway is not. That is
not hypothetical — the 2026-10-01 pass set i1's caps and then read `cpu.max` to check
them, found `max`, and concluded the caps were waiting for a restart. They were not:
incus implements `limits.cpu` as a **cpuset pin**, not a CPU quota, so `cpu.max` stays
`max` on every container that has a cap, and the cap was live the whole time. The
wrong file made a correct state look broken, which is the same class of error as a
check that never fails.

THE RULES
---------
1. **The enforced cap is the cpuset.** A running container whose `limits.cpu` is N
   must have exactly N CPUs in `/sys/fs/cgroup/lxc.payload.<c>/cpuset.cpus.effective`
   (a container cannot use a CPU it is not assigned, which is why this bounds a
   runaway harder than a quota would). Differs → failure, and it names both numbers.
2. **A cap above the host is a warning, not a failure.** `limits.cpu=8` on a 4-CPU
   host says nothing an operator can act on; incus clamps it to the host, so the
   cpuset will be smaller than the number written and that is reported as the
   misconfiguration it is rather than as a broken cap.
3. **No declared cap is not a finding.** The estate caps containers it wants to
   fence, not all of them; an uncapped container is a decision (see
   `docs/container-placement.md`), not drift.
4. **A stopped container is a note.** It has no cgroup to be wrong.

THE OTHER HALF OF A LIMIT
-------------------------
A limit can be in force *and about to be hit*, and that failure is silent in the
same way. `cerulean-npm` on the edge held 572 open files against a soft `nofile` of
1024, and nginx opens the log file of every generated vhost again each time it
parses the config, so `nginx -s reload` needed ~1050 of them and aborted:

    nginx: [emerg] open() "/data/logs/redirection-host-31_error.log"
                         failed (24: Too many open files)

The master dies before it swaps the config, so the OLD one kept serving — every
proxy-host change needed a full container restart instead of a reload — and nothing
said so: `nginx -t` passes, because a test process starts with no files open while
the master already held 572. `socket() failed (24: Too many open files) while
connecting to upstream` was in the access log the whole time, reading as an upstream
fault. Nothing watched the one number that would have said so.

THE FILE-DESCRIPTOR RULES
-------------------------
5. **A process at its soft file limit is a failure.** The next `open()` fails, which
   for a server is the outage rather than the warning sign of one.
6. **A process holding more than half its soft file limit is a warning.** Half is the
   measured line, not a round number: a config reload costs about as much as the
   working set already open (572 held, ~1050 needed), so a limit with less than one
   working set of headroom cannot take the reload, the restart or the traffic peak
   that has not happened yet. The old edge sat at 56 % — the case this rule is for.
   It is a warning and not a failure because the number is a budget running out, not
   an outage, and the estate's worst is 42 % (measured 2026-10-27).
7. **A host where no docker container could be read is a note.** Four hosts have
   docker somewhere, so a host that suddenly reports nothing is the probe having
   stopped working — the failure mode of a check that has quietly stopped checking.

THE MEMORY HALF
---------------
Until 2026-11-14 this file's subject was CPU caps and file descriptors, and the two
limits that decide whether a *host* stays up were never read. `atheniq` was configured

    limits.memory: 3GiB
    limits.memory.enforce: soft
    limits.memory.swap: 2GiB

and held **2 547 MiB of that host's 4 095 MiB swap** — more than the cap it was written
to have, on a host whose swap was already full. Nothing was misconfigured; the config was
simply not what the kernel had: `memory.max` and `memory.swap.max` both read `max`, because
the limits had been written on a container that was already running and incus applies a
soft limit to a live container as `memory.high`.

Which file carries a limit is therefore not a matter of taste. Read across the estate on
2026-11-14: a container that started with its limit has it in `memory.max` (`blueprint`
512 MiB, `ontrak` 2 944 MiB), and one whose limit was changed while running has it in
`memory.high` with `memory.max` at `max` (`atheniq`, before it was made hard). Both are
the ceiling in force; a container showing `max` in *both* is the one with no ceiling.

8. **The declared memory limit must be in one of the two files.** `memory.max`, as incus
   writes it at start or under hard enforcement; or `memory.high`, as it writes a soft
   limit applied to a running container. Neither holding it is a failure that names both.
   A percentage is resolved against the host's MemTotal, the way incus resolves it.
9. **A written swap size must be `memory.swap.max`.** `true` and `false` are flags, not
   sizes, and are not compared. What this catches is a cap the kernel never took — the
   `atheniq` case above, where a written 2 GiB sat beside `memory.swap.max` at `max`.
10. **`limits.memory.swap: false` must be `memory.swap.max` 0.** `true` asks for no
    ceiling and is not a finding.
11. **No declared memory limit is not a finding** — the same decision as rule 3, for the
    same reason: the estate fences what it wants fenced.

WHAT IS MEASURED, AND WHY NOT THE DECLARED VALUE
------------------------------------------------
The soft limit is read from `/proc/<pid>/limits` of the process that is being
measured, because that is the limit in force — a compose `ulimits:` block, an
explicit `--ulimit`, and an inherited default all end up there and nowhere else.
EMFILE is per process, not per container, so the process measured is the busiest
one (`docker top` lists them; the one holding the most descriptors wins) rather than
PID 1: the edge's nginx master holds 575 while its entrypoint holds 7, and only the
master is the one that dies. A limit reported as `unlimited` is not a finding —
nothing can exhaust it.

Usage:
    ./scripts/check-container-limits.py
    ./scripts/check-container-limits.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-container-limits.py        # where the estate uses passwords

Exit codes: 0 = every declared cap is enforced and no file limit is nearly spent,
1 = one of those does not hold, 2 = the check could not run (a host unreachable —
never silently a pass).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, count_cpus, read_host, ssh, write_prom  # noqa: E402

CHECK = "container_limits"

#: The FD walk enumerates every process of every container on docker *and* incus. i1 grew
#: past the 25-second ssh read: on 2026-11-17 the sweep timed out at 40 s, the check named
#: the host unreadable, and the identical read finished when run by hand a minute later.
#: One host is read at a time, so a longer cap costs nothing while a host is healthy.
READ_TIMEOUT = 90


def read(target: str, command: str) -> str:
    """`ssh` with room for the whole sweep; `read_host` still names the host on failure."""
    return ssh(target, command, timeout=READ_TIMEOUT)



#: Where each estate host is reachable. `i4` is in scope from 2026-10-02: it took the
#: edge and three other containers off i1, each of which declares a `limits.cpu`, so a
#: cap there is a fence to check like any other. i4 is a 2-vCPU host and the moved
#: containers declare 1-2, so a cap above the host would be a real finding here.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    # `i2` is up and running on 2026-11-16 (owner) (`docs/estate-rebalance-2026-11-15.md` §19).
    # `i3` (2c/4t, 15 403 MiB) took six of its containers on 2026-11-15. Its caps are
    # the interesting ones now: the moved set sums to ~14.4 GiB of its RAM, which is
    # overcommit rather than reservation, and `copilotkit`'s cap was cut 10 GiB -> 4 GiB
    # in the move. A cap that does not fit the host is exactly what this check is for.
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}


#: The share of a soft file limit at which it is worth reporting *before* it is
#: reached — rule 6. Half, because a config reload costs about what is already open.
FD_WARN_SHARE = 0.5

#: The docker half of the read, run once for the host itself and once inside each
#: running incus container that has a docker. Deliberately free of single quotes, so
#: the whole thing can travel as one `sh -c` argument (see `FD_WALK`); it prints one
#: line per container — `$0` (the scope it was handed) first, the process name last,
#: so a process name containing the separator cannot break the line.
FD_PROBE = """
for id in $(docker ps -q 2>/dev/null); do
  name=$(docker inspect -f "{{.Name}}" "$id" 2>/dev/null | sed "s|^/||")
  soft=; open=0; proc=
  for pid in $(docker top "$id" -o pid 2>/dev/null | tail -n +2); do
    [ -r "/proc/$pid/limits" ] || continue
    s=$(awk "/Max open files/{print \\$4}" "/proc/$pid/limits")
    o=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
    if [ "$o" -gt "$open" ]; then open=$o; soft=$s; proc=$(cat "/proc/$pid/comm"); fi
  done
  [ -n "$proc" ] && echo "$0|$name|$soft|$open|$proc"
done
"""

#: The walk that feeds `FD_PROBE` every docker a host has. `incus list -c n,s` reads
#: every container's state in one call rather than an `incus info` per container, and
#: a container without docker is skipped in one round trip.
FD_WALK = (
    "P='" + FD_PROBE + "'\n"
    "sh -c \"$P\" host\n"
    "for line in $(incus list --format csv -c n,s 2>/dev/null); do\n"
    "  case \"$line\" in *,RUNNING) ;; *) continue ;; esac\n"
    "  c=${line%,*}\n"
    "  incus exec \"$c\" -- sh -c 'command -v docker >/dev/null 2>&1' || continue\n"
    "  incus exec \"$c\" -- sh -c \"$P\" \"$c\"\n"
    "done"
)


@dataclass
class Container:
    """One instance's declared cap and the cpuset actually in force."""

    name: str
    state: str
    declared: int | None = None
    effective: int | None = None
    #: `container` or `virtual-machine`. The cpuset rule is container-shaped: a VM's
    #: `limits.cpu` is the vCPU count incus hands qemu at boot (`-smp N`), so there is no
    #: `lxc.payload.<name>` cgroup to read and its absence proves nothing (see `audit`).
    kind: str = "container"
    #: The memory side: as declared (raw and resolved) and as the cgroup holds it. The raw
    #: text is kept because a finding has to quote what was written — `3GiB`, a percentage
    #: — rather than a byte count nobody typed.
    mem_declared_raw: str | None = None
    mem_declared: int | None = None
    mem_enforce: str = "soft"
    swap_declared: str | None = None
    mem_max: str | None = None
    mem_high: str | None = None
    swap_max: str | None = None


@dataclass
class FdPressure:
    """The busiest process in one docker container, and the limit it is spending.

    `scope` is where the container lives: `host` for the host's own docker, or the
    incus container whose docker holds it. `soft` is the limit read from that
    process's `/proc/<pid>/limits` — the one in force.
    """

    host: str
    scope: str
    container: str
    process: str
    soft: int
    open: int

    @property
    def share(self) -> float:
        """How much of the limit is in use, 0.0-1.0 — the number rule 6 turns on."""
        return self.open / self.soft if self.soft else 0.0


def parse_fd(host: str, text: str) -> list[FdPressure]:
    """The probe's lines as data; anything unreadable is dropped, not guessed.

    A limit the kernel reports as `unlimited` has no number to compare against, and
    that is the one case where having none is the answer rather than a gap.
    """
    out: list[FdPressure] = []
    for line in text.splitlines():
        parts = line.strip().split("|", 4)
        if len(parts) != 5:
            continue
        scope, container, soft, count, process = parts
        try:
            limit, held = int(soft), int(count)
        except ValueError:
            continue
        if limit <= 0:
            continue
        out.append(
            FdPressure(host=host, scope=scope, container=container, process=process,
                       soft=limit, open=held)
        )
    return out


def fd_findings(observed: dict[str, list[FdPressure]]) -> list[Finding]:
    """Rules 5-7: a limit in force that is at, or nearly at, its end."""
    out: list[Finding] = []
    for host, containers in observed.items():
        if not containers:
            out.append(
                Finding(
                    "fd_unread",
                    "note",
                    f"{host}: no docker container could be read — nothing there for the "
                    f"file-descriptor rule to look at",
                )
            )
            continue
        for pressure in sorted(containers, key=lambda c: (-c.share, c.container)):
            where = f"{host} {pressure.scope}/{pressure.container}"
            if pressure.open >= pressure.soft:
                out.append(
                    Finding(
                        "fd_exhausted",
                        "fail",
                        f"{where}: {pressure.process} holds {pressure.open} of "
                        f"{pressure.soft} open files — at the limit, so the next open() "
                        f"fails",
                    )
                )
            elif pressure.share > FD_WARN_SHARE:
                out.append(
                    Finding(
                        "fd_near_limit",
                        "warn",
                        f"{where}: {pressure.process} holds {pressure.open} of "
                        f"{pressure.soft} open files ({pressure.share:.0%}) — past half "
                        f"the budget, so a config reload or a peak the size of its "
                        f"working set has nowhere to go",
                    )
                )
    return out


def audit(
    observed: dict[str, list[Container]],
    host_cpus: dict[str, int],
    fd_pressure: dict[str, list[FdPressure]] | None = None,
    host_mem: dict[str, int] | None = None,
) -> Audit:
    """Compare what each container declares against what the host enforces — and what
    each docker container is spending of the limit it has."""
    out = Audit()
    for host, containers in observed.items():
        cpus = host_cpus.get(host, 0)
        for inst in sorted(containers, key=lambda c: c.name):
            if inst.declared is None:
                continue  # rule 3: no declared cap is not a finding
            if inst.state != "RUNNING":
                out.findings.append(
                    Finding("stopped", "note", f"{host} {inst.name}: {inst.state} — no cgroup to check")
                )
                continue
            if inst.declared > cpus:
                out.findings.append(
                    Finding(
                        "cap_above_host",
                        "warn",
                        f"{host} {inst.name}: limits.cpu={inst.declared} on a {cpus}-CPU host — "
                        f"incus clamps it, so the written cap is not the fenced one",
                    )
                )
            if inst.kind == "virtual-machine":
                # The first VM in the estate (a Windows packaging build, 2026-10-26) failed
                # this check on every run for having no container cgroup. Its cap is the
                # vCPU count qemu was started with, which incus applies at boot.
                out.findings.append(
                    Finding(
                        "vm_cap_at_boot",
                        "note",
                        f"{host} {inst.name}: virtual machine with limits.cpu={inst.declared} — "
                        "the cap is the vCPU count qemu boots with, not a cpuset, so the "
                        "container rule does not apply",
                    )
                )
                continue
            if inst.effective is None:
                out.findings.append(
                    Finding(
                        "no_cpuset",
                        "fail",
                        f"{host} {inst.name}: running with limits.cpu={inst.declared} but no readable "
                        f"cpuset — the cap is not enforced",
                    )
                )
                continue
            if inst.effective != inst.declared and inst.declared <= cpus:
                out.findings.append(
                    Finding(
                        "cap_not_enforced",
                        "fail",
                        f"{host} {inst.name}: limits.cpu={inst.declared} but the cpuset holds "
                        f"{inst.effective} CPU(s) — the fence is not the one that was written",
                    )
                )
    for host, containers in observed.items():
        for inst in sorted(containers, key=lambda c: c.name):
            out.findings.extend(memory_findings(host, inst, (host_mem or {}).get(host, 0)))
    if fd_pressure is not None:
        out.findings.extend(fd_findings(fd_pressure))
    return out


#: Sizes incus writes in a config (`3GiB`, `512MiB`) and the ones the kernel writes
#: (`3221225472`). Both sides of a memory comparison are parsed to bytes, because that is
#: the only form two of them can be compared in.
_SIZE = {
    "b": 1,
    "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
    "k": 1000, "m": 1000 ** 2, "g": 1000 ** 3, "t": 1000 ** 4,
    "ki": 1024, "mi": 1024 ** 2, "gi": 1024 ** 3, "ti": 1024 ** 4,
}


def parse_size(value: str | None, total: int = 0) -> int | None:
    """Bytes for a size incus or the kernel wrote; None when it is not a size.

    `max` and `unlimited` mean no ceiling and parse to None, which the memory rules read
    as *nothing is enforced here* rather than as a missing reading — the same distinction
    the CPU rules draw between `max` and an absent cpuset. A percentage is a share of
    `total` (the host's MemTotal), which is what incus resolves it against.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if not text or text in ("max", "unlimited"):
        return None
    if text.endswith("%"):
        if not total:
            return None
        try:
            return int(total * float(text[:-1]) / 100)
        except ValueError:
            return None
    for suffix in sorted(_SIZE, key=len, reverse=True):
        if not text.endswith(suffix):
            continue
        try:
            return int(float(text[: -len(suffix)]) * _SIZE[suffix])
        except ValueError:
            continue
    try:
        return int(float(text))
    except ValueError:
        return None


def memory_findings(host: str, inst: Container, mem_total: int) -> list[Finding]:
    """Rules 8-11: the memory and swap ceiling written down is the one in force.

    A stopped container has no cgroup to be wrong and is reported once, by the CPU pass.
    """
    if inst.state != "RUNNING":
        return []
    if inst.mem_declared is None and not inst.swap_declared:
        return []
    where = f"{host} {inst.name}"
    out: list[Finding] = []

    if inst.mem_declared is not None:
        # A size is exact; a percentage is rounded to a page boundary on the way in.
        slack = 1048576 if str(inst.mem_declared_raw or "").strip().endswith("%") else 0

        def holds(value: str | None) -> bool:
            got = parse_size(value, mem_total)
            return got is not None and abs(got - inst.mem_declared) <= slack

        if inst.mem_max is None and inst.mem_high is None:
            out.append(
                Finding(
                    "memory_unreadable",
                    "fail",
                    f"{where}: limits.memory={inst.mem_declared_raw} but no memory cgroup "
                    f"could be read — nothing shows the ceiling is enforced",
                )
            )
        elif inst.mem_enforce == "hard":
            if not holds(inst.mem_max):
                out.append(
                    Finding(
                        "memory_not_enforced",
                        "fail",
                        f"{where}: limits.memory={inst.mem_declared_raw} with "
                        f"limits.memory.enforce=hard but memory.max holds "
                        f"{inst.mem_max or 'nothing'} — the ceiling in force is not the "
                        f"written one",
                    )
                )
        elif not (holds(inst.mem_max) or holds(inst.mem_high)):
            out.append(
                Finding(
                    "memory_not_enforced",
                    "fail",
                    f"{where}: limits.memory={inst.mem_declared_raw} but neither memory.max "
                    f"({inst.mem_max or 'nothing'}) nor memory.high "
                    f"({inst.mem_high or 'nothing'}) holds it — nothing caps this container",
                )
            )

    raw_swap = (inst.swap_declared or "").strip()
    if not raw_swap or raw_swap.lower() in ("true", "false"):
        return out
    want = parse_size(raw_swap, mem_total)
    if want is None:
        return out
    got = parse_size(inst.swap_max, mem_total)
    if got is None or abs(got - want) > 1048576:
        out.append(
            Finding(
                "swap_not_enforced",
                "fail",
                f"{where}: limits.memory.swap={raw_swap} but memory.swap.max holds "
                f"{inst.swap_max or 'nothing'} — the swap cap is not one the kernel took "
                f"(atheniq held 2547 MiB against a written 2 GiB, 2026-11-14)",
            )
        )
    return out


# --------------------------------------------------------------------------------------
# The estate, as it is
# --------------------------------------------------------------------------------------


def gather(
    hosts: dict[str, str], runner=read
) -> tuple[
    dict[str, list[Container]],
    dict[str, int],
    dict[str, list[FdPressure]],
    dict[str, int],
]:
    """Read each host's declared caps, the cpuset actually in force, and every docker
    container's file-descriptor pressure.

    Two reads per host rather than one per container: `incus list --format json` carries
    every container's `expanded_config` (profiles merged), and one shell loop reads every
    cpuset. The declared cap therefore comes from the same merged view incus uses. The
    file descriptors are a fourth read — the same walk over the host and every running
    incus container, one round trip each, because a docker lives wherever a docker runs.
    """
    observed: dict[str, list[Container]] = {}
    host_cpus: dict[str, int] = {}
    host_mem: dict[str, int] = {}
    pressure: dict[str, list[FdPressure]] = {}
    for host, target in hosts.items():
        host_cpus[host] = int(read_host(host, target, "nproc", runner).strip() or 0)
        # MemTotal in bytes, because a percentage limit is a share of it.
        host_mem[host] = int(
            read_host(
                host, target, "awk '/^MemTotal:/{print $2*1024}' /proc/meminfo", runner
            ).strip()
            or 0
        )
        listing = json.loads(read_host(host, target, "incus list --format json", runner) or "[]")
        pressure[host] = parse_fd(host, read_host(host, target, FD_WALK, runner))
        cpuset = read_host(
            host,
            target,
            "for c in $(incus list --format csv -c n); do "
            'd=/sys/fs/cgroup/lxc.payload.$c; '
            'printf "%s|%s|%s|%s|%s\\n" "$c" '
            '"$(cat $d/cpuset.cpus.effective 2>/dev/null)" '
            '"$(cat $d/memory.max 2>/dev/null)" '
            '"$(cat $d/memory.high 2>/dev/null)" '
            '"$(cat $d/memory.swap.max 2>/dev/null)"; '
            "done",
            runner,
        )
        effective: dict[str, str] = {}
        memory: dict[str, tuple[str | None, str | None, str | None]] = {}
        for line in cpuset.splitlines():
            fields = [part.strip() for part in line.split("|")]
            if not fields or not fields[0]:
                continue
            name = fields[0]
            fields += [""] * (4 - len(fields))
            effective[name] = fields[1]
            memory[name] = (fields[2] or None, fields[3] or None, fields[4] or None)

        containers: list[Container] = []
        for entry in listing:
            name = entry.get("name", "")
            cfg = entry.get("expanded_config") or {}
            declared_raw = cfg.get("limits.cpu")
            declared = int(declared_raw) if declared_raw not in (None, "", "0") else None
            mem_raw = cfg.get("limits.memory")
            if mem_raw in (None, "", "0"):
                mem_raw = None
            swap_raw = (cfg.get("limits.memory.swap") or "").strip() or None
            spec = effective.get(name, "")
            containers.append(
                Container(
                    name=name,
                    # `incus list --format json` says "Running"; the csv says "RUNNING".
                    # Normalise once here so the audit has one spelling to reason about.
                    state=entry.get("status", "").upper(),
                    declared=declared,
                    effective=count_cpus(spec) if spec else None,
                    kind=entry.get("type", "container") or "container",
                    mem_declared_raw=mem_raw,
                    mem_declared=parse_size(mem_raw, host_mem[host]),
                    mem_enforce=(cfg.get("limits.memory.enforce") or "soft").strip().lower(),
                    swap_declared=swap_raw,
                    mem_max=memory.get(name, (None, None, None))[0],
                    mem_high=memory.get(name, (None, None, None))[1],
                    swap_max=memory.get(name, (None, None, None))[2],
                )
            )
        observed[host] = containers
    return observed, host_cpus, pressure, host_mem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", help="name=target pairs, comma separated (default: the estate's)")
    parser.add_argument("--prom", metavar="PATH", help="write a Prometheus textfile here for the alerts")
    args = parser.parse_args(argv)

    hosts = dict(HOSTS)
    if args.hosts:
        hosts = {}
        for pair in args.hosts.split(","):
            name, _, target = pair.partition("=")
            hosts[name.strip()] = target.strip()

    def emit(result: Audit | None, ran: bool) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran)
            except OSError as error:
                print(f"check-container-limits: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-container-limits: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        observed, host_cpus, pressure, host_mem = gather(hosts)
    except Exception as error:
        print(f"check-container-limits: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-container-limits: reads hosts with key-based `ssh`; where the estate uses "
                "passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(observed, host_cpus, pressure, host_mem)
    emit(result, ran=True)
    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")
    declared = sum(1 for cs in observed.values() for c in cs if c.declared)
    memcaps = sum(1 for cs in observed.values() for c in cs if c.mem_declared or c.swap_declared)
    read = sum(len(cs) for cs in pressure.values())
    print(
        f"\ncheck-container-limits: {len(result.failures)} failure(s), "
        f"{len(result.warnings)} warning(s), {declared} declared cpu cap(s), {memcaps} declared memory/swap cap(s), "
        f"{read} docker container(s) read for file-descriptor pressure"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
