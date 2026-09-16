#!/usr/bin/env python3
"""Migrate one stack group from this server to another — credentials, databases and all.

WHY THIS EXISTS. A group (``2-voice``, ``4-social``, …) is a set of compose
projects run from a group directory beside this repo. Moving it by hand means
reconstructing, from memory: which volumes hold databases, which ``.env`` files
carry credentials, which bind mounts hold uploads, and the order things must
come back up in. Miss one and the new host serves an empty product that *looks*
healthy. This script walks the running containers instead, and moves what they
actually use — the same discipline ``migrate-studio-data.py`` applies to one
volume, applied to a whole group.

    # OLD SERVER — stop the group so databases are consistent, then pack
    python3 scripts/migrate-stack.py pack 2 --stop --out /tmp/voice-migrate
    scp /tmp/voice-migrate/migrate-voice-*.tgz newhost:/tmp/

    # NEW SERVER — the group's repos must already be checked out beside ips/
    python3 scripts/migrate-stack.py restore --in /tmp/migrate-voice-*.tgz

WHAT IT MOVES, and why each thing is the thing it is:

* **named docker volumes** — the databases (Postgres, MariaDB, Meilisearch,
  redis persistence) and whatever else a service persists. Tarring the volume
  while its containers are stopped is a consistent copy of the database *and*
  its schema *and* its users, with no engine-specific dump tooling to get
  wrong. This is why ``--stop`` exists and why pack refuses to run without it
  (``--live`` overrides, loudly, for smoke tests).
* **every ``.env`` under the group directory** — the credentials. Copied with
  0600, never printed. ``vault://`` references are *not* values; pack also
  reads the referenced secrets out of Cerulean Vault (KV v2) and stores them in
  the bundle, so the new host can restore them into its Vault rather than
  pointing at the old one across a move.
* **bind mounts under the group directory** — uploads, staged sites, TLS
  material. Bind mounts *outside* the group directory are recorded in the
  manifest instead of packed, because they usually belong to another group's
  stack and packing them silently would migrate half of that group too.
* **images** — ``docker save`` of everything the containers run, so the new
  host does not need the registry or a build to come back (``--no-images``
  skips this and lets ``compose up`` rebuild).

Restore brings the group up with ``docker compose up -d`` in the recorded
order, against the restored volumes and env files, and finishes with a status
summary. It refuses to overwrite existing volumes or env files without
``--force`` — a bundle unpacked over a live deployment is exactly the
"mix two deployments" mistake the Studio migration tool refuses.

The bundle is one tarball plus a manifest with SHA-256 per member, so ``restore``
can prove what it is about to write is what was packed.

Nothing here prints a secret: values are written to the bundle only.

Usage:
  migrate-stack.py pack <group> --stop --out DIR [--no-images]
  migrate-stack.py restore --in BUNDLE [--force] [--skip-volumes] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GROUPS = {
    "1": "primary",
    "2": "voice",
    "3": "media",
    "4": "social",
    "5": "dev",
}
REPO_ROOT = Path(__file__).resolve().parent.parent
# Where the group directories live relative to this repo (<root>/<n>-<name>).
ROOT_DIR = REPO_ROOT.parent

VOLUME_EXCLUDES = ("__pycache__",)

# Directories that never hold state worth moving, skipped when collecting env
# files and bind mounts.
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "builds", "__pycache__", ".venv"}


def sh(args: list[str], *, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, check=check, input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()[-400:]
        raise SystemExit(f"command failed: {' '.join(args)}\n{detail}") from exc


def docker(*args: str, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
    return sh(["docker", *args], check=check, input=input)


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def group_dir(group: str) -> Path:
    name = GROUPS.get(group.lstrip("-"))
    if name is None:
        raise SystemExit(f"unknown group {group!r} — known: {', '.join(GROUPS)}")
    d = ROOT_DIR / f"{group.lstrip('-')}-{name}"
    if not d.is_dir():
        raise SystemExit(f"group directory {d} does not exist on this server")
    return d


def compose_projects(group_path: Path) -> list[dict]:
    """Every compose file under the group dir, in a stable restore order.

    The group-level ``docker-compose.yml`` comes first (it usually defines the
    network the repos join); everything else follows path-sorted, so a repo's
    own stack starts after the mesh exists.
    """
    files = sorted(group_path.rglob("docker-compose*.yml"))
    files = [f for f in files if not any(part in SKIP_DIRS for part in f.parts)]
    group_level = [f for f in files if f.parent == group_path]
    rest = [f for f in files if f.parent != group_path]
    ordered = group_level + rest
    return [
        {
            "path": str(f.relative_to(ROOT_DIR)),
            "dir": str(f.parent.relative_to(ROOT_DIR)),
            "file": f.name,
        }
        for f in ordered
    ]


def group_containers(group_path: Path) -> list[dict]:
    """Every compose-managed container whose project lives under this group dir."""
    out = docker("ps", "-a", "--filter", "label=com.docker.compose.project", "-q")
    records = []
    for cid in out.stdout.decode().split():
        inspect = json.loads(docker("inspect", cid).stdout.decode())[0]
        labels = inspect.get("Config", {}).get("Labels") or {}
        workdir = labels.get("com.docker.compose.project.working_dir", "")
        try:
            under = Path(workdir).resolve().is_relative_to(group_path.resolve())
        except (ValueError, OSError):
            under = False
        if not under:
            continue
        mounts = []
        for m in inspect.get("Mounts", []):
            if m["Type"] == "volume":
                mounts.append({"type": "volume", "name": m["Name"], "dest": m["Destination"]})
            elif m["Type"] == "bind":
                mounts.append({"type": "bind", "source": m["Source"], "dest": m["Destination"]})
        records.append({
            "name": inspect["Name"].lstrip("/"),
            "project": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
            "image": inspect.get("Config", {}).get("Image", ""),
            "running": inspect.get("State", {}).get("Running", False),
            "mounts": mounts,
            "compose_dir": workdir,
        })
    return records


def env_files(group_path: Path) -> list[Path]:
    found = []
    for p in sorted(group_path.rglob(".env")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        found.append(p)
    root_env = REPO_ROOT / ".env"
    if root_env.is_file():
        found.append(root_env)
    return found


VAULT_REF_PREFIX = "vault://"


def vault_refs(env_path: Path) -> dict[str, str]:
    """The ``vault://<mount>/<path>#<key>`` references in one env file → their keys."""
    refs: dict[str, str] = {}
    for line in env_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value.startswith(VAULT_REF_PREFIX):
            refs[key.strip()] = value
    return refs


def parse_vault_ref(ref: str) -> tuple[str, str, str]:
    """``vault://cerulean/distro#KEY`` → (``cerulean``, ``distro``, ``KEY``)."""
    body = ref[len(VAULT_REF_PREFIX):]
    mount, _, rest = body.partition("/")
    path, _, key = rest.partition("#")
    if not mount or not path or not key:
        raise ValueError(f"malformed vault reference: {ref}")
    return mount, path, key


def vault_creds(env_candidates: list[Path]) -> tuple[str, str] | None:
    """Vault address + token, from the environment first, then the repo's .env.

    Both docker-isms are handled here because pack runs on the host, not inside
    a compose network: ``VAULT_ADDR=http://vault:8200`` names the compose
    service, which only resolves from inside a container, so the address is
    rewritten to the host-published 127.0.0.1. And cerulean's .env carries an
    empty VAULT_TOKEN with the real one in VAULT_TOKEN_FILE — a path inside the
    vault container — so token files are tried as-is and then remapped to the
    estate's token directory (1-primary/cerulean/data/vault/token/)."""
    addr = os.environ.get("VAULT_ADDR", "")
    token = os.environ.get("VAULT_TOKEN", "")
    token_files = [p for p in (os.environ.get("VAULT_TOKEN_FILE", ""),) if p]
    for env_path in env_candidates:
        try:
            text = env_path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("VAULT_ADDR=") and not addr:
                addr = line.split("=", 1)[1].strip()
            elif line.startswith("VAULT_TOKEN="):
                candidate = line.split("=", 1)[1].strip()
                if candidate and not token:
                    token = candidate  # first non-empty wins; empty never clobbers
            elif line.startswith("VAULT_TOKEN_FILE=") and line.split("=", 1)[1].strip():
                token_files.append(line.split("=", 1)[1].strip())

    # compose-isms -> host loopback: pack runs on the host, where neither the
    # compose service name (``vault``) nor ``host.docker.internal`` resolves;
    # the services themselves are published on 127.0.0.1.
    if addr:
        parts = urllib.parse.urlsplit(addr)
        if parts.hostname in ("vault", "host.docker.internal"):
            netloc = f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1"
            addr = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))

    if not token:
        for tf in token_files:
            for path in (Path(tf), ROOT_DIR / "1-primary/cerulean/data/vault/token" / Path(tf).name):
                try:
                    candidate = path.read_text(errors="replace").strip()
                except OSError:
                    continue
                if candidate:
                    token = candidate
                    break
            if token:
                break

    if addr and token:
        return addr.rstrip("/"), token
    return None


def vault_read(addr: str, token: str, mount: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{addr}/v1/{mount}/data/{path}", headers={"X-Vault-Token": token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.load(resp)
    return (body.get("data") or {}).get("data") or {}


def vault_write(addr: str, token: str, mount: str, path: str, data: dict) -> None:
    req = urllib.request.Request(
        f"{addr}/v1/{mount}/data/{path}",
        data=json.dumps({"data": data}).encode(),
        headers={"X-Vault-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def collect_vault_secrets(all_refs: dict[str, dict[str, str]], creds) -> dict:
    """Read every referenced secret from Vault, grouped by secret path."""
    if not creds:
        return {"reachable": False, "secrets": {}, "note": "no VAULT_ADDR/VAULT_TOKEN — references copied as references"}
    addr, token = creds
    secrets: dict[str, dict] = {}
    for env_name, refs in all_refs.items():
        for ref in refs.values():
            mount, path, key = parse_vault_ref(ref)
            secret_id = f"{mount}/{path}"
            if secret_id not in secrets:
                try:
                    secrets[secret_id] = {"mount": mount, "path": path, "data": vault_read(addr, token, mount, path)}
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    secrets[secret_id] = {"mount": mount, "path": path, "data": {}, "error": str(exc)[:200]}
    return {"reachable": bool(creds), "secrets": secrets}


def tar_volume(volume: str, out_path: Path) -> None:
    """Tar a named volume read-only, streaming from a throwaway alpine container."""
    result = docker("run", "--rm", "-v", f"{volume}:/src:ro", "alpine:3.20",
                    "tar", "cf", "-", "-C", "/src", ".")
    with open(out_path, "wb") as fh:
        fh.write(result.stdout)


def tar_tree(source: Path, out_path: Path) -> None:
    result = sh(["tar", "cf", "-", "-C", str(source.parent), source.name])
    with open(out_path, "wb") as fh:
        fh.write(result.stdout)


# ── pack ──────────────────────────────────────────────────────────────────────


def cmd_pack(args: argparse.Namespace) -> int:
    group_path = group_dir(args.group)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    gnum = args.group.lstrip("-")
    gname = GROUPS[gnum]
    container_records = group_containers(group_path)
    projects = compose_projects(group_path)

    print(f"group {gnum}-{gname}: {group_path}")
    print(f"  compose files : {len(projects)}")
    print(f"  containers    : {len(container_records)} ({sum(1 for c in container_records if c['running'])} running)")

    # ── stop the group so database files are consistent ──
    running = [c["name"] for c in container_records if c["running"]]
    if running and args.live:
        print(f"  LIVE PACK: {len(running)} container(s) stay running — DB files may be inconsistent")
    elif running and args.stop:
        print(f"  stopping {len(running)} container(s) for a consistent copy…")
        for name in running:
            docker("stop", name)
        # wait for them to actually exit
        for _ in range(30):
            still = [c for c in group_containers(group_path) if c["running"]]
            if not still:
                break
            time.sleep(1)
    elif running:
        print("  refusing: containers are running and neither --stop nor --live was given", file=sys.stderr)
        return 2

    # ── volumes used by these containers ──
    volume_names = sorted({m["name"] for c in container_records for m in c["mounts"] if m["type"] == "volume"})
    bind_outside = sorted({m["source"] for c in container_records for m in c["mounts"]
                           if m["type"] == "bind" and not Path(m["source"]).resolve().is_relative_to(group_path.resolve())})
    bind_inside = sorted({m["source"] for c in container_records for m in c["mounts"]
                          if m["type"] == "bind" and Path(m["source"]).resolve().is_relative_to(group_path.resolve())})
    print(f"  volumes       : {len(volume_names)}")
    print(f"  bind mounts   : {len(bind_inside)} inside the group dir, {len(bind_outside)} outside (recorded, not packed)")
    for src in bind_outside:
        print(f"    ⚠ outside bind mount, migrate by hand: {src}")

    # ── env files + vault secrets ──
    envs = env_files(group_path)
    all_refs = {}
    for e in envs:
        refs = vault_refs(e)
        if refs:
            all_refs[str(e.relative_to(ROOT_DIR))] = refs
    creds = vault_creds(envs + [REPO_ROOT / ".env", ROOT_DIR / "1-primary/cerulean/.env"])
    # The cerulean .env is deliberately last: group-local config wins, and
    # VAULT_ADDR/TOKEN live in cerulean's env, not the group being packed.
    vault_bundle = collect_vault_secrets(all_refs, creds)
    ref_count = sum(len(v) for v in all_refs.values())
    print(f"  env files     : {len(envs)} ({ref_count} vault:// references)")
    if vault_bundle.get("reachable"):
        ok_count = sum(1 for s in vault_bundle["secrets"].values() if not s.get("error"))
        print(f"  vault secrets : {ok_count}/{len(vault_bundle['secrets'])} secret paths read")
    else:
        print("  vault secrets : not read (" + vault_bundle.get("note", "") + ")")

    # ── images ──
    images = sorted({c["image"] for c in container_records if c["image"]})
    if args.no_images:
        print(f"  images        : skipped (--no-images), compose will pull/build")
        images_to_save: list[str] = []
    else:
        images_to_save = images
        print(f"  images        : {len(images_to_save)}")

    bundle = {
        "v": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "group": gnum,
        "group_name": gname,
        "source_host": socket.gethostname(),
        "source_root": str(ROOT_DIR),
        "projects": projects,
        "containers": container_records,
        "volumes": volume_names,
        "binds_inside": bind_inside,
        "binds_outside": bind_outside,
        "env_files": [str(e.relative_to(ROOT_DIR)) for e in envs],
        "vault_refs": all_refs,
        "vault": vault_bundle,
        "images": images_to_save,
    }

    manifest = {"bundle.json": bundle}
    members: list[tuple[str, Path]] = []
    tmp = Path(tempfile.mkdtemp(prefix="migrate-stack-"))
    try:
        (tmp / "bundle.json").write_text(json.dumps(bundle, indent=2))
        members.append(("bundle.json", tmp / "bundle.json"))

        for env_rel in bundle["env_files"]:
            src = ROOT_DIR / env_rel
            dst = tmp / "env" / env_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            members.append((f"env/{env_rel}", dst))

        for vol in volume_names:
            dst = tmp / "volumes" / f"{vol}.tar"
            dst.parent.mkdir(parents=True, exist_ok=True)
            print(f"    packing volume {vol}")
            tar_volume(vol, dst)
            members.append((f"volumes/{vol}.tar", dst))

        for rel in bundle["binds_inside"]:
            src = Path(rel)
            dst = tmp / "binds" / f"{hashlib.sha256(rel.encode()).hexdigest()[:12]}.tar"
            dst.parent.mkdir(parents=True, exist_ok=True)
            print(f"    packing bind mount {rel}")
            tar_tree(src, dst)
            members.append((f"binds/{dst.name}", dst))

        if images_to_save:
            print(f"    packing {len(images_to_save)} image(s) — this is the big part")
            # The file NAME must carry .gz: bundle arcnames come from disk names,
            # and restore looks for images.tar.gz — calling it images.tar made
            # every bundle fail restore's member check.
            dst = tmp / "images.tar.gz"
            result = docker("save", *images_to_save)
            # gzip at level 1: images are already compressed layers, gzip mostly
            # buys something only on the metadata
            import gzip
            with open(dst, "wb") as fh:
                with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=1) as gz:
                    gz.write(result.stdout)
            members.append(("images.tar.gz", dst))

        # manifest with per-member hashes, so restore can prove the bundle
        manifest["members"] = {
            name: {"sha256": digest_file(path), "bytes": path.stat().st_size}
            for name, path in members
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))

        stamp = time.strftime("%Y%m%d-%H%M%S")
        bundle_path = out_dir / f"migrate-{gname}-{stamp}.tgz"
        with tarfile.open(bundle_path, "w:gz") as tf:
            for path in sorted(tmp.rglob("*")):
                if path.is_file():
                    tf.add(path, arcname=str(path.relative_to(tmp)))
        size = bundle_path.stat().st_size
        print(f"\n  bundle: {bundle_path} ({size / 1e6:.1f} MB)")
        print("  next: scp it to the new server and run:")
        print(f"    python3 scripts/migrate-stack.py restore --in {bundle_path.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return 0


# ── restore ───────────────────────────────────────────────────────────────────


def verify_members(bundle_dir: Path, manifest: dict) -> None:
    members = manifest.get("members") or {}
    for name, meta in members.items():
        path = bundle_dir / name
        if not path.is_file():
            raise SystemExit(f"bundle is missing member {name} — the archive is incomplete")
        got = digest_file(path)
        if got != meta["sha256"]:
            raise SystemExit(f"member {name} does not match its manifest hash — refusing to restore")


def extract_bundle(archive: Path, workdir: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(workdir)  # noqa: S202 - members are hash-verified below
    manifest = json.loads((workdir / "manifest.json").read_text())
    verify_members(workdir, manifest)
    return manifest


def existing_volumes(names: list[str]) -> list[str]:
    if not names:
        return []
    out = docker("volume", "ls", "--format", "{{.Name}}")
    present = set(out.stdout.decode().split())
    return [n for n in names if n in present]


def cmd_restore(args: argparse.Namespace) -> int:
    archive = Path(args.archive).resolve()
    if not archive.is_file():
        raise SystemExit(f"no such bundle: {archive}")

    if args.dry_run:
        # Read the manifest without extracting: useful to see what a restore would do.
        with tarfile.open(archive, "r:gz") as tf:
            manifest = json.load(tf.extractfile("manifest.json"))
        bundle = manifest["bundle.json"]
        print(f"group {bundle['group']}-{bundle['group_name']} packed on {bundle['created']} from {bundle['source_host']}")
        print(f"  compose projects : {len(bundle['projects'])}")
        for p in bundle["projects"]:
            print(f"    {p['path']}")
        print(f"  containers       : {len(bundle['containers'])}")
        print(f"  volumes          : {len(bundle['volumes'])}")
        print(f"  env files        : {len(bundle['env_files'])}")
        print(f"  vault secret paths: {len((bundle.get('vault') or {}).get('secrets') or {})}")
        print(f"  images           : {len(bundle['images'])}")
        conflicts = existing_volumes(bundle["volumes"])
        if conflicts:
            print(f"  ⚠ {len(conflicts)} volume(s) already exist here: {', '.join(conflicts[:8])}")
            print("    restore will refuse without --force")
        return 0

    with tempfile.TemporaryDirectory(prefix="migrate-restore-") as td:
        workdir = Path(td)
        manifest = extract_bundle(archive, workdir)
        bundle = manifest["bundle.json"]
        gnum, gname = bundle["group"], bundle["group_name"]
        print(f"restoring group {gnum}-{gname} (packed {bundle['created']} on {bundle['source_host']})")

        # ── refuse to mix deployments ──
        conflicts = existing_volumes(bundle["volumes"])
        if conflicts and not args.force:
            print(f"\n  refusing: {len(conflicts)} volume(s) already exist on this host:", file=sys.stderr)
            for name in conflicts:
                print(f"    {name}", file=sys.stderr)
            print("\n  restore with --force to overwrite them, or remove them first.", file=sys.stderr)
            return 2

        # ── env files ──
        for env_rel in bundle["env_files"]:
            src = workdir / "env" / env_rel
            dst = ROOT_DIR / env_rel
            if dst.exists() and not args.force:
                print(f"  refusing to overwrite {env_rel} (use --force)", file=sys.stderr)
                return 2
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            print(f"  env        {env_rel}")

        # ── vault secrets: restore into THIS host's vault ──
        vault = bundle.get("vault") or {}
        secrets = vault.get("secrets") or {}
        if secrets:
            # Same candidate list as pack — the group's own envs (just restored,
            # possibly carrying their own token files) then ips/.env then
            # cerulean's. ips/.env alone never holds Vault creds, which is why
            # every restore used to punt with "write them later".
            group_dir_guess = ROOT_DIR / f"{gnum}-{gname}"
            candidates = (sorted(group_dir_guess.glob("*/.env"))
                          + sorted(group_dir_guess.glob("*/*/.env"))
                          + [REPO_ROOT / ".env", ROOT_DIR / "1-primary/cerulean/.env"])
            creds = vault_creds(candidates)
            if creds:
                addr, token = creds
                written = skipped = failed = 0
                for secret_id, secret in secrets.items():
                    if secret.get("error"):
                        failed += 1
                        continue
                    try:
                        existing = vault_read(addr, token, secret["mount"], secret["path"])
                    except (urllib.error.URLError, OSError, ValueError):
                        existing = {}
                    merged = {**existing, **(secret.get("data") or {})}
                    try:
                        vault_write(addr, token, secret["mount"], secret["path"], merged)
                        written += 1
                    except (urllib.error.URLError, OSError, ValueError) as exc:
                        print(f"    ⚠ could not write {secret_id}: {str(exc)[:120]}", file=sys.stderr)
                        failed += 1
                print(f"  vault      {written} written, {skipped} unchanged, {failed} failed (union merge)")
            else:
                print("  vault      no VAULT_ADDR/VAULT_TOKEN here — secrets are in the bundle, write them later")

        # ── volumes ──
        for vol in bundle["volumes"]:
            src = workdir / "volumes" / f"{vol}.tar"
            if not src.is_file():
                print(f"  ⚠ volume {vol}: no data in bundle, skipping", file=sys.stderr)
                continue
            print(f"  volume     {vol}")
            docker("volume", "create", vol)
            docker("run", "--rm", "-v", f"{vol}:/dst", "alpine:3.20",
                   "sh", "-c", "cd /dst && find . -mindepth 1 -delete",
                   check=True)
            # -i is load-bearing: the tar stream arrives on stdin and docker
            # drops it unless told to keep it, which once made every volume
            # restore "succeed" with an empty volume.
            sh(["docker", "run", "--rm", "-i", "-v", f"{vol}:/dst", "alpine:3.20",
                "tar", "xf", "-", "-C", "/dst"], input=src.read_bytes())

        # ── bind mounts inside the group dir ──
        for rel in bundle["binds_inside"]:
            src = workdir / "binds" / f"{hashlib.sha256(rel.encode()).hexdigest()[:12]}.tar"
            if not src.is_file():
                continue
            target = ROOT_DIR / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            print(f"  bind       {rel}")
            sh(["tar", "xf", str(src), "-C", str(target.parent)])

        # ── images ──
        images_tar = workdir / "images.tar.gz"
        if images_tar.is_file():
            print(f"  images     loading {len(bundle['images'])} — the slow part")
            import gzip
            payload = gzip.decompress(images_tar.read_bytes())
            docker("load", input=payload)

        # ── bring the group up, in the recorded order ──
        print()
        for project in bundle["projects"]:
            proj_dir = ROOT_DIR / project["dir"]
            if not (proj_dir / project["file"]).is_file():
                print(f"  ⚠ {project['path']} does not exist on this host — check out the repo first", file=sys.stderr)
                continue
            print(f"  up         {project['path']}")
            docker("compose", "-f", str(proj_dir / project["file"]), "up", "-d", check=False)

        print()
        print("  done. Check:")
        print(f"    ./stack.sh status {gnum}")
        print("    containers that were running before should be healthy again;")
        print("    anything that needs a registry key or a DNS change is on you.")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="pack a group: volumes, env, vault secrets, images")
    p_pack.add_argument("group", help="group number 1-5 (e.g. 2 for voice)")
    p_pack.add_argument("--out", required=True, help="directory for the bundle")
    p_pack.add_argument("--stop", action="store_true", help="stop running containers first (consistent databases)")
    p_pack.add_argument("--live", action="store_true", help="pack while containers run (risky for DBs)")
    p_pack.add_argument("--no-images", action="store_true", help="skip docker save; restore rebuilds/pulls")
    p_pack.set_defaults(func=cmd_pack)

    p_restore = sub.add_parser("restore", help="restore a bundle onto this host")
    p_restore.add_argument("--in", dest="archive", required=True, help="bundle .tgz")
    p_restore.add_argument("--force", action="store_true", help="overwrite existing volumes and env files")
    p_restore.add_argument("--dry-run", action="store_true", help="show what a restore would do")
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
