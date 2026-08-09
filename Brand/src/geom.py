#!/usr/bin/env python3
"""Curve helpers for the Wren marks. Pure Python — no dependencies.

The silhouette is a closed cubic-Bezier spline through named anchors. Anchors
are either smooth (the curve keeps a continuous tangent through them) or
corners (the tangent breaks, for the beak tip and the two tail-tip corners).

An earlier version of this file unioned superellipses and filleted the joins
with shapely. It produced a mush: convex primitives glued together give you a
blob with lumps, not a bird. Explicit anchors cost more to author and are worth
it — the crown, the nape dip, the rump and the belly are each one number to
adjust, and the shape stays exactly as drawn at every size.
"""


# ---------------------------------------------------------------- anchors
def A(x, y, corner=False, t=1.0, tin=None, tout=None):
    """One anchor. `t` scales both tangents (fullness of the curve through it);
    `tin`/`tout` override each side independently where a curve needs to leave
    an anchor harder than it arrives."""
    return {"p": (float(x), float(y)), "corner": bool(corner),
            "t": float(t),
            "tin": float(tin) if tin is not None else None,
            "tout": float(tout) if tout is not None else None}


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _mul(a, k):
    return (a[0] * k, a[1] * k)


def _tangents(anchors, closed=True):
    """Per-anchor incoming and outgoing tangent vectors."""
    n = len(anchors)
    out = []
    for i, a in enumerate(anchors):
        p = a["p"]
        pv = anchors[(i - 1) % n]["p"] if closed or i > 0 else p
        nx = anchors[(i + 1) % n]["p"] if closed or i < n - 1 else p
        if a["corner"]:
            tin = _sub(p, pv)
            tout = _sub(nx, p)
        else:
            d = _mul(_sub(nx, pv), 0.5)
            tin = tout = d
        ki = a["tin"] if a["tin"] is not None else a["t"]
        ko = a["tout"] if a["tout"] is not None else a["t"]
        out.append((_mul(tin, ki), _mul(tout, ko)))
    return out


def spline_path(anchors, closed=True, prec=2):
    """Closed cubic-Bezier `d` through `anchors`."""
    n = len(anchors)
    T = _tangents(anchors, closed)
    f = "%%.%df" % prec

    def s(v):
        return f % v

    p0 = anchors[0]["p"]
    parts = ["M %s %s" % (s(p0[0]), s(p0[1]))]
    last = n if closed else n - 1
    for i in range(last):
        a = anchors[i]
        b = anchors[(i + 1) % n]
        c1 = _add(a["p"], _mul(T[i][1], 1.0 / 3.0))
        c2 = _sub(b["p"], _mul(T[(i + 1) % n][0], 1.0 / 3.0))
        parts.append("C %s %s %s %s %s %s" % (
            s(c1[0]), s(c1[1]), s(c2[0]), s(c2[1]), s(b["p"][0]), s(b["p"][1])))
    if closed:
        parts.append("Z")
    return " ".join(parts)


# ---------------------------------------------------------------- bounds
def _bezier_bounds(p0, c1, c2, p3):
    """Exact extrema of one cubic, per axis — the control polygon overestimates
    and the mark's framing is computed from these."""
    lo = [min(p0[i], p3[i]) for i in (0, 1)]
    hi = [max(p0[i], p3[i]) for i in (0, 1)]
    for i in (0, 1):
        a = -p0[i] + 3 * c1[i] - 3 * c2[i] + p3[i]
        b = 2 * (p0[i] - 2 * c1[i] + c2[i])
        c = -p0[i] + c1[i]
        ts = []
        if abs(a) < 1e-12:
            if abs(b) > 1e-12:
                ts.append(-c / b)
        else:
            disc = b * b - 4 * a * c
            if disc >= 0:
                r = disc ** 0.5
                ts += [(-b + r) / (2 * a), (-b - r) / (2 * a)]
        for t in ts:
            if 0.0 < t < 1.0:
                u = 1 - t
                v = (u ** 3 * p0[i] + 3 * u * u * t * c1[i]
                     + 3 * u * t * t * c2[i] + t ** 3 * p3[i])
                lo[i] = min(lo[i], v)
                hi[i] = max(hi[i], v)
    return lo, hi


def bounds(anchors, closed=True):
    n = len(anchors)
    T = _tangents(anchors, closed)
    lo = [1e9, 1e9]
    hi = [-1e9, -1e9]
    last = n if closed else n - 1
    for i in range(last):
        a = anchors[i]
        b = anchors[(i + 1) % n]
        c1 = _add(a["p"], _mul(T[i][1], 1.0 / 3.0))
        c2 = _sub(b["p"], _mul(T[(i + 1) % n][0], 1.0 / 3.0))
        l, h = _bezier_bounds(a["p"], c1, c2, b["p"])
        lo = [min(lo[i2], l[i2]) for i2 in (0, 1)]
        hi = [max(hi[i2], h[i2]) for i2 in (0, 1)]
    return lo[0], lo[1], hi[0], hi[1]


# ---------------------------------------------------------------- transform
def transform(anchors, k, tx, ty):
    out = []
    for a in anchors:
        b = dict(a)
        b["p"] = (a["p"][0] * k + tx, a["p"][1] * k + ty)
        out.append(b)
    return out


def fit(groups, key_index, margin=4.0, box=100.0):
    """Scale/translate every group so `groups[key_index]` fits `box` with
    `margin` clear, centred. Returns (groups, k, tx, ty) so callers can map
    non-anchor items (the eye) through the same transform."""
    x0, y0, x1, y1 = bounds(groups[key_index])
    span = max(x1 - x0, y1 - y0)
    k = (box - margin * 2.0) / span
    tx = margin + ((box - margin * 2.0) - (x1 - x0) * k) / 2.0 - x0 * k
    ty = margin + ((box - margin * 2.0) - (y1 - y0) * k) / 2.0 - y0 * k
    return [transform(g, k, tx, ty) for g in groups], k, tx, ty
