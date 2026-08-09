#!/usr/bin/env python3
"""Wren brand marks — constructed geometry, not a traced drawing.

The bird is a closed cubic-Bezier spline through eighteen named anchors: beak
tip, forehead, crown, nape, back, the four tail corners, vent, belly, breast,
throat. Each is one line below and each is adjustable on its own. The wing is a
second, smaller spline; the eye and the supercilium (the pale stripe over the
eye that is half of what makes a wren look like a wren) are derived from the
head anchors.

The wordmark is converted to outlines from the font, so nothing downstream
carries a webfont dependency and the letterforms are identical on a machine
that has never heard of TeX Gyre.

    python3 logo.py            # writes ../brand/*.svg, prints logo.json to stdout

Dependencies: fonttools, for the wordmark only. The bird is pure Python.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geom as G  # noqa: E402
from geom import A  # noqa: E402

# ---------------------------------------------------------------- palette
CLAY = "#C4694A"
CLAY_DEEP = "#A9553A"
MOSS = "#6B7F5C"
CREAM = "#F4EDE2"
BARK = "#3A322B"

MARGIN = 4.0

# ---------------------------------------------------------------- the bird
# Clockwise from the beak tip, y-down, facing left. Corners break the tangent;
# everywhere else the curve runs smooth. Tension `t` is how full the curve
# bulges through an anchor — the belly wants more than the back.
OUTLINE = [
    A(13.0, 45.2, corner=True, t=0.55),         # beak tip
    A(26.0, 39.8, corner=True, t=0.70),         # upper mandible meets the face
    A(30.5, 30.6, t=0.80),                      # forehead
    A(42.0, 19.5, t=1.05),                      # crown
    A(57.0, 26.5, t=0.95),                      # nape — the dip behind the head
    A(69.0, 32.5, t=0.85),                      # back
    A(76.5, 36.0, t=0.45),                      # rump, where the tail springs
    A(85.0, 10.0, corner=True, t=0.42),         # tail tip, leading corner
    A(95.5, 16.5, corner=True, t=0.42),         # tail tip, trailing corner
    A(85.5, 45.0, t=0.50),                      # tail root, underside
    A(81.0, 60.0, t=0.85),                      # vent
    A(71.0, 74.0, t=1.00),                      # belly, rear
    A(52.0, 81.5, t=1.10),                      # belly
    A(33.0, 75.0, t=1.05),                      # breast, lower
    A(24.5, 61.0, t=0.95),                      # breast
    A(23.0, 52.0, t=0.70),                      # throat
    A(26.0, 47.2, corner=True, t=0.70),         # lower mandible meets the face
]

# A leaf with one point, laid along the body's own axis. The point aims at the
# tail root, which is where a folded primary actually ends up.
WING = [
    A(40.0, 56.5, t=0.95),
    A(54.0, 50.0, t=1.00),
    A(76.5, 57.5, corner=True, t=0.55),
    A(55.0, 65.5, t=1.00),
]

# The supercilium. Thin, and it must stop short of the outline: run it to the
# edge and it reads as a chip out of the head rather than a marking.
BROW = [
    A(30.4, 35.2, corner=True, t=0.5),
    A(42.0, 29.0, t=0.85),
    A(42.7, 31.4, t=0.85),
    A(31.3, 37.5, corner=True, t=0.5),
]

EYE = (33.4, 40.4, 2.7)


def geometry():
    """Path data for the mark, normalised into a 100-box. Framing is computed
    from the silhouette's exact Bezier extrema, so moving any anchor above
    cannot silently decentre the mark or clip it."""
    (out, wing, brow), k, tx, ty = G.fit([OUTLINE, WING, BROW], 0, margin=MARGIN)
    return {
        "body": G.spline_path(out),
        "wing": G.spline_path(wing),
        "brow": G.spline_path(brow),
        "eye": {"cx": round(EYE[0] * k + tx, 2),
                "cy": round(EYE[1] * k + ty, 2),
                "r": round(EYE[2] * k, 2)},
    }


# ---------------------------------------------------------------- wordmark
ADVENTOR_R = "/usr/share/texmf/fonts/opentype/public/tex-gyre/texgyreadventor-regular.otf"
ADVENTOR_B = "/usr/share/texmf/fonts/opentype/public/tex-gyre/texgyreadventor-bold.otf"

_cache = {}


def _font(path):
    from fontTools.ttLib import TTFont
    if path not in _cache:
        f = TTFont(path)
        _cache[path] = (f, f.getGlyphSet(), f["head"].unitsPerEm, f.getBestCmap())
    return _cache[path]


def text_path(s, font_path, size, x=0.0, tracking=0.0):
    """Outline `s` as SVG path data, baseline at y=0, y-down. -> (d, width)."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    font, gs, upem, cmap = _font(font_path)
    scale = size / upem
    out, cursor = [], x
    for ch in s:
        name = cmap.get(ord(ch))
        if name is None:
            cursor += size * 0.5 + tracking
            continue
        spen = SVGPathPen(gs, ntos=lambda v: f"{v:.2f}")
        gs[name].draw(TransformPen(spen, (scale, 0, 0, -scale, cursor, 0)))
        d = spen.getCommands()
        if d:
            out.append(d)
        cursor += gs[name].width * scale + tracking
    return " ".join(out), (cursor - x - tracking if s else 0.0)


def wordmark(size=64.0, tracking_ratio=0.055, text="wren", bold=False):
    """Set lowercase. 'wren' in caps reads as an acronym and loses the bird;
    lowercase keeps it a small domestic word, which is the whole idea."""
    tr = size * tracking_ratio
    d, w = text_path(text, ADVENTOR_B if bold else ADVENTOR_R, size, 0.0, tr)
    return {"d": d, "width": round(w, 2), "cap": size * 0.73, "xh": size * 0.48}


# ---------------------------------------------------------------- SVG
def _open(w, h, label):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.1f} {h:.1f}" '
            f'width="{w:.0f}" height="{h:.0f}" role="img" aria-label="{label}">')


def mark_group(size=100.0, fg=CLAY, wing_fill=CLAY_DEEP, detail=CREAM,
               simple=False, tx=0.0, ty=0.0):
    """The bird as a <g>. `simple=True` is the one-colour form: no wing, no
    brow, no eye. Below about 24 px the details stop resolving and start
    reading as dirt, so the favicon uses this."""
    g = geometry()
    k = size / 100.0
    t = (f' transform="translate({tx:.2f},{ty:.2f}) scale({k:.4f})"'
         if (k != 1.0 or tx or ty) else "")
    p = [f"<g{t}>", f'<path d="{g["body"]}" fill="{fg}"/>']
    if not simple:
        p.append(f'<path d="{g["wing"]}" fill="{wing_fill}"/>')
        p.append(f'<path d="{g["brow"]}" fill="{detail}"/>')
        e = g["eye"]
        p.append(f'<circle cx="{e["cx"]}" cy="{e["cy"]}" r="{e["r"]}" fill="{detail}"/>')
    p.append("</g>")
    return "\n".join(p)


def svg_mark(size=128.0, fg=CLAY, bg=None, radius=0.0, simple=False, pad=0.06):
    inner = size * (1.0 - pad * 2.0)
    off = size * pad
    p = [_open(size, size, "Wren")]
    if bg:
        p.append(f'<rect width="{size:.0f}" height="{size:.0f}" rx="{radius:.1f}" fill="{bg}"/>')
    p.append(mark_group(inner, fg=fg, simple=simple, tx=off, ty=off))
    p.append("</svg>")
    return "\n".join(p)


def svg_wordmark(size=64.0, fg=BARK, bg=None, text="wren", bold=False):
    wm = wordmark(size, text=text, bold=bold)
    pad = size * 0.30
    w = wm["width"] + pad * 2
    h = size * 1.00 + pad * 2
    p = [_open(w, h, "Wren")]
    if bg:
        p.append(f'<rect width="{w:.1f}" height="{h:.1f}" fill="{bg}"/>')
    p.append(f'<g transform="translate({pad:.2f},{pad + size * 0.73:.2f})">'
             f'<path d="{wm["d"]}" fill="{fg}"/></g>')
    p.append("</svg>")
    return "\n".join(p)


def svg_lockup(size=64.0, fg=BARK, mark_fg=CLAY, bg=None, stacked=False, simple=False):
    """Mark + wordmark. The mark's optical centre sits a little above its box
    centre (the tail is tall and light), so the wordmark baseline is aligned to
    the body rather than to the box."""
    wm = wordmark(size)
    ms = size * 1.75
    pad = size * 0.32
    if not stacked:
        gap = size * 0.34
        w = pad * 2 + ms + gap + wm["width"]
        h = pad * 2 + ms
        p = [_open(w, h, "Wren")]
        if bg:
            p.append(f'<rect width="{w:.1f}" height="{h:.1f}" fill="{bg}"/>')
        p.append(mark_group(ms, fg=mark_fg, simple=simple, tx=pad, ty=pad))
        p.append(f'<g transform="translate({pad + ms + gap:.2f},'
                 f'{pad + ms * 0.62 + size * 0.24:.2f})">'
                 f'<path d="{wm["d"]}" fill="{fg}"/></g>')
    else:
        gap = size * 0.26
        w = max(ms, wm["width"]) + pad * 2
        h = pad * 2 + ms + gap + size * 0.75
        p = [_open(w, h, "Wren")]
        if bg:
            p.append(f'<rect width="{w:.1f}" height="{h:.1f}" fill="{bg}"/>')
        p.append(mark_group(ms, fg=mark_fg, simple=simple, tx=(w - ms) / 2.0, ty=pad))
        p.append(f'<g transform="translate({(w - wm["width"]) / 2.0:.2f},'
                 f'{pad + ms + gap + size * 0.73:.2f})">'
                 f'<path d="{wm["d"]}" fill="{fg}"/></g>')
    p.append("</svg>")
    return "\n".join(p)


FILES = {
    "mark.svg": lambda: svg_mark(128),
    "mark-simple.svg": lambda: svg_mark(128, simple=True),
    "mark-cream.svg": lambda: svg_mark(128, fg=CREAM, simple=True),
    "mark-bark.svg": lambda: svg_mark(128, fg=BARK, simple=True),
    "mark-badge.svg": lambda: svg_mark(512, bg=CREAM, radius=96),
    "avatar-512.svg": lambda: svg_mark(512, bg=CREAM, radius=0, pad=0.14),
    "avatar-bark-512.svg": lambda: svg_mark(512, fg=CREAM, bg=BARK, radius=0, pad=0.14),
    "avatar-clay-512.svg": lambda: svg_mark(512, fg=CREAM, bg=CLAY, radius=0, pad=0.14),
    "favicon.svg": lambda: svg_mark(64, fg=CREAM, bg=CLAY, radius=12, simple=True, pad=0.13),
    "wordmark.svg": lambda: svg_wordmark(64),
    "wordmark-cream.svg": lambda: svg_wordmark(64, fg=CREAM),
    "wordmark-clay.svg": lambda: svg_wordmark(64, fg=CLAY),
    "lockup.svg": lambda: svg_lockup(64),
    "lockup-cream.svg": lambda: svg_lockup(64, fg=CREAM, mark_fg=CREAM, simple=True),
    "lockup-stacked.svg": lambda: svg_lockup(64, stacked=True),
}


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "brand")
    os.makedirs(out, exist_ok=True)
    for name, fn in FILES.items():
        with open(os.path.join(out, name), "w") as f:
            f.write(fn() + "\n")
    print(json.dumps({
        "geometry": geometry(),
        "wordmark": wordmark(64),
        "palette": {"clay": CLAY, "clayDeep": CLAY_DEEP, "moss": MOSS,
                    "cream": CREAM, "bark": BARK},
    }))
