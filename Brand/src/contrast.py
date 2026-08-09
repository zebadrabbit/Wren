#!/usr/bin/env python3
"""WCAG 2.1 relative luminance and contrast. Used to fill in — and keep
honest — the ratios quoted in tokens.css and shown live on the brand page."""

def _srgb(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

def lum(hexstr):
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)

def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)

if __name__ == "__main__":
    PAPER, NIGHT = "#F4EDE2", "#241F1A"
    fg = {
        "ink       ": "#3A322B", "ink-2     ": "#6E6255", "ink-3     ": "#857767",
        "clay      ": "#C4694A", "clay-deep ": "#A9553A",
        "moss      ": "#6B7F5C", "moss-deep ": "#55684A",
        "ok        ": "#4F7343", "warn      ": "#9A6712", "alert     ": "#B4462F",
    }
    night_fg = {
        "cream     ": "#F4EDE2", "cream-2   ": "#C4B7A5", "cream-3   ": "#9C8E7C",
        "clay-lift ": "#E08D6C", "moss-lift ": "#9CB388",
    }
    print("against paper %s:" % PAPER)
    for k, v in fg.items():
        r = ratio(v, PAPER)
        print("  %s %s  %5.2f:1   %s" % (k, v, r, "AA" if r >= 4.5 else ("AA-large" if r >= 3 else "FAIL")))
    print("against night %s:" % NIGHT)
    for k, v in night_fg.items():
        r = ratio(v, NIGHT)
        print("  %s %s  %5.2f:1   %s" % (k, v, r, "AA" if r >= 4.5 else ("AA-large" if r >= 3 else "FAIL")))
    print("cream on clay #C4694A: %.2f:1" % ratio("#F4EDE2", "#C4694A"))
    print("ink   on clay #C4694A: %.2f:1" % ratio("#3A322B", "#C4694A"))
    print("cream on moss #6B7F5C: %.2f:1" % ratio("#F4EDE2", "#6B7F5C"))
