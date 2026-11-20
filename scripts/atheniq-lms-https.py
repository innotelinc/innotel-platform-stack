#!/usr/bin/env python3
"""Make tutor render the platform's own URLs as https, and prove it afterwards.

WHY THIS EXISTS
---------------
On 2026-11-20 `https://next.atheniq.courses/catalog/courses` said "An unexpected
error occurred" while every container, certificate and vhost was healthy. The
catalog MFE asks the LMS for its configuration and builds every API call from the
answer; that answer was `LMS_BASE_URL: http://next.atheniq.courses` on a page served
over https, so the browser blocked each call as mixed content and the app's error
boundary showed a generic message.

Tutor builds those URLs from `ENABLE_HTTPS`, which is false here on purpose: turning
it on makes tutor's own Caddy demand port 443 and its own certificates, and 443 on
this estate belongs to the edge. So the scheme was corrected where tutor *reads* it —
in the templates and patches it renders from — which is what `tutor config save`
reproduces. A tutor **upgrade** replaces those files, and this script is the thing to
run afterwards.

WHAT IT DOES
------------
* Rewrites the three `ENABLE_HTTPS` scheme conditionals in `tutor/templates/**` and
  `tutormfe/patches/**` to a literal `https`. All three forms are in the tree; the
  `{{ "https" if ENABLE_HTTPS else "http" }}` one is easy to miss and is what left
  `STUDIO_BASE_URL` on http after the first pass.
* Appends `SECURE_PROXY_SSL_HEADER` and `CSRF_TRUSTED_ORIGINS` to the LMS settings
  patch, so Django trusts the edge's `X-Forwarded-Proto`.

Idempotent: a second run changes nothing, and `--check` reads the *rendered* settings
file instead of the templates.

AFTER A TUTOR UPGRADE
---------------------
    sudo python3 atheniq-lms-https.py
    TUTOR_ROOT=/home/tutor/tutor-next /opt/tutor/bin/tutor config save
    docker restart tutor_local-lms-1
    sudo python3 atheniq-lms-https.py --check \\
        /home/tutor/tutor-next/env/apps/openedx/settings/lms/production.py

The rendered file is the one Django loads, so `--check` is the statement that matters;
the templates are only how it gets that way. See docs/atheniq-lms-https-urls.md.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Every form tutor uses to turn `ENABLE_HTTPS` into a scheme, longest first.
SCHEME_FORMS: tuple[tuple[str, str], ...] = (
    ("{% if ENABLE_HTTPS %}https://{% else %}http://{% endif %}", "https://"),
    ("{% if ENABLE_HTTPS %}https{% else %}http{% endif %}", "https"),
    ('{{ "https" if ENABLE_HTTPS else "http" }}://', "https://"),
)

#: The patch appended to the LMS production settings, after the MFE config dict.
PROXY_BLOCK = """
# AthenIQ terminates TLS at the estate edge, so Django has to trust the proxy that
# forwarded the request, and the two public names have to pass the CSRF origin check.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = list(CSRF_TRUSTED_ORIGINS) + ["https://{{ LMS_HOST }}", "https://{{ CMS_HOST }}"]
"""

#: Where tutor keeps the sources it renders from, relative to site-packages.
SOURCE_ROOTS = ("tutor/templates", "tutormfe/patches")

#: The one patch that also has to carry the proxy trust.
LMS_PATCH = "tutormfe/patches/openedx-lms-production-settings"

PROXY_MARKER = "SECURE_PROXY_SSL_HEADER"

#: Hosts that are legitimately plain http even in production settings. A single-label
#: name (`meilisearch`, `redis`, `minio`) is a container inside the estate network, which
#: never reaches a browser; every platform URL has a dot, so that is the boundary.
PLAIN_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "www.w3.org"})


def rewrite_scheme(text: str) -> tuple[str, int]:
    """Literal https in place of every `ENABLE_HTTPS` conditional. Returns (text, changes)."""
    changes = 0
    for old, new in SCHEME_FORMS:
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            changes += count
    return text, changes


def ensure_proxy_settings(text: str) -> tuple[str, int]:
    """Append the proxy-trust block once. Returns (text, 1) or (text, 0) if it is there."""
    if PROXY_MARKER in text:
        return text, 0
    return text.rstrip("\n") + "\n" + PROXY_BLOCK, 1


def candidate_files(site_packages: Path) -> list[Path]:
    """Every regular file under the two roots tutor renders from, in a stable order."""
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        base = site_packages / root
        if base.is_dir():
            files.extend(sorted(path for path in base.rglob("*") if path.is_file()))
    return files


def apply(site_packages: Path, dry_run: bool = False) -> list[tuple[Path, int]]:
    """Rewrite the templates under `site_packages`. Returns the files it changed."""
    if not (site_packages / "tutor").is_dir():
        raise SystemExit(f"atheniq-lms-https: {site_packages} is not a tutor site-packages directory")
    touched: list[tuple[Path, int]] = []
    for path in candidate_files(site_packages):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        new, changes = rewrite_scheme(text)
        if path.as_posix().endswith(LMS_PATCH):
            new, extra = ensure_proxy_settings(new)
            changes += extra
        if not changes:
            continue
        touched.append((path, changes))
        if not dry_run:
            path.write_text(new)
    return touched


def stale_urls(text: str) -> list[str]:
    """`http://host` occurrences a browser would block, in order and without repeats."""
    found: list[str] = []
    for match in re.finditer(r"http://([A-Za-z0-9_.-]+)", text):
        host = match.group(1)
        if host in PLAIN_HOSTS or "." not in host:
            continue
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def check(settings_files: list[Path]) -> int:
    """Report plain-http platform URLs in already-rendered settings. 1 when any remain."""
    problems = 0
    for path in settings_files:
        try:
            text = path.read_text()
        except OSError as error:
            print(f"{path}: {error}", file=sys.stderr)
            problems += 1
            continue
        urls = stale_urls(text)
        for url in urls:
            print(f"{path}: {url}")
        problems += len(urls)
    if problems:
        print(f"atheniq-lms-https: {problems} plain-http URL(s) still rendered")
        return 1
    print(f"atheniq-lms-https: {len(settings_files)} rendered file(s) carry no plain-http platform URL")
    return 0


def default_site_packages() -> Path:
    """The site-packages tutor is installed into, across python minor versions."""
    for candidate in sorted(Path("/opt/tutor/lib").glob("python3*/site-packages")):
        if (candidate / "tutor").is_dir():
            return candidate
    return Path("/opt/tutor/lib/python3.12/site-packages")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--site-packages",
        type=Path,
        default=None,
        help="tutor's site-packages (default: the one under /opt/tutor/lib)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the files that would change and write nothing",
    )
    parser.add_argument(
        "--check",
        nargs="+",
        type=Path,
        metavar="RENDERED_SETTINGS",
        help="read rendered settings instead of patching, and fail on any http:// platform URL",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check(args.check)

    site_packages = args.site_packages or default_site_packages()
    touched = apply(site_packages, dry_run=args.dry_run)
    verb = "would change" if args.dry_run else "changed"
    for path, changes in touched:
        print(f"{verb} {changes:>3}  {path}")
    if not touched:
        print(f"atheniq-lms-https: {site_packages} already renders https, nothing to do")
    else:
        print(f"atheniq-lms-https: {verb} {len(touched)} file(s); run `tutor config save` next")
    return 0


if __name__ == "__main__":
    sys.exit(main())
