[![Theme: Unity](https://img.shields.io/badge/theme-Unity-6366f1)](https://github.com/innotelinc/innotel-platform-stack/tree/main/standards/unity)

# Unity

**The one theme.** Every Innotel project wears it — the OnTrak products, and anything
added after them. This directory is the standard: the stylesheet, the switch, and the
tools that put them on a page.

It exists because the alternative is what usually happens. Five apps each start from a
different dashboard template, and a year later they disagree about what "attention"
looks like, what a table row is, and whether dark mode is a setting or a redesign.

Unity is not a component library. It is a vocabulary of **semantic tokens** plus a
handful of shared primitives, and a switch for the two axes a person actually cares
about.

## The two axes

Unity separates *how bright* from *what kind of work this is*, because they are
different questions asked by different people at different times.

| Axis | Attribute | Values | Absent means |
| --- | --- | --- | --- |
| **Mode** | `data-mode` on `<html>` | `light` · `dark` · *(absent)* | follow the machine |
| **Scheme** | `data-scheme` on `<html>` | `desk` · `operations` · `soc` · *(absent)* | the page's own default |

**Mode** is the personal, per-browser preference. It is stored because a person who
prefers dark on the machine in front of them prefers dark there. It is deliberately not
an account setting.

**Scheme** is the professional register — the same neutral ground, tuned for a kind of
work. A page declares which one it opens in; a person may switch.

| scheme | family | who wears it |
| --- | --- | --- |
| `desk` | violet | the service desk (Tix) and the training range (ITS) |
| `operations` | graphite + blue | the operations consoles, the OnTrak Unity portal, and the landing pages |
| `soc` | navy + cyan | the security-operations console (Sentinel) |

They combine freely: six rendered states, from `light + desk` to `dark + soc`.

## The files

| file | what it is |
| --- | --- |
| `unity-theme.css` | the palette: every token, in all three schemes × both modes, then the shared primitives |
| `unity-theme.js` | the switch, framework-free. Owns `data-mode` and `data-scheme` on `<html>` |
| `unity-theme.tsx` | the switch as a React component — the same control, for apps that have React |
| `apply.py` | puts the theme on a **static landing page**: rewrites the page's own palette into Unity tokens and adds the switch |
| `readmes.py` | adds the theme badge (and the landing-page link) to a repository README |

Every repository in the estate carries a copy of all three of the first files and is
badged with this document.

## Adding it to a landing page

The estate's landing pages are hand-written HTML with a private palette in one
`:root` block. `apply.py` reads that palette, maps every colour it declares onto a
Unity token, maps every literal colour used further down the file, and inserts the
stylesheet, the blocking switch script, and the theme control.

```bash
python3 standards/unity/apply.py /path/to/repos            # report only
python3 standards/unity/apply.py /path/to/repos            # write
python3 standards/unity/apply.py /path/to/one-repo         # a single repo
```

It is **idempotent**: a page already carrying the stylesheet is reported and skipped,
so running it over the whole estate a second time changes nothing.

Two details are worth knowing because they were bugs first:

- The palette block is rebuilt, not appended to. A page that declares a name Unity
  already owns — `--surface: var(--surface)` — has written a *cycle*, not an alias,
  and a self-referencing custom property is invalid at computed-value time, which
  poisons every use of that token on the page. Those declarations are dropped;
  Unity's own already apply.
- The new `<style>` tag and the two head assets go in during **one** replacement.
  Once the block's contents have changed, the original `<style>…</style>` is no
  longer a substring of the document, so a second `replace` silently matches
  nothing — which is exactly how a page came to be missing its stylesheet while
  looking otherwise themed.

Names it does not recognise are reported rather than guessed at. `HUE_WORDS`,
`EXTRA_HEX` and `RADIUS_FILE` are the explicit tables for the cases the estate has
actually contained; adding a hex there is a reviewable one-line change, where a
nearest-colour heuristic would silently rewrite a shade somebody chose on purpose.

## Adding it to an application

Three copies and two lines. The copies are not a suggestion about tidiness: each app
is built from its own directory, so a file outside the build context cannot be
imported, and a symlink breaks the moment the image is built.

```bash
# 1. copy the three files in (paths vary by framework; these are Next.js)
cp standards/unity/unity-theme.css myapp/src/theme/unity-theme.css
cp standards/unity/unity-theme.js  myapp/public/unity-theme.js
cp standards/unity/unity-theme.tsx myapp/src/components/ThemeToggle.tsx
```

```css
/* 2. the app's own stylesheet imports the palette, and nothing visual itself */
@import "../theme/unity-theme.css";
```

```tsx
// 3. the layout sets the scheme and loads the switch *before* the first paint
<html lang="en" data-scheme="operations">
  <head>
    <script dangerouslySetInnerHTML={{ __html: 'window.UNITY_DEFAULT_SCHEME = "operations";' }} />
    <script src="/unity-theme.js" />
  </head>
```

That `<script src>` is **blocking** on purpose. A theme applied by a framework runs
after the first paint, which is a white flash on a dark screen — the one detail
everybody notices about a dark mode that was added later.

## Using the tokens

```css
.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-card);
}
.card__note { color: var(--ink-faint); }
.card--overdue { border-color: var(--bad); background: var(--bad-soft); }
```

| token group | tokens | notes |
| --- | --- | --- |
| surfaces | `--canvas` `--surface` `--surface-muted` `--surface-sunken` | `canvas` is the page, `surface` is a card on it |
| lines | `--line` `--line-strong` | dividers, and the edge that needs to be seen |
| text | `--ink` `--ink-soft` `--ink-faint` | three levels, no more. A fourth means the hierarchy is wrong |
| brand | `--brand` `--brand-soft` `--brand-ink` `--brand-ring` | `--brand-ink` is the *text on* brand, which in dark mode is dark |
| status | `--ok` `--info` `--bad` `--attention` `--unknown` | each with a `-soft` for backgrounds |
| product tones | `--tone-its` `--tone-tix` `--tone-sentinel` `--tone-sync` | a label, never the only signal — always with the name in text |
| shape | `--radius-sm` `--radius` `--radius-lg` `--radius-pill` | |
| depth | `--shadow-card` `--shadow-pop` | |
| type | `--font-sans` `--font-display` `--mono` | |
| space | `--space-1` … `--space-6` | |

Two rules that matter more than the table:

1. **A token says what a thing *is*, not what colour it is.** `--attention`, never
   `--orange`. A class that names a colour cannot be re-themed, and the day the scheme
   changes it becomes a lie.
2. **`--unknown` is its own token on purpose.** A check that never ran must never be
   painted the same as a check that passed. Most dashboards get this wrong, and it is
   the failure that makes an operator stop trusting the whole page.

The stylesheet also carries a small set of classes — `.ot-panel`, `.ot-btn`,
`.ot-field`, `.ot-pill`, `.ot-table`, `.ot-note`, `.ot-theme` — so two products cannot
disagree about what a button is. The `.ot-` prefix is a namespace from the theme's
earlier name, not a product; renaming it would churn every stylesheet in the estate for
nothing.

## The shared primitives, from plain JavaScript

```js
window.UnityTheme.mode();          // "system" | "light" | "dark"
window.UnityTheme.setMode("dark");
window.UnityTheme.scheme();        // "desk" | "operations" | "soc"
window.UnityTheme.cycle();         // light -> dark -> system -> light
window.addEventListener("unity:theme", (event) => {
  console.log(event.detail.mode, event.detail.scheme);
});
```

`window.OntrakTheme` is kept as an alias so anything already calling it keeps working.

## Badging a repository

```bash
python3 standards/unity/readmes.py /path/to/repos                  # the whole estate
python3 standards/unity/readmes.py --repo /path/to/one --slug Name # one repo, any name
```

It finds the badge block — whole lines that are a linked image, so a badge quoted
mid-sentence is not mistaken for the header — appends the theme badge, and adds a
landing-page link when the README has none. Running it twice is a no-op.

## Changing the theme

Edit the files here, then copy them over every product's copy and run that product's
guard (`make theme` in OnTrak). A change made in one app and not here is exactly the
drift the guards exist to catch.

Adding a **scheme** touches all three files: the token blocks in `unity-theme.css`
(light, explicit dark, and the `prefers-color-scheme` dark), `unity-theme.js`'s
`normaliseScheme`, and `unity-theme.tsx`'s `SCHEMES` list. `soc` was added for
Sentinel and is the worked example — a grep for it finds every place a scheme has to
be declared.

## License

AGPL-3.0-or-later, like the rest of the platform stack. See [LICENSE](../../LICENSE).
