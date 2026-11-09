#!/usr/bin/env python3
"""Republish Coolify's addresses when a deployment changes them.

The publisher (`coolify-cerulean-dns.py`) is a single reconcile pass; this is
what makes it react to deployments instead of a two-minute clock. Coolify's
deployments, restarts and removals all surface as container lifecycle events on
this Docker daemon, so the watcher drains `docker events` and runs one reconcile
after a burst of them quiets down.

Coolify's own containers are ignored: restarting the control plane is not a
deployment, and reconciling on it only adds noise. Events are debounced so a
compose stack coming up (twelve containers in two seconds) costs one pass, not
twelve. A reconcile failure is retried three times and then left to the next
event; `coolify-cerulean-dns-health.service` is what turns sustained failure
into an alert.
"""
import json
import os
import select
import signal
import subprocess
import sys
import time

DEBOUNCE_SECONDS = 10
RETRY_SECONDS = 30
CONTROL_PLANE = ("coolify", "traefik")
PUBLISHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coolify-cerulean-dns.py")

_running = True


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


def stop(signum, _frame):
    global _running
    _running = False
    log(f"signal {signum}: stopping after this pass")


def relevant(event):
    if event.get("Type") != "container":
        return None
    name = (event.get("Actor") or {}).get("Attributes", {}).get("name", "")
    if not name or name.startswith(CONTROL_PLANE):
        return None
    return name


def publish():
    """One reconcile pass, retried; returns True when it succeeded."""
    for attempt in range(1, 4):
        result = subprocess.run([sys.executable, PUBLISHER, "--apply"], capture_output=True, text=True)
        output = (result.stdout or "").strip().replace("\n", " | ")
        if output:
            log(f"reconcile: {output}")
        if result.returncode == 0:
            return True
        log(f"reconcile failed ({result.returncode}): {(result.stderr or '').strip()[:300]}")
        if attempt < 3:
            time.sleep(RETRY_SECONDS)
    return False


def subscribe():
    return subprocess.Popen(
        [
            "docker", "events", "--format", "{{json .}}",
            "--filter", "type=container",
            "--filter", "event=start", "--filter", "event=die",
            "--filter", "event=destroy", "--filter", "event=stop",
            "--filter", "event=rename",
        ],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log("reconciling once at startup")
    publish()

    backoff = 5
    while _running:
        events = subscribe()
        log("watching docker events")
        pending = None
        while _running:
            ready, _, _ = select.select([events.stdout], [], [], 1.0)
            if events.poll() is not None:
                log(f"docker events exited ({events.returncode}); reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                break
            backoff = 5
            if not ready:
                if pending is not None and time.monotonic() - pending >= DEBOUNCE_SECONDS:
                    publish()
                    pending = None
                continue
            line = events.stdout.readline()
            if not line:
                continue
            try:
                name = relevant(json.loads(line))
            except ValueError:
                continue
            if not name:
                continue
            log(f"event: container {name} changed")
            pending = time.monotonic()
        if events.poll() is None:
            events.terminate()
            try:
                events.wait(timeout=10)
            except subprocess.TimeoutExpired:
                events.kill()


if __name__ == "__main__":
    main()
