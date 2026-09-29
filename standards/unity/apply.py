#!/usr/bin/env python3
"""Apply Unity to a project's GitHub Pages landing page.

The estate's landing pages were all built from one house template: an inlined
`<style>` block whose whole palette is CSS custom properties, with a per-repo
prefix (`--p-`, `--d-`, `--onyx-`). That shape is what makes this a mapping rather
than a rewrite — the layout CSS is already written against variables, so pointing
those variables at Unity's tokens re-themes the page without touching a single
layout rule.

Two things it does that a find-and-replace would not:

  1. **Every literal that matches the page's own palette is rewritten too.** The
     pages are ~95% variable-driven, and the remaining 5% — a topbar background
     with alpha, a brand glow in a radial-gradient, the ink on a primary button —
     is exactly where a "dark mode" was hard-coded. Those are mapped to tokens with
     `color-mix()`, so light mode is actually readable rather than theoretically
     supported. Anything left over is *reported*, never silently left behind.

  2. **The scheme is chosen from the accent hue**, so a product keeps its identity
     inside one theme instead of every page being flattened to the same colour.

Run it from anywhere:

    python3 standards/unity/apply.py ../ --dry-run     # show what would change
    python3 standards/unity/apply.py ../               # write

It is idempotent: a page that already links `unity-theme.css` is skipped, so
running it twice is harmless and running it after a hand-edit is a no-op.
"""

from __future__ import annotations

import argparse
import colorsys
import pathlib
import re
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
THEME_CSS = HERE / "unity-theme.css"
THEME_JS = HERE / "unity-theme.js"

# ── The mapping ───────────────────────────────────────────────────────────────
#
# Keyed by the *suffix* of the page's variable, so the prefix does not matter.
# Every Unity token has one meaning; this is where a page's name for it is
# translated. A suffix that is not listed is left alone — the page is the owner of
# anything Unity has no word for.
ALIASES = {
    "bg": "--canvas",
    "bg-elevated": "--surface-muted",
    "surface": "--surface",
    "surface-hover": "--surface-sunken",
    "border": "--line",
    "text": "--ink",
    "text-secondary": "--ink-soft",
    "text-muted": "--ink-faint",
    "accent": "--brand",
    # Unity has no `-hover`: a hover is a second tone of the brand, and the ring is
    # the one Unity already describes. Mapping both to `--brand` would make a hover
    # invisible, which reads as a broken button.
    "accent-hover": "--brand-ring",
    "accent-tint": "--brand-soft",
    "accent-2": "--brand",
    "success": "--ok",
    "warning": "--attention",
    "danger": "--bad",
    "info": "--info",
    # Bare-prefix pages abbreviate: they never had a name for the role, only for the
    # shade. These are the same roles under the short spelling.
    "elev": "--surface-muted",
    "elevated": "--surface-muted",
    "elev-1": "--surface-muted",
    "surface-2": "--surface-muted",
    "surface-alt": "--surface-muted",
    "bg-2": "--surface-muted",
    "panel": "--surface",
    "card": "--surface",
    "card-bg": "--surface",
    "hover": "--surface-sunken",
    "secondary": "--ink-soft",
    "muted": "--ink-faint",
    "fg": "--ink",
    "fg-soft": "--ink-soft",
    "fg-muted": "--ink-faint",
    "text-2": "--ink-soft",
    "text-3": "--ink-faint",
    "tint": "--brand-soft",
    "accent-soft": "--brand-soft",
    "ok": "--ok",
    "warn": "--attention",
    "error": "--bad",
    "danger-soft": "--bad-soft",
    "ok-soft": "--ok-soft",
    "warn-soft": "--attention-soft",
    "warning-soft": "--attention-soft",
    "success-soft": "--ok-soft",
    "info-soft": "--info-soft",
    "border-strong": "--line-strong",
    "line": "--line",
    "line-strong": "--line-strong",
}

# Literal colours a page uses *outside* its own palette block. A page in this shape is
# almost always naming a second accent it never declared, and these are the few the
# estate actually contains. Kept as an explicit table rather than a hue guess: a
# nearest-colour heuristic would silently rewrite a shade somebody chose on purpose,
# and there is no way to review that from a diff.
EXTRA_HEX = {
    "#14b8a6": "--info",     # teal accent on a surface nobody declared
    "#d97706": "--attention",  # amber gradient stop
    "#16233d": "--surface-muted",  # a dark navy wash in a hero glow
}

# Secondary accents, named by hue. A page's "gold" and Unity's "attention" are the
# same role; the page simply had no word for the role, only for the colour.
HUE_WORDS = {
    "gold": "--attention",
    "amber": "--attention",
    "yellow": "--attention",
    "orange": "--attention",
    "teal": "--info",
    "cyan": "--info",
    "sky": "--info",
    "green": "--ok",
    "lime": "--ok",
    "red": "--bad",
    "rose": "--bad",
    "pink": "--bad",
    "purple": "--brand",
    "violet": "--brand",
    "indigo": "--brand",
}

# Radii: Unity owns `--radius-sm`, `--radius` and `--radius-lg`. The pages use
# `--radius-sm/-md/-lg`, so `-sm` and `-lg` must be *removed* (not aliased — a
# declaration of `--radius-sm: var(--radius-sm)` is a cycle and resolves to
# nothing) and `-md` is the only one that needs a name the pages can still use.
RADIUS_FILE = {"radius-md": "--radius"}

# The switch, in plain markup. The landing pages have no framework, so this is the
# same control the React apps render, using the same `.ot-theme` classes the shared
# stylesheet already styles.
SWITCH = (
    '<div class="ot-theme" role="group" aria-label="Colour theme" data-unity-switch>'
    '<button type="button" data-theme-mode="system" aria-pressed="false">System</button>'
    '<button type="button" data-theme-mode="light" aria-pressed="false">Light</button>'
    '<button type="button" data-theme-mode="dark" aria-pressed="false">Dark</button>'
    "</div>"
)

SWITCH_SCRIPT = """<script>(function(){
  var api=window.UnityTheme||window.OntrakTheme;if(!api)return;
  function paint(){var now=api.mode();var all=document.querySelectorAll('[data-theme-mode]');
    for(var i=0;i<all.length;i++){all[i].setAttribute('aria-pressed',String(all[i].getAttribute('data-theme-mode')===now));}}
  var buttons=document.querySelectorAll('[data-theme-mode]');
  for(var i=0;i<buttons.length;i++){buttons[i].addEventListener('click',function(){api.setMode(this.getAttribute('data-theme-mode'));});}
  window.addEventListener('unity:theme',paint);window.addEventListener('ontrak:theme',paint);paint();
})();</script>"""

HEAD_ASSETS = (
    '<link rel="stylesheet" href="unity-theme.css">\n'
    '  <script>window.UNITY_DEFAULT_SCHEME = "{scheme}";</script>\n'
    '  <script src="unity-theme.js"></script>'
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def normalise_hex(value: str) -> str | None:
    """`#abc` / `#aabbcc` / `#aabbccdd` -> `#aabbcc`, lowercased. None if not a hex."""
    value = value.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{3,8}", value):
        return None
    body = value[1:]
    if len(body) in (3, 4):
        body = "".join(char * 2 for char in body[:3])
    return "#" + body[:6]


def rgba_to_hex(rgba: str) -> tuple[str, float] | None:
    """`rgba(1,2,3,0.4)` -> (`#010203`, 0.4). None if it is not an rgba we can read."""
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", rgba.strip())
    if not m:
        return None
    r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    return "#%02x%02x%02x" % (r, g, b), float(a) if a is not None else 1.0


def token_for(suffix: str | None) -> str | None:
    """The Unity token a page's variable name maps to, or None.

    One resolver, used by both the palette scan and the palette rebuild: two copies of
    this lookup is how the master list came to know about a page's gold while the
    rebuild did not, and the inconsistency showed up as a single un-themed variable.
    """
    if not suffix:
        return None
    return ALIASES.get(suffix) or HUE_WORDS.get(suffix)


def scheme_for(hex_colour: str | None) -> str:
    """Which Unity scheme suits this product, judged by its accent hue.

    Violet keeps the desk palette, a magenta reads as the security console, and
    everything else — cyan, blue, green, amber, gold, orange — takes the neutral
    operations palette, which is the one that does not argue with a house colour it
    was not built for.
    """
    if not hex_colour:
        return "operations"
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hue, _, _ = colorsys.rgb_to_hsv(r, g, b)
    degrees = hue * 360
    if 255 <= degrees <= 300:
        return "desk"      # violet
    if 300 < degrees <= 345:
        return "soc"       # magenta
    return "operations"


def percent(alpha: float) -> str:
    return f"{round(alpha * 100, 1):g}%"


def scan_declarations(text: str, rewrite) -> str:
    """Rewrite every declaration *value*, leaving selectors untouched.

    `rewrite(property, value)` is called once per declaration. Selectors are copied
    verbatim, which is the whole point: an id like `#ace` is a name, not a colour, and
    only the scanner knows which side of a `:` it is on. `{`, `}` and `;` all end a
    value, so a gradient that spans four lines is one declaration rather than four.

    A media query's `(min-width: 720px)` is passed through as well — it looks exactly
    like a declaration, and a rewriter that only cares about colours ignores it.
    """
    out: list[str] = []
    in_value = False
    segment_start = 0
    value_start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if not in_value and char == ":":
            in_value = True
            value_start = index + 1
            # Everything from the last delimiter to the colon is the property name —
            # which may still carry a selector prefix on one line, hence the split.
            property_name = text[segment_start:index].split("{")[-1]
            out.append(char)
            index += 1
            continue
        if in_value and char in ";{}":
            property_name = text[segment_start:value_start - 1].split("{")[-1].split(";")[-1]
            out.append(rewrite(property_name, text[value_start:index]))
            out.append(char)
            in_value = False
            segment_start = index + 1
            index += 1
            continue
        if not in_value:
            out.append(char)
        index += 1
    if in_value:
        property_name = text[segment_start:value_start - 1].split("{")[-1].split(";")[-1]
        out.append(rewrite(property_name, text[value_start:]))
    return "".join(out)


def luminance(hexed: str) -> float:
    """Rough perceived brightness, 0..1. Enough to answer "is this ink or a fill"."""
    r, g, b = (int(hexed[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ── The transform ─────────────────────────────────────────────────────────────
def transform(html: str, name: str, dry: bool) -> tuple[str, list[str]]:
    notes: list[str] = []

    if "unity-theme.css" in html:
        return html, ["already themed — skipped"]

    style_match = re.search(r"<style[^>]*>(.*?)</style>", html, re.S)
    if not style_match:
        return html, ["no inline <style> block — needs a hand"]
    css = style_match.group(1)

    root_match = re.search(r":root\s*\{.*?\}", css, re.S)
    if not root_match:
        return html, ["no :root palette block — needs a hand"]

    # The page's own vocabulary: every `--name: <hex>` it declares.
    declarations = re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", root_match.group(0))
    if not declarations:
        return html, ["the :root block declares no hex colours — needs a hand"]

    # Most pages namespace their tokens (`--p-bg`, `--onyx-bg`), but some declare them
    # bare (`--bg`, `--surface`). A bare page is still a perfectly good palette; the
    # only thing that changes is that its names already *are* the suffixes, so the
    # empty prefix is the correct reading rather than a reason to give up.
    prefix_match = re.search(r"(--[a-z0-9]+)-bg\s*:", root_match.group(0))
    if prefix_match:
        prefix = prefix_match.group(1)
    elif re.search(r"(?<![a-z0-9-])--bg\s*:", root_match.group(0)):
        prefix = ""
    else:
        return html, ["no `--bg` or `--<prefix>-bg` to derive the palette from — needs a hand"]

    def suffix_of(variable: str) -> str:
        """The part of a variable name that names its *role*.

        The pages mix two conventions in one block: product-prefixed variables
        (`--p-surface`) and shared unprefixed ones (`--radius-sm`, `--ease`). Stripping
        the prefix only when it is present is what lets `--radius-sm` be recognised as a
        radius — without this, `--radius-sm` looked like an unknown name and was copied
        through as a literal, which is one of the two things that kept light mode from
        being a real option.
        """
        if prefix and variable.startswith(prefix + "-"):
            return variable[len(prefix) + 1:]
        return variable[2:]


    hex_to_token: dict[str, str] = {}
    accent_hex: str | None = None
    for var, value in declarations:
        hexed = normalise_hex(value)
        if not hexed:
            continue
        suffix = suffix_of(var)
        # A second accent Unity has no name for — a house gold, a teal — is named after
        # the colour it *is*, because Unity has one token per *meaning* rather than one
        # per hue: a gold is `--attention`, which is what attention is for.
        token = token_for(suffix)
        if token:
            hex_to_token[hexed] = token
        if suffix in ("accent", "accent-2"):
            accent_hex = hexed

    if not hex_to_token:
        return html, [f"the prefix `{prefix}` matched none of Unity's names — needs a hand"]

    scheme = scheme_for(accent_hex)

    # ── 1. the palette block, replaced with aliases onto Unity ────────────────
    lines: list[str] = []
    for var, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", root_match.group(0)):
        suffix = suffix_of(var)
        if suffix in RADIUS_FILE:
            lines.append(f"      {var}: var({RADIUS_FILE[suffix]});")
            continue
        if suffix in ("radius-sm", "radius-lg"):
            continue  # Unity already owns these names; a self-alias would be a cycle
        token = token_for(suffix)
        if token:
            # A page whose names already *are* Unity's (`--surface: var(--surface)`) has
            # written a cycle, not an alias: a custom property that references itself is
            # invalid at computed-value time, which poisons every use of the token on the
            # page. Unity's own declaration is already in effect from the stylesheet, so
            # the right move is to drop the line rather than restate it.
            if token == var:
                continue
            lines.append(f"      {var}: var({token});")
            continue
        # Anything Unity has no word for stays as it was: motion and radii are not
        # colours, and the page owns its layout.
        lines.append(f"      {var}: {value.strip()};")
        if re.search(r"#[0-9a-fA-F]{3,8}\b", value):
            notes.append(f"{var} keeps a literal colour Unity has no token for")

    new_root = ":root {\n" + "\n".join(lines) + "\n    }"
    new_css = css.replace(root_match.group(0), new_root, 1)

    # ── 2. every literal below :root, mapped through the page's own palette ───
    body = new_css.replace(new_root, "", 1)
    leftovers: set[str] = set()

    def token_for_hex(hexed: str) -> str | None:
        return hex_to_token.get(hexed) or EXTRA_HEX.get(hexed)

    def swap_hex(match: re.Match[str]) -> str:
        hexed = normalise_hex(match.group(0))
        token = token_for_hex(hexed or "")
        if token:
            return f"var({token})"
        leftovers.add(match.group(0))
        return match.group(0)

    def swap_rgba(match: re.Match[str]) -> str:
        parsed = rgba_to_hex(match.group(0))
        if not parsed:
            return match.group(0)
        hexed, alpha = parsed
        token = token_for_hex(hexed)
        if token:
            # `color-mix` rather than a token per alpha: the pages use a dozen
            # opacities of the same two or three colours, and inventing `--brand-14`
            # would be inventing a vocabulary to describe arithmetic.
            return f"color-mix(in srgb, var({token}) {percent(alpha)}, transparent)"
        # A pure-black shadow is the one literal that is right in both modes, so it is
        # left alone rather than "fixed" into something worse.
        if hexed == "#000000" and alpha < 1:
            return match.group(0)
        leftovers.add(match.group(0))
        return match.group(0)

    # A declaration scanner rather than a regex over the whole document: `#abcdef` in a
    # selector is an *id*, not a colour, and a gradient's arguments can span lines, so
    # neither a document pass nor a line pass is right. This walks the CSS and hands
    # every declaration value — and only values — to the rewriter.
    def rewrite_value(prop: str, value: str) -> str:
        is_colour = prop.strip().endswith(("color", "background", "border", "shadow", "fill", "stroke"))
        if not is_colour and "gradient" not in value:
            return value

        def swap_dark_rgba(match: re.Match[str]) -> str:
            """A very dark colour with alpha is a page background showing through.

            That is the sticky top bar in every one of these pages, and it is the one
            literal whose *meaning* is unambiguous: `rgba(11, 13, 16, 0.72)` is the
            canvas at 72%, whatever the page happens to have called its canvas.
            """
            parsed = rgba_to_hex(match.group(0))
            if not parsed:
                return match.group(0)
            hexed, alpha = parsed
            if alpha < 1 and luminance(hexed) < 0.12:
                return f"color-mix(in srgb, var(--canvas) {percent(alpha)}, transparent)"
            return match.group(0)

        # The ink on a filled button is the one colour in these pages that is dark *on
        # purpose*; everywhere else dark means a background. `--brand-ink` exists for it
        # because in dark mode the brand is light and that ink has to flip.
        if prop.strip().endswith("color") and not prop.strip().endswith("background-color"):
            def swap_ink(match: re.Match[str]) -> str:
                hexed = normalise_hex(match.group(0))
                if hexed and luminance(hexed) < 0.25:
                    return "var(--brand-ink)"
                return match.group(0)
            value = re.sub(r"#[0-9a-fA-F]{3,8}\b", swap_ink, value)

        value = re.sub(r"#[0-9a-fA-F]{3,8}\b", swap_hex, value)
        value = re.sub(r"rgba?\([^)]*\)", swap_rgba, value)
        value = re.sub(r"rgba?\([^)]*\)", swap_dark_rgba, value)

        # A dark literal in a *background* is the last thing that would survive as-is
        # and look wrong in light mode: it is a surface, and the page did not have a
        # name for it. Anything brighter is left alone — a vivid leftover is a house
        # accent the page chose deliberately, and it is reported rather than replaced.
        if "background" in prop:
            def swap_dark_fill(match: re.Match[str]) -> str:
                hexed = normalise_hex(match.group(0))
                return "var(--surface-sunken)" if hexed and luminance(hexed) < 0.12 else match.group(0)
            value = re.sub(r"#[0-9a-fA-F]{3,8}\b", swap_dark_fill, value)

        # A border colour that was a shade nobody named still has an answer: it was a
        # divider, and the theme has one.
        if "border" in prop and re.search(r"#[0-9a-fA-F]{3,8}\b", value):
            value = re.sub(r"#[0-9a-fA-F]{3,8}\b", "var(--line-strong)", value)
        return value

    body = scan_declarations(body, rewrite_value)

    # Report what is *still* literal, not what was mentioned along the way: a colour the
    # border pass went on to replace is not a concern worth raising.
    for leftover in sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", body))):
        if normalise_hex(leftover) == "#000000":
            continue
        notes.append(f"left literal: {leftover}")

    # ── 3. the head: stylesheet, then the switch, then the page's own rules ───
    #
    # One replacement, carrying both the new palette and the two shared assets. Unity's
    # own order applies: the stylesheet first so the tokens exist, then the blocking
    # script so `<html>` already has the remembered mode before the first paint, then
    # the page's rules last so a layout rule always beats a primitive.
    new_style_tag = style_match.group(0).replace(css, new_root + "\n" + body, 1)
    html = html.replace(
        style_match.group(0),
        HEAD_ASSETS.format(scheme=scheme) + "\n  " + new_style_tag,
        1,
    )

    # ── 4. the scheme on <html> ───────────────────────────────────────────────
    html = re.sub(r"<html\b([^>]*)>", lambda m: f'<html{m.group(1)} data-scheme="{scheme}">', html, count=1)

    # ── 5. the switch, in the header nav ──────────────────────────────────────
    if "</nav>" in html:
        html = html.replace("</nav>", f"</nav>\n        {SWITCH}", 1)
    elif '<header class="topbar">' in html:
        # Nothing to hang it on: put it directly in the top bar.
        html = re.sub(
            r'(<a class="brand"[^>]*>.*?</a>)',
            lambda m: m.group(1) + "\n        " + SWITCH,
            html, count=1, flags=re.S,
        )
    else:
        notes.append("no header nav — the switch was placed nowhere")

    html = html.replace("</body>", f"  {SWITCH_SCRIPT}\n</body>", 1)
    notes.append(f"scheme: {scheme}  (accent {accent_hex or 'none'})")
    return html, notes


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the Unity theme to landing pages.")
    parser.add_argument("root", nargs="?", default=".", help="the directory holding the repos")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    if not THEME_CSS.is_file() or not THEME_JS.is_file():
        print(f"the theme is missing beside this script: {THEME_CSS}", file=sys.stderr)
        return 2

    root = pathlib.Path(args.root).resolve()
    # `**` because the estate nests repos by layer (`1-primary/cerulean`, `5-dev/atlas`),
    # and a one-level glob silently finds exactly one of them. The exclusions matter as
    # much: `zeus/.next/standalone/web/landing` is a *copy of the landing page inside a
    # build artefact*, and a theme written there is overwritten by the next build and
    # invisible to Pages.
    skip = ("node_modules", ".next", "dist", "build", "out", "vendor", "target", "coverage", ".git")
    targets = sorted(
        p for p in root.glob("**/web/landing")
        if p.is_dir() and not any(part in skip for part in p.parts)
    )

    if not targets:
        print(f"no web/landing directories found under {root}", file=sys.stderr)
        return 1

    changed = 0
    for landing in targets:
        index = landing / "index.html"
        if not index.is_file():
            continue
        name = landing.parent.parent.name
        html = index.read_text()
        new_html, notes = transform(html, name, args.dry_run)
        touched = new_html != html
        changed += 1 if touched else 0
        print(f"{'~' if touched else '='} {name:<22} " + "; ".join(notes))
        if touched and not args.dry_run:
            index.write_text(new_html)
            shutil.copyfile(THEME_CSS, landing / "unity-theme.css")
            shutil.copyfile(THEME_JS, landing / "unity-theme.js")

    print(f"\n{changed} of {len(targets)} landing page(s) {'would change' if args.dry_run else 'changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
