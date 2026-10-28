#!/usr/bin/env python3
"""Memory headroom guard for ord-atheniq.

On 2026-10-27 this host froze rather than OOM-killed: every TCP port stopped
answering while ICMP and WireGuard still replied, and the kernel log held no
out-of-memory record. Swap had filled and the machine spent all its time
faulting pages back in. `earlyoom` has since been installed, but it reacts at
the default 10%/10% line, by which point swap is already exhausted and it has
begun killing platform services (it took `mysqld`, and the container restarted).

This guard watches the real numbers every 15 seconds and acts earlier and more
narrowly than earlyoom: it warns, and when the host is genuinely heading for
the same wall it cancels the *build* rather than letting the box thrash. It
never kills a database, a cache, or anything it did not recognise as a build.

State is appended to /var/log/memguard.jsonl and logged to the journal, so a
breach is visible afterwards even though this host has no metrics path to the
estate Prometheus (wg0 is a /30 to i4 only, and Vault/NPM are LAN-only).

Set MEMGUARD_WEBHOOK to POST a one-line JSON alert somewhere reachable from the
internet; with no webhook configured it stays local and silent, by design.

    memguard.py --once        # print one sample and exit (no action)
    memguard.py               # run the loop
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

SAMPLE_SECONDS = 15

# Warn while there is still room to react; act only when the same shape as the
# freeze is forming (MemAvailable tiny *and* swap nearly full, or swap itself
# gone). Percentages, because the box's size may change.
# A build legitimately uses almost all of RAM (page cache is reclaimable), so
# low MemAvailable alone is not the emergency. What froze this host was swap
# running out: with nowhere to put a page, every fault became a disk wait.
# So swap exhaustion is critical on its own, and low memory is only critical
# when swap is also nearly gone.
WARN_MEM_PCT = 12.0
WARN_SWAP_PCT = 15.0
CRIT_MEM_PCT = 4.0
CRIT_SWAP_PCT = 6.0

# Two consecutive critical samples, so a single spike does not cancel a build
# that was about to finish.
CRIT_SAMPLES_TO_ACT = 2

STATE = "/run/memguard-state.json"
LOG = "/var/log/memguard.jsonl"
LOG_MAX_BYTES = 5 * 1024 * 1024

# A build is the only thing this guard will ever cancel. The pattern is the
# tutor/buildx command line, which also covers `tutor images build`.
BUILD_PATTERNS = (
    r"buildx build --tag=\S*openedx-mfe",
    r"tutor images build mfe",
)

# Never touch these, whatever they cost. earlyoom already killed mysqld once.
PROTECTED = re.compile(
    r"^(mysqld|mariadbd|redis-server|mongod|meilisearch|dockerd|containerd|"
    r"sshd|systemd|init|python3|memguard)$"
)


def read_meminfo():
    info = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])  # kB
    return info


def read_psi():
    """full avg60 from /proc/pressure/memory, or None when unavailable."""
    try:
        with open("/proc/pressure/memory", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("full "):
                    for field in line.split()[1:]:
                        if field.startswith("avg60="):
                            return float(field.split("=")[1])
    except OSError:
        return None
    return None


def sample():
    info = read_meminfo()
    mem_total = info.get("MemTotal", 1)
    swap_total = info.get("SwapTotal", 0)
    mem_pct = 100.0 * info.get("MemAvailable", 0) / mem_total
    swap_pct = 100.0 * info.get("SwapFree", 0) / swap_total if swap_total else 100.0
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mem_avail_pct": round(mem_pct, 2),
        "mem_avail_mb": round(info.get("MemAvailable", 0) / 1024),
        "swap_free_pct": round(swap_pct, 2),
        "swap_free_mb": round(info.get("SwapFree", 0) / 1024),
        "psi_full60": read_psi(),
    }


def level_of(s):
    if s["swap_free_pct"] <= CRIT_SWAP_PCT:
        return "CRIT"
    if s["mem_avail_pct"] <= CRIT_MEM_PCT and s["swap_free_pct"] <= WARN_SWAP_PCT:
        return "CRIT"
    if s["mem_avail_pct"] <= WARN_MEM_PCT or s["swap_free_pct"] <= WARN_SWAP_PCT:
        return "WARN"
    return "OK"


def log(level, s, action=""):
    line = dict(s, level=level, action=action)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass
    msg = (f"{level} mem_avail={s['mem_avail_pct']}% ({s['mem_avail_mb']}MB) "
           f"swap_free={s['swap_free_pct']}% ({s['swap_free_mb']}MB) "
           f"psi_full60={s['psi_full60']}")
    if action:
        msg += f" action={action}"
    # daemon.notice shows up in the journal; crit marks the ones that mattered.
    pri = "-p daemon.crit" if level == "CRIT" else "-p daemon.warning"
    subprocess.run(["logger"] + pri.split() + ["-t", "memguard", msg], check=False)
    webhook = os.environ.get("MEMGUARD_WEBHOOK")
    if webhook and level != "OK":
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    webhook,
                    data=json.dumps({"text": f"ord-atheniq {msg}"}).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=5,
            )
        except Exception:
            pass


def find_builds():
    """PIDs of a running MFE build, excluding this process and its parents."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if not cmd or PROTECTED.match(os.path.basename(cmd.split()[0] or "")):
            continue
        if any(re.search(p, cmd) for p in BUILD_PATTERNS):
            found.append(int(pid))
    return found


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"crit_streak": 0}


def save_state(state):
    try:
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def act_on_crit(state, s):
    """Cancel a build; never kill anything else. Returns a description."""
    pids = find_builds()
    if not pids:
        return "no build to cancel"
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            return f"cannot signal {pid}"
    return f"canceled build (pid {','.join(map(str, pids))})"


def run_once():
    s = sample()
    lvl = level_of(s)
    if lvl == "OK":
        print(f"OK   mem_avail={s['mem_avail_pct']}% swap_free={s['swap_free_pct']}% "
              f"psi_full60={s['psi_full60']}")
        return 0
    print(f"{lvl} mem_avail={s['mem_avail_pct']}% swap_free={s['swap_free_pct']}% "
          f"psi_full60={s['psi_full60']}")
    return 0 if lvl == "WARN" else 2


def main():
    if "--once" in sys.argv:
        return run_once()

    state = load_state()
    while True:
        s = sample()
        lvl = level_of(s)
        action = ""
        if lvl == "CRIT":
            state["crit_streak"] = state.get("crit_streak", 0) + 1
            if state["crit_streak"] >= CRIT_SAMPLES_TO_ACT:
                action = act_on_crit(state, s)
                # Reset so a second cancellation needs fresh evidence.
                state["crit_streak"] = 0
        else:
            state["crit_streak"] = 0
        save_state(state)
        if lvl != "OK" or action:
            log(lvl, s, action)
        elif int(time.time()) % 300 < SAMPLE_SECONDS:
            log("OK", s)  # a heartbeat every ~5 min, so silence means "not running"
        time.sleep(SAMPLE_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
