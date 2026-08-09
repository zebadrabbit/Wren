#!/usr/bin/env python3
"""Render every card in social-pack.html to a PNG at its exact platform size.

The cards are the source of truth; these files are pictures of them. Nothing in
github/ or social/ is drawn by hand, which is the only reason the architecture
figure can honestly be captioned MEASURED, NOT DRAWN — it is a rendering of a
page that read the repository at build time.

    python3 render_pngs.py                     # everything, one browser launch
    python3 render_pngs.py gh-architecture     # one card
    python3 render_pngs.py --favicons          # just the favicon ladder

Rasterising happens on a canvas inside the page — the same path the page's own
"PNG @1x" buttons use — and the finished image comes back as a data URL via
--dump-dom. That is deliberate. The obvious approach, pointing the browser's
--screenshot at the page and sizing the window to the card, is broken on any
current Chrome or Edge: --headless=new emulates a real browser window, so the
viewport is ~87 px shorter than --window-size asks for and every card comes back
its correct width with a band of background along the bottom. Old headless,
which did not do this, no longer exists. A canvas has none of that ambiguity —
it is exactly as many pixels as you ask for.

Rendering is still engine- and machine-dependent for TEXT: glyph rasterisation
differs between browsers and installed font sets. Re-render the WHOLE set on one
machine rather than a single card, or that card will not quite match its
siblings. One launch renders them all, so this is the default.
"""
import base64
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(ROOT, "social-pack.html")
BRAND = os.path.join(ROOT, "brand")

DIRS = {"gh-": "github", "so-": "social"}          # card id prefix -> output dir
FAVICONS = [("favicon-16.png", 16), ("favicon-32.png", 32), ("favicon-48.png", 48),
            ("apple-touch-icon-180.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)]

BROWSERS = [
    # Playwright's cached Chromium, newest first. A dev box often has this and
    # nothing else on PATH — and the pinned path below only matches one CI
    # image, so without this the script bails on a machine that can render fine.
    *sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")), reverse=True),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

INJECT = """
<script>
function __toPNG(svgText, w, h) {
  return new Promise(function (resolve) {
    var img = new Image();
    var blobUrl = URL.createObjectURL(new Blob([svgText], {type: 'image/svg+xml;charset=utf-8'}));
    img.onload = function () {
      var cv = document.createElement('canvas');
      cv.width = w; cv.height = h;
      cv.getContext('2d').drawImage(img, 0, 0, w, h);
      URL.revokeObjectURL(blobUrl);
      resolve(cv.toDataURL('image/png'));
    };
    img.onerror = function () { URL.revokeObjectURL(blobUrl); resolve(null); };
    img.src = blobUrl;
  });
}
addEventListener('load', async function () {
  var want = %(want)s;             // null = every card
  var out = {};
  for (var i = 0; i < CARDS.length; i++) {
    var c = CARDS[i];
    if (want && want.indexOf(c.id) < 0) continue;
    out[c.id] = await __toPNG(svgOf(c), c.w, c.h);
  }
  var favSvg = %(favicon)s;
  var favSizes = %(favsizes)s;
  for (var j = 0; j < favSizes.length; j++) {
    var s = favSizes[j];
    out['favicon@' + s] = await __toPNG(favSvg, s, s);
  }
  var el = document.createElement('div');
  el.id = 'PNGOUT';
  el.textContent = JSON.stringify(out);
  document.body.replaceChildren(el);
  document.title = 'done';
});
</script>
"""


def _sandbox_flags():
    """Chromium cannot start its sandbox as uid 0, and cannot build one at all
    when the kernel blocks unprivileged user namespaces — the default on Ubuntu
    23.10+, which AppArmor-restricts any binary without a profile, and a
    downloaded Playwright Chromium is exactly that. Without this second case the
    render dies with a bare SIGABRT on an ordinary desktop.

    Dropping the sandbox is safe *here* specifically: the only page loaded is a
    temp file this script just wrote from local sources. On a machine where the
    sandbox works this stays empty and it stays on."""
    if os.name != "posix":
        return []
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return ["--no-sandbox"]
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") as f:
            if f.read().strip() == "1":
                return ["--no-sandbox"]
    except OSError:
        pass
    return []


def find_browser():
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    for n in ("chromium", "google-chrome", "msedge", "chromium-browser"):
        p = shutil.which(n)
        if p:
            return p
    sys.exit("no Chromium, Chrome or Edge found — install one, or export the "
             "PNGs by hand from social-pack.html (see Brand/README.md)")


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def cards():
    """Card ids and sizes read out of the built page rather than hardcoded, so
    adding a card to the template is the only edit needed."""
    src = open(PAGE, encoding="utf-8").read()
    out, seen = [], set()
    for cid, w, h in re.findall(r"card\('([\w-]+)',\s*(\d+),\s*(\d+),", src):
        if cid not in seen:
            seen.add(cid)
            out.append((cid, int(w), int(h)))
    return out


def harvest(browser, want, fav_sizes):
    """One browser launch: render everything to data URLs and read them back."""
    src = open(PAGE, encoding="utf-8").read()
    fav_path = os.path.join(BRAND, "favicon.svg")
    fav = open(fav_path, encoding="utf-8").read() if os.path.exists(fav_path) else ""
    inject = INJECT % {"want": json.dumps(want) if want else "null",
                       "favicon": json.dumps(fav),
                       "favsizes": json.dumps(fav_sizes)}
    body = src.rfind("</body>")
    patched = (src[:body] + inject + src[body:]) if body > 0 else src + inject

    fd, tmp = tempfile.mkstemp(suffix=".html", dir=ROOT, prefix=".render-")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(patched)
        dom = subprocess.run(
            [browser, "--headless=new", *_sandbox_flags(), "--disable-gpu",
             "--virtual-time-budget=30000", "--dump-dom",
             "file:///" + tmp.replace("\\", "/")],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace").stdout
    finally:
        os.remove(tmp)

    m = re.search(r'<div id="PNGOUT">(.*?)</div>', dom, re.S)
    if not m:
        sys.exit("the page never finished rendering — no PNGOUT payload came back. "
                 "Raise --virtual-time-budget in this script, or export by hand "
                 "from social-pack.html.")
    return json.loads(m.group(1))


def write(dest, data_url, w, h):
    if not data_url:
        sys.exit("%s: the page failed to rasterise this one" % os.path.basename(dest))
    raw = base64.b64decode(data_url.split(",", 1)[1])
    got = png_size(raw)
    if got != (w, h):
        # A silently mis-sized asset is worse than a missing one: it gets
        # committed and only shows up later as a blurry banner.
        sys.exit("%s: expected %dx%d, got %dx%d" % (os.path.basename(dest), w, h, got[0], got[1]))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(raw)
    print("  %-38s %5dx%-5d %4d KB" % (os.path.relpath(dest, ROOT), w, h, len(raw) // 1024))


def main():
    if not os.path.exists(PAGE):
        sys.exit("social-pack.html not found — run build.py first")
    browser = find_browser()
    print("using %s" % browser)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_fav = "--favicons" in sys.argv

    cs = cards()
    known = {c[0] for c in cs}
    if args:
        bad = [a for a in args if a not in known]
        if bad:
            sys.exit("unknown card(s): %s\nknown: %s" % (", ".join(bad), ", ".join(sorted(known))))
        cs = [c for c in cs if c[0] in args]
        print("rendering %d of %d cards — the rest keep whatever renderer made them; "
              "see the note at the top of this script" % (len(cs), len(known)))
    if only_fav:
        cs = []

    fav_sizes = [s for _, s in FAVICONS] if (not args or only_fav) else []
    got = harvest(browser, [c[0] for c in cs] if args else (None if not only_fav else []), fav_sizes)

    for cid, w, h in cs:
        outdir = next((d for p, d in DIRS.items() if cid.startswith(p)), "social")
        name = re.sub(r"^(gh|so)-", "", cid)
        write(os.path.join(ROOT, outdir, "%s-%dx%d.png" % (name, w, h)), got.get(cid), w, h)

    if fav_sizes:
        for name, size in FAVICONS:
            write(os.path.join(ROOT, "favicon", name), got.get("favicon@%d" % size), size, size)
        src = os.path.join(BRAND, "favicon.svg")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(ROOT, "favicon", "favicon.svg"))


if __name__ == "__main__":
    main()
