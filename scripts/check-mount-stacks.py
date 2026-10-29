#!/usr/bin/env python3
"""Fail when an NFS mount is stale, or when the same share is mounted more than once.

WHY THIS EXISTS
---------------
On 2026-10-29 `admin.distro.innotel.us` answered nothing for six hours and the cause was
not in the app, the door or the network: `/srv/onyx-pool` in the `distro` container held
**32 769 mount entries**.

What happened is one loop. `nfs-ganesha` on onyx restarted at 00:24 on 2026-10-28, which
left every NFS handle issued before it stale. `mountpoint` and `df` do not return an error
on a stale handle — they block, uninterruptibly, walking a mount that will never answer. The
container's mount timer therefore decided, every minute, that the share was not mounted,
mounted a fresh copy on top of the stale one, and left its probe behind. Within hours every
`df` on the host was queued behind those probes, the load average reached ~80 on two vCPUs,
the control plane's 5 s health probe could no longer be answered, and the door to it went
dark. Nothing in the estate was watching a mount table, so the only symptom was a host that
felt slow and one URL that did not load.

The mounting is fixed (`scripts/onyx-pool-mounts` bounds every probe and refuses to stack);
this is the half that says so when it happens again — to a different share, on a different
host, with the same shape.

THE RULES
---------
1. **A target mounted more than once is a failure.** No stack here is intended: every extra
   copy makes the next probe slower and the next `df` longer, and the count is the only
   early warning a storm gives. One is normal, two is a bug in whatever mounted the second.
2. **A mount that cannot stat itself in five seconds is a failure.** A stale NFS handle is
   an outage for everyone who reads that path, and `timeout` is the only way to ask the
   question safely — without it the probe joins the queue it is meant to detect.
3. **A mount that answers slowly is a warning.** Two seconds is the line: the estate's shares
   answer in single-digit milliseconds, so a share taking hundreds is already in trouble and
   a share taking seconds is on its way to rule 2.
4. **A host that cannot be read is exit 2, never a pass.** Same as every check here.
5. **A container that cannot be read is a note.** It has no mount table to be wrong about;
   `distro` is the estate's only NFS client today, so a container that stops answering
   `incus exec` is the probe's own failure mode, not a finding about the estate.

Only NFS is read. A local filesystem cannot go stale, and the estate has no other networked
mount; when one appears it belongs in `FSTYPES` beside the timeout.

Usage:
    ./scripts/check-mount-stacks.py
    ./scripts/check-mount-stacks.py --prom /path/to/textfile
    SSHPASS='…' ./scripts/check-mount-stacks.py

Exit codes: 0 = every mount is single and answers, 1 = one is stacked, stale or slow,
2 = a host could not be read (never silently a pass).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from estate_check import Audit, Finding, METRIC, read_host, ssh, write_prom  # noqa: E402

CHECK = "mount_stack"

#: Where each estate host is reachable — the same table the other checks read.
HOSTS: dict[str, str] = {
    "i1": "root@192.168.1.51",
    "i2": "root@192.168.1.52",
    "i3": "root@192.168.1.53",
    "i4": "root@192.168.1.54",
}

#: Filesystems worth asking about. A local one cannot go stale.
FSTYPES = ("nfs", "nfs4")

#: Seconds a probe may take before it is killed. Matches the mount helper's own bound.
PROBE_TIMEOUT = 5

#: Milliseconds above which a share that did answer is still worth a look (rule 3).
SLOW_MS = 2000

#: The probe, run **inside** the scope it is measuring: a container's mountpoints are not
#: paths on the host, so `stat -f` has to run where the mount is. It reads that scope's own
#: `/proc/mounts` and prints one row per NFS mount — `scope|source|target|fstype|state|ms`.
#: Deliberately free of single quotes so it can travel as one `sh -c` argument, and
#: `timeout -k` sends SIGKILL because a task blocked in an NFS walk does not always take
#: SIGTERM.
PROBE = """
probe() {
  while read -r src tgt fstype rest; do
    case " $FSTYPES " in *" $fstype "*) ;; *) continue ;; esac
    start=$(date +%s%N)
    if timeout -k 2 "$PROBE_TIMEOUT" stat -f "$tgt" </dev/null >/dev/null 2>&1; then state=ok; else state=stale; fi
    end=$(date +%s%N)
    echo "$SCOPE|$src|$tgt|$fstype|$state|$(( (end - start) / 1000000 ))"
  done
}
probe < /proc/mounts
"""

#: The remote half: one round trip per host, which probes its own mount table and then every
#: running container's — inside the container, via `incus exec`.
WALK = (
    'SCOPE=host FSTYPES="nfs nfs4" PROBE_TIMEOUT=5 sh -c __PROBE__\n'
    "for line in $(incus list --format csv -c n,s 2>/dev/null); do\n"
    '  case "$line" in *,RUNNING) ;; *) continue ;; esac\n'
    '  c=${line%,*}\n'
    '  incus exec "$c" -- env SCOPE="$c" FSTYPES="nfs nfs4" PROBE_TIMEOUT=5 sh -c __PROBE__\n'
    "done\n"
)


@dataclass
class Mount:
    """One NFS mount entry, as read from a host's or a container's `/proc/mounts`."""

    scope: str
    source: str
    target: str
    fstype: str = "nfs"
    state: str = "ok"  # ok | stale
    elapsed_ms: int = 0


def parse_walk(text: str) -> list[Mount]:
    """Read the remote walk's rows. A malformed row is skipped, not guessed at."""
    mounts: list[Mount] = []
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 6:
            continue
        scope, source, target, fstype, state, ms = fields
        try:
            elapsed = int(ms)
        except ValueError:
            elapsed = 0
        mounts.append(Mount(scope=scope, source=source, target=target, fstype=fstype,
                            state="stale" if state != "ok" else "ok", elapsed_ms=elapsed))
    return mounts


def audit(mounts: list[Mount]) -> Audit:
    """Read the mounts as findings. Pure, so what counts as a stack is the thing under test."""
    out = Audit()
    depth = Counter((m.scope, m.target) for m in mounts)

    for (scope, target), count in sorted(depth.items()):
        if count > 1:
            sources = sorted({m.source for m in mounts if m.scope == scope and m.target == target})
            out.findings.append(Finding(
                "stacked", "fail",
                f"{scope} {target}: mounted {count} times ({', '.join(sources)}) — "
                "every copy makes the next probe and df slower; clear the stack, do not add to it",
            ))

    for mount in sorted(mounts, key=lambda m: (m.scope, m.target)):
        if mount.state == "stale":
            out.findings.append(Finding(
                "stale", "fail",
                f"{mount.scope} {mount.target}: {mount.source} does not answer in "
                f"{PROBE_TIMEOUT}s — a stale handle, or a share that is gone; "
                "reads on it block forever",
            ))
        elif mount.elapsed_ms >= SLOW_MS:
            out.findings.append(Finding(
                "slow", "warn",
                f"{mount.scope} {mount.target}: answered in {mount.elapsed_ms} ms "
                f"(the estate's shares answer in single digits) — on its way to a stale handle",
            ))
    return out


def mount_series(mounts: list[Mount]) -> str:
    """The per-scope gauges, so a growing stack is chartable and not only a verdict."""
    lines = [
        f"# HELP {METRIC}_mount_entries NFS mount entries in this scope's mount table",
        f"# TYPE {METRIC}_mount_entries gauge",
        f"# HELP {METRIC}_mount_probe_ms How long the newest mount's self-stat took",
        f"# TYPE {METRIC}_mount_probe_ms gauge",
    ]
    depth = Counter(m.scope for m in mounts)
    for scope in sorted(depth):
        lines.append(f'{METRIC}_mount_entries{{check="{CHECK}",scope="{scope}"}} {depth[scope]}')
    for mount in sorted(mounts, key=lambda m: (m.scope, m.target)):
        selector = f'check="{CHECK}",scope="{mount.scope}",target="{mount.target}"'
        lines.append(f"{METRIC}_mount_probe_ms{{{selector}}} {mount.elapsed_ms}")
    return "\n".join(lines)


def gather(hosts: dict[str, str], runner=ssh) -> tuple[list[Mount], list[str]]:
    """Read every host's mount table, and every container's, through `read_host`.

    Returns the mounts and the hosts that produced no read at all. An unreachable host
    raises `HostUnreadable` and `main` exits 2 — "the host is gone" is a different fact
    from "the host answered and its mount table is a mess".
    """
    mounts: list[Mount] = []
    empty: list[str] = []
    for host, target in hosts.items():
        text = read_host(host, target, WALK.replace("__PROBE__", _q(PROBE)), runner)
        found = parse_walk(text)
        if not found:
            empty.append(host)
        mounts += [Mount(**{**m.__dict__, "scope": f"{host}/{m.scope}"}) for m in found]
    return mounts, empty


def _q(text: str) -> str:
    """Quote a shell program for the remote `sh -c` without touching its own quotes."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


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

    def emit(result: Audit | None, ran: bool, mounts: list[Mount] | None = None) -> None:
        if args.prom:
            try:
                write_prom(args.prom, CHECK, result, ran, extra=mount_series(mounts) if mounts else None)
            except OSError as error:
                print(f"check-mount-stacks: could not write {args.prom}: {error}", file=sys.stderr)

    if shutil.which("ssh") is None:
        print("check-mount-stacks: no ssh on PATH", file=sys.stderr)
        emit(None, ran=False)
        return 2

    try:
        mounts, empty = gather(hosts)
    except Exception as error:  # noqa: BLE001 — any ssh failure is the same finding here
        print(f"check-mount-stacks: could not read a host: {error}", file=sys.stderr)
        if not (os.environ.get("SSHPASS") and shutil.which("sshpass")):
            print(
                "check-mount-stacks: reads hosts with key-based `ssh`; where the estate uses "
                "passwords instead, set SSHPASS and install sshpass",
                file=sys.stderr,
            )
        emit(None, ran=False)
        return 2

    result = audit(mounts)
    for host in empty:
        result.findings.append(Finding(
            "no_reading", "note",
            f"{host}: no NFS mounts were read — nothing to judge (an empty table, or the walk "
            "could not list the host's containers)",
        ))
    emit(result, ran=True, mounts=mounts)

    for level in ("fail", "warn", "note"):
        for finding in [f for f in result.findings if f.level == level]:
            print(f"{level.upper():4} {finding.message}")

    by_scope = Counter(m.scope for m in mounts)
    print(f"\ncheck-mount-stacks: {len(result.failures)} failure(s), {len(result.warnings)} warning(s)")
    print(f"  {len(mounts)} NFS mount(s) over {len(by_scope)} scope(s): "
          + ", ".join(f"{scope} {count}" for scope, count in sorted(by_scope.items())))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
