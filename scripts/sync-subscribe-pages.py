#!/usr/bin/env python3
"""sync_subscribe_pages.py — regenerate the shared subscribe portal's pages.

Every Innotel service gets its own subscription landing page, served by one
small nginx (nginx.conf here) that picks the page by Host header:

    subscribe.<service>.innotel.us  ->  pages/<service>.html
    subscribe.innotel.us (apex)     ->  pages/index.html  (all services)

The pages are GENERATED: each service repo owns its brand (name, tagline,
accent colour, logo mark) in web/landing/subscribe.json, and this script
renders the shared template (template.html) with that spec into
pages/<service>.html.

Prices are never baked in. Every page fetches its price list live from
Magnate's public API (/api/plans?service=<slug>) at view time, so the master
dashboard (admin.magnate.innotel.us) is the single place a price is set —
change it there and every subscribe page reflects it on next load.

Usage:
    python3 scripts/sync-subscribe-pages.py            # regenerate pages/
    python3 scripts/sync-subscribe-pages.py --check    # verify only

Reads:   <repo>/web/landing/subscribe.json     (one spec per service repo)
Writes:  web/subscribe/pages/<service>.html    (and pages/index.html)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STACK = Path(__file__).resolve().parent.parent
REPOS = STACK.parent                      # sibling product repos
OUT_DIR = STACK / "web" / "subscribe" / "pages"
TEMPLATE = STACK / "web" / "subscribe" / "template.html"

# Services we render. The slug is both the directory/file name and the
# ?service= value on Magnate's price API — they must stay identical.
SERVICES = [
    "magnate", "monarch", "zeus", "capstone", "oasis", "onyx", "signara",
    "atlas", "atheniq", "olympus", "plutus", "distro", "rizzaura", "zapit",
    "cerulean",
]

# repo dir name per service slug (only where they differ from the slug)
REPO_OF = {
    "monarch": "monarch-media-platform",
    "zeus": "zeus-pbx-platform",
    "capstone": "capstone-voice-aiagent-platform",
    "magnate": "magnate-subscription-platform",
    "onyx": "onyx-oss-platform",
    "oasis": "oasis-mail-platform",
    "signara": "signara-trust-platform",
    "rizzaura": "rizzaura-platform",
    "cerulean": "cerulean-dns-platform",
    "atheniq": "atheniq",
    "olympus": "olympus",
    "plutus": "plutus",
    "distro": "distro",
    "zapit": "zapit",
    "atlas": "atlas",
}

# Fallback brand specs for services whose repo has no subscribe.json yet.
# Anything the spec provides wins over these.
DEFAULTS = {
    "magnate": {"name": "Magnate", "tagline": "The billing platform for the whole stack", "accent": "#10b981"},
    "monarch": {"name": "Monarch", "tagline": "Your personal streaming empire", "accent": "#e50914"},
    "zeus": {"name": "Zeus", "tagline": "VoIP made simple", "accent": "#fbbf24"},
    "capstone": {"name": "Capstone", "tagline": "Voice AI that answers for you", "accent": "#a78bfa"},
    "oasis": {"name": "Oasis", "tagline": "Mail and collaboration, self-hosted", "accent": "#fbbf24"},
    "onyx": {"name": "Onyx", "tagline": "Object storage you own", "accent": "#38bdf8"},
    "signara": {"name": "Signara", "tagline": "Trust and signing infrastructure", "accent": "#38bdf8"},
    "atlas": {"name": "Atlas", "tagline": "The DevOps platform", "accent": "#22d3ee"},
    "atheniq": {"name": "AthenIQ", "tagline": "Learning, on your terms", "accent": "#a78bfa"},
    "olympus": {"name": "Olympus", "tagline": "The AI studio", "accent": "#f472b6"},
    "plutus": {"name": "PLUTUS", "tagline": "The AI shopping channel", "accent": "#f5b544"},
    "distro": {"name": "Distro", "tagline": "Build and ship, your way", "accent": "#22d3ee"},
    "rizzaura": {"name": "Rizz Aura", "tagline": "Your community, your rules", "accent": "#f472b6"},
    "zapit": {"name": "ZapIt", "tagline": "Short links, fast", "accent": "#38bdf8"},
    "cerulean": {"name": "Cerulean", "tagline": "One login for the whole platform", "accent": "#3aa0ff"},
}

# Accent tints used for the gradient wash, derived per page from --accent.
TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Subscribe to {NAME} — Innotel</title>
  <meta name="description" content="{TAGLINE}. Subscribe to {NAME} — billed by Magnate, one account for every Innotel service.">
  <meta property="og:title" content="Subscribe to {NAME}">
  <meta property="og:description" content="{TAGLINE}. One account, every service.">
  <meta property="og:type" content="website">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='{ACCENT}'/%3E%3C/svg%3E">
  <style>
    :root {{
      color-scheme: dark;
      --accent: {ACCENT};
      --accent-tint: color-mix(in srgb, {ACCENT} 12%, transparent);
      --bg: #0b0d10;
      --surface: #16191f;
      --border: #262c35;
      --text: #f4f7fb;
      --muted: #a8b2bf;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background: var(--bg); color: var(--text);
      font-family: ui-sans-serif, system-ui, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
      line-height: 1.6; -webkit-font-smoothing: antialiased;
      background-image: radial-gradient(900px 480px at 50% -10%, var(--accent-tint), transparent 70%);
      background-repeat: no-repeat;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 0 24px; }}
    header {{ border-bottom: 1px solid var(--border); }}
    .bar {{ display: flex; align-items: center; justify-content: space-between; height: 64px; }}
    .brand {{ display: flex; align-items: center; gap: 10px; font-weight: 650; }}
    .mark {{ width: 28px; height: 28px; border-radius: 8px; background: var(--accent); }}
    .signin {{ color: var(--muted); font-size: 14px; }}
    .signin:hover {{ color: var(--text); }}
    .hero {{ text-align: center; padding: 72px 0 48px; }}
    .kicker {{
      display: inline-flex; align-items: center; gap: 8px;
      font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
      color: var(--accent);
      border: 1px solid color-mix(in srgb, {ACCENT} 35%, transparent);
      background: var(--accent-tint);
      border-radius: 999px; padding: 6px 14px;
    }}
    h1 {{ font-size: clamp(32px, 6vw, 52px); line-height: 1.05; margin: 20px 0 12px; letter-spacing: -0.02em; }}
    h1 .accent {{ color: var(--accent); }}
    .lede {{ color: var(--muted); max-width: 560px; margin: 0 auto; font-size: 17px; }}
    .plans {{ display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); padding: 24px 0 12px; }}
    .plan {{
      background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
      padding: 24px; display: flex; flex-direction: column; gap: 12px;
    }}
    .plan.popular {{ border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); position: relative; }}
    .plan .tag {{
      position: absolute; top: -11px; left: 50%; transform: translateX(-50%);
      background: var(--accent); color: #0b0d10; font-size: 11px; font-weight: 700;
      padding: 2px 12px; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.06em;
    }}
    .plan h2 {{ margin: 0; font-size: 18px; }}
    .price {{ font-size: 34px; font-weight: 700; }}
    .price small {{ font-size: 14px; color: var(--muted); font-weight: 400; }}
    .plan ul {{ list-style: none; margin: 0 0 4px; padding: 0; flex: 1; }}
    .plan li {{ font-size: 14px; color: var(--muted); padding: 3px 0; }}
    .plan li::before {{ content: "\\2713\\00a0\\00a0"; color: var(--accent); }}
    .cta {{
      display: block; text-align: center; border-radius: 10px; padding: 10px 16px;
      background: var(--accent); color: #0b0d10; font-weight: 600; font-size: 14px;
    }}
    .cta:hover {{ filter: brightness(1.1); color: #0b0d10; }}
    .empty {{
      text-align: center; color: var(--muted); background: var(--surface);
      border: 1px dashed var(--border); border-radius: 16px; padding: 36px 24px; margin: 24px 0;
    }}
    .fineprint {{
      display: flex; gap: 12px; align-items: flex-start; max-width: 720px; margin: 28px auto 0;
      background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 16px 20px;
      font-size: 13px; color: var(--muted);
    }}
    .fineprint b {{ color: var(--text); }}
    /* Service directory tiles (apex page only — a service page has none). */
    .tile {{
      display: block; background: var(--surface); border: 1px solid var(--border);
      border-radius: 16px; padding: 20px; transition: border-color .2s, transform .2s;
    }}
    .tile:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
    .tile .mark {{ display: block; width: 28px; height: 4px; border-radius: 999px; background: var(--accent); margin-bottom: 14px; }}
    .tile b {{ display: block; font-size: 17px; color: var(--text); }}
    .tile small {{ display: block; color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .tile .from {{ display: inline-block; margin-top: 12px; font-style: normal; font-weight: 600; font-size: 13px; color: var(--accent); }}
    footer {{ border-top: 1px solid var(--border); margin-top: 56px; }}
    .foot {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; padding: 24px 0; font-size: 13px; color: var(--muted); }}
    .foot a {{ color: var(--muted); }}
    .foot a:hover {{ color: var(--text); }}
    .all-services {{ font-size: 12px; }}
  </style>
</head>
<body>
  <header>
    <div class="wrap bar">
      <span class="brand"><span class="mark"></span>{NAME}</span>
      <a class="signin" href="{PORTAL_URL}">All Innotel plans</a>
    </div>
  </header>

  <main class="wrap">
    <section class="hero">
      <span class="kicker">Powered by Innotel</span>
      <h1>Subscribe to {NAME}<span class="accent">.</span></h1>
      <p class="lede">{TAGLINE}. One account, one bill — every Innotel service is billed through Magnate.</p>
    </section>

    <section>
      <div class="plans" id="plans" hidden></div>
      <div class="empty" id="empty" hidden>
        <b>{NAME}</b> is not sold separately yet.<br>
        It is included with the platform membership — <a href="{PORTAL_URL}">see all plans</a>.
      </div>

      <div class="fineprint">
        <span aria-hidden="true">&#128179;</span>
        <span>
          <b>One bill for everything.</b> Subscriptions are billed by Magnate, the platform's
          billing service — never by {NAME} itself. Prices are read live from the master
          dashboard; manage or cancel any plan from your Magnate account.
        </span>
      </div>
    </section>
  </main>

  <footer>
    <div class="wrap foot">
      <span>{NAME} — Powered by Innotel</span>
      <span class="all-services"><a href="{PORTAL_URL}">subscribe.innotel.us</a> · every service, one account</span>
    </div>
  </footer>

  <script>
    // Prices are never baked in: read them live from Magnate, the billing
    // platform, so the master dashboard is the only place a price is set.
    (function () {{
      var SERVICE = "{SERVICE}";
      var PORTAL_SIGNUP = "{PORTAL_URL}/signup";
      var plansEl = document.getElementById("plans");
      var emptyEl = document.getElementById("empty");
      function money(cents) {{
        var v = cents / 100;
        return v % 1 === 0 ? String(v) : v.toFixed(2);
      }}
      function card(p) {{
        var el = document.createElement("div");
        el.className = "plan" + (p.highlighted ? " popular" : "");
        var feats = (p.features || []).map(function (f) {{
          return "<li>" + f.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</li>";
        }}).join("");
        el.innerHTML =
          (p.highlighted ? '<span class="tag">Most popular</span>' : "") +
          "<h2>" + p.name.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</h2>" +
          '<div class="price">$' + money(p.priceMonthlyCents) + "<small>/month</small></div>" +
          (p.priceYearlyCents ? '<div style="font-size:12px;color:var(--muted)">or $' + money(p.priceYearlyCents) + "/year</div>" : "") +
          "<ul>" + feats + "</ul>" +
          '<a class="cta" href="' + PORTAL_SIGNUP + "?plan=" + encodeURIComponent(p.slug) + '">Subscribe</a>';
        return el;
      }}
      fetch("{MAGNATE_URL}/api/plans?service=" + encodeURIComponent(SERVICE))
        .then(function (r) {{ return r.ok ? r.json() : {{ plans: [] }}; }})
        .then(function (d) {{
          var plans = (d && d.plans) || [];
          if (!plans.length) {{ emptyEl.hidden = false; return; }}
          plans.forEach(function (p) {{ plansEl.appendChild(card(p)); }});
          plansEl.hidden = false;
        }})
        .catch(function () {{
          emptyEl.hidden = false;
          emptyEl.innerHTML = 'Prices are temporarily unavailable — <a href="{PORTAL_URL}">open the billing portal</a>.';
        }});
    }})();
  </script>
</body>
</html>
"""

INDEX_NOTE = """
  <!-- apex page: directory of every service's subscribe page. Each tile shows
       that service's own price, fetched live from Magnate — the master
       dashboard is the only place a price is set. -->
"""

# Apex page script: annotate every tile with its service's own entry price.
# Deliberately NOT the single-service plan script below — the apex sells
# nothing itself (no shared catalog); it only routes to each service's page.
APEX_SCRIPT = """  <script>
    // Prices are never baked in: each tile asks Magnate for that one service's
    // plan list. A service with no plan yet keeps its tagline only.
    (function () {
      var MAGNATE = "{MAGNATE_URL}";
      var tiles = Array.prototype.slice.call(
        document.querySelectorAll(".tile[data-service]")
      );
      function money(cents) {
        var v = cents / 100;
        return v % 1 === 0 ? String(v) : v.toFixed(2);
      }
      tiles.forEach(function (tile) {
        var svc = tile.getAttribute("data-service");
        fetch(MAGNATE + "/api/plans?service=" + encodeURIComponent(svc))
          .then(function (r) { return r.ok ? r.json() : { plans: [] }; })
          .then(function (d) {
            var plans = (d && d.plans) || [];
            if (!plans.length) return;
            var cheapest = plans.reduce(function (min, p) {
              return min === null || p.priceMonthlyCents < min
                ? p.priceMonthlyCents
                : min;
            }, null);
            if (cheapest === null) return;
            var price = document.createElement("em");
            price.className = "from";
            price.textContent = "from $" + money(cheapest) + "/mo";
            tile.appendChild(price);
          })
          .catch(function () {});
      });
    })();
  </script>"""


def load_spec(service: str) -> dict:
    repo = REPOS / REPO_OF.get(service, service)
    spec_file = repo / "web" / "landing" / "subscribe.json"
    spec = dict(DEFAULTS.get(service, {"name": service.title(), "tagline": "", "accent": "#38bdf8"}))
    if spec_file.is_file():
        try:
            data = json.loads(spec_file.read_text())
            for key in ("name", "tagline", "accent"):
                if data.get(key):
                    spec[key] = data[key]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARN {spec_file}: {exc} — using defaults", file=sys.stderr)
    return spec


def render(service: str) -> str:
    spec = load_spec(service)
    return TEMPLATE.format(
        NAME=spec["name"],
        TAGLINE=spec["tagline"],
        ACCENT=spec["accent"],
        SERVICE=service,
        MAGNATE_URL="https://app.magnate.innotel.us",
        PORTAL_URL="https://subscribe.innotel.us",
    )


def render_index() -> str:
    # Every subscribable service gets a tile. Magnate is left out on purpose:
    # it IS the billing platform, so it is not something you subscribe to —
    # its own page is the account/billing portal.
    tiles = "\n".join(
        f'    <a class="tile" data-service="{s}" style="--accent: {load_spec(s)["accent"]}" '
        f'href="https://subscribe.{s}.innotel.us">'
        f'<span class="mark"></span><b>{load_spec(s)["name"]}</b>'
        f'<small>{load_spec(s)["tagline"]}</small></a>'
        for s in SERVICES if s != "magnate"
    )
    page = TEMPLATE.format(
        NAME="Innotel",
        TAGLINE="Every service, one account",
        ACCENT="#38bdf8",
        SERVICE="generic",
        MAGNATE_URL="https://app.magnate.innotel.us",
        PORTAL_URL="https://subscribe.innotel.us",
    )
    # The directory replaces the single-service plan grid …
    page = page.replace(
        '<div class="plans" id="plans" hidden></div>',
        f'<div class="plans">\n{tiles}\n  </div>',
    )
    # … and the "not sold separately" notice is unused (the tiles always show).
    page = re.sub(r'\n *<div class="empty" id="empty" hidden>.*?</div>\n', "\n", page,
                  flags=re.S)
    # … served by the directory script, not the one-service plan script.
    start, end = page.index("  <script>"), page.index("  </script>")
    # plain replace, not .format(): the script's JS object literals contain braces
    script = APEX_SCRIPT.replace("{MAGNATE_URL}", "https://app.magnate.innotel.us")
    return INDEX_NOTE + page[:start] + script + page[end + len("  </script>"):]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify pages are current; exit 1 if not")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []

    for service in SERVICES:
        content = render(service)
        out = OUT_DIR / f"{service}.html"
        if args.check:
            if not out.is_file() or out.read_text() != content:
                drift.append(out.name)
        else:
            out.write_text(content)
            print(f"  wrote {out.relative_to(STACK)}")
            if out.read_text() != content:
                drift.append(out.name)

    index_content = render_index()
    index_out = OUT_DIR / "index.html"
    if args.check:
        if not index_out.is_file() or index_out.read_text() != index_content:
            drift.append("index.html")
    else:
        index_out.write_text(index_content)
        print(f"  wrote {index_out.relative_to(STACK)}")

    if args.check:
        if drift:
            print(f"FAIL {len(drift)} page(s) stale or missing: {', '.join(drift)}", file=sys.stderr)
            return 1
        print(f"PASS all {len(SERVICES) + 1} subscribe pages are current")
        return 0
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
