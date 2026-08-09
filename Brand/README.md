# Wren — brand and social pack

Warm, domestic, local-first. Every built page is one self-contained file: no
build step needed to *use* one, no webfonts, no network requests. Open any
`.html` in a browser.

## What's here

| File | What it is |
|---|---|
| `brand-system.html` | The system itself — marks, reduction ladder, clear space, palette with live WCAG contrast checks, type scale, do/don't. **Read this first.** |
| `social-pack.html` | Eleven cards at exact platform sizes, each with SVG and PNG export. |
| `brand/*.svg` | Mark, wordmark, lockups, avatar and favicon as standalone vector files. |
| `github/*.png` | Social preview, README hero, and the architecture figure. |
| `social/*.png` | Avatars and banners for X, Bluesky, LinkedIn, Discord, Instagram. |
| `favicon/*` | The favicon ladder, `site.webmanifest`, and a `<head>` snippet to paste. |
| `templates/`, `src/`, `build.py` | The sources. `python3 build.py` regenerates both pages. |
| `src/logo.py` | Generates the bird and the wordmark. The mark lives here. |
| `src/gen_arch.py` | Measures `../wren/` and writes `src/arch.json`. The architecture figure starts here. |
| `render_pngs.py` | Re-renders every PNG from `social-pack.html`, headlessly and size-checked. |

## Rebuilding

```sh
cd Brand
python3 src/logo.py > src/logo.json     # the marks (also writes brand/*.svg)
python3 build.py                        # templates/ + src/ -> the two pages
python3 render_pngs.py                  # the pages -> github/, social/, favicon/
```

`build.py` runs the first and second steps for you, so in practice it is
`python3 build.py && python3 render_pngs.py`.

That is the whole chain, and every step of it is in the repo: the bird is
constructed from named parameters, the plugin graph measures itself, the pages
inline both, and the PNGs are pictures of the pages. Nothing in `github/` or
`social/` is drawn by hand.

`build.py` inlines `src/tokens.css`, `src/logo.json`, `src/arch.json` and
`src/contrast.js` into `templates/{brand,social}.html`, replacing the
`/*__TOKENS__*/`, `/*__LOGO__*/`, `/*__ARCH__*/` and `/*__CONTRAST__*/`
markers. If a generator fails it keeps the last good JSON and says so rather
than failing the build — the geometry and the plugin graph only change when the
project does.

## The mark

A wren: compact body, fine beak, and the short tail cocked up over the back.
The tail is the whole identity — it is the one feature that separates this from
a generic songbird — so it survives every reduction.

`src/logo.py` builds the silhouette as a closed cubic-Bezier spline through
eighteen named anchors (beak tip, forehead, crown, nape, back, the four tail
corners, vent, belly, breast, throat). Each anchor is one line, each is
adjustable on its own, and the framing normalises itself from the curve's exact
Bezier extrema — so moving an anchor cannot silently decentre the mark or clip
it in any downstream asset.

**Only `src/logo.py` needs `fonttools`,** and only for the wordmark, which is
converted to outlines from TeX Gyre Adventor so nothing downstream carries a
webfont dependency. The bird itself is pure Python with no dependencies at all.
If the font is missing, the build keeps whatever outlines `src/logo.json`
already holds.

> An earlier version built the bird by unioning superellipses and filleting the
> joins with `shapely`. It is worth knowing why that is gone: gluing convex
> primitives together gives you a blob with lumps, not a bird — the wing read as
> a shark fin and the tail as a mitten. Explicit anchors cost more to author and
> are worth it. The dependency went away as a bonus.

### Reduction

The full mark carries a wing, an eye and the pale supercilium. Below roughly
24 px those stop resolving and start reading as dirt, so anything smaller uses
the **simple mark** — one colour, silhouette only. `brand/favicon.svg` already
does. Minimum size for the full mark is 20 px.

Clear space is **0.35 × the mark's height** on every side.

## Colour

Clay `#C4694A` is the brand. It is a *graphic* colour, not a text colour: at
3.30:1 on paper it is right for the mark, for display type at 24 px and up, and
for fills and borders — and wrong for body copy. Use `--w-clay-ink` `#A04E33`
when clay has to carry small text, or sit under cream (4.97:1).

Every ratio quoted in `src/tokens.css` is computed by `src/contrast.py`, and
`brand-system.html` recomputes all of them in the browser on load. That is not
decoration. The first draft of that page claimed unlifted clay on night was
2.6:1 from memory; the live table said 4.25:1, and the prose now quotes the
computed value instead of a remembered one.

## Where the architecture figure comes from

`github/architecture-1280x720.png` is captioned **MEASURED, NOT DRAWN**, and it
is. `src/gen_arch.py` parses `../wren/registry.py` and `../wren/communication/`
with `ast` — deliberately *not* importing them, since importing `wren.config`
wants a populated `.env` and a reachable LLM provider, which a brand build has
no business needing — and writes what it finds to `src/arch.json`.

Measured at the last build:

```text
2 chat channels (discord, http) · 2 input watchers (gmail, github) · 1 stub (telegram)
6 skills · 22 intents
```

Add a skill to `registry.py`, run `python3 build.py && python3 render_pngs.py`,
and the figure grows a box on its own. **Regenerate after any change to the
plugin list**, or the figure quietly starts lying — which is the exact failure
the caption is there to rule out.

## Rendering notes

`render_pngs.py` rasterises on a canvas *inside* the page — the same path the
page's own "PNG @1×" buttons use — and reads the finished image back as a data
URL. That is deliberate, and worth not undoing:

> The obvious approach, pointing the browser's `--screenshot` at the page with
> `--window-size` set to the card, is broken on any current Chrome or Edge.
> `--headless=new` emulates a real browser window, so the viewport comes out
> ~87 px shorter than asked for and every card renders at its correct width
> with a band of page background along the bottom edge. Old headless, which did
> not do this, no longer exists. A canvas has none of that ambiguity — it is
> exactly as many pixels as you ask for.

The script exits non-zero if any output comes out the wrong size. A silently
mis-sized asset is worse than a missing one: it gets committed and only shows
up later as a blurry banner.

**Render the whole set, not one card.** Glyph rasterisation differs between
browser engines and font sets, so a single re-rendered card will not quite
match its siblings. One launch renders them all, so this is the default.

## Before you publish

1. **GitHub social preview** — `github/social-preview-1280x640.png` → repo
   Settings → General → Social preview.
2. **README hero** — `github/hero-1280x400.png` at the top of the repository
   README, replacing the current `user-attachments` link. Committing the image
   means it survives the account that uploaded it.
3. **Architecture figure** — `github/architecture-1280x720.png` belongs in the
   README's Architecture section, next to the prose describing the
   Communication/Skills split.
4. **Favicons** — copy `favicon/` to your web root and paste
   `favicon/head-snippet.html` into `<head>`. For Wren's own browser chat that
   means serving it alongside `wren/communication/chat.html`.
5. **Avatars** — `social/avatar-512x512.png` for light contexts,
   `social/avatar-clay-512x512.png` where it needs to pop,
   `social/discord-icon-512x512.png` for Discord (it crops to a circle; the
   mark is already inset for that).
6. **Banner safe areas** — on X the avatar covers the lower-left of
   `x-banner-1500x500`; the lockup is placed clear of it. Check LinkedIn's
   current crop before using `linkedin-banner-1128x191`, it changes.

## Copy

The lines used across the pack, so they stay consistent:

- *A private, self-hosted assistant for your household.*
- *Talk to it in plain English. It figures out the rest.*
- *One assistant, every channel.*
- *local-first · plugin channels · drop-in skills*
- *runs on your hardware · your data stays home*

Wren is a household assistant, so the voice is plain and domestic — short
sentences, no enterprise register, no exclamation marks. It is private and
self-hosted, and that is a statement of fact about where it runs, not a
security guarantee: the README is explicit that the HTTP surface has no TLS and
a bearer token is the only thing guarding it. Don't let the marketing copy
outrun that.

Licence: same as the project (MIT). The bird is original geometry generated by
`src/logo.py`; no third-party marks or artwork are used anywhere in this pack.
The wordmark is set in TeX Gyre Adventor (GUST Font License, an OFL-compatible
licence permitting embedding and outline conversion).
