#!/usr/bin/env python3
"""Inline the shared sources into each self-contained deliverable.

Every output is one file that opens in a browser with no build step, no
webfonts and no network requests: tokens, brand geometry and the measured
architecture data are substituted into the templates here.

    python3 build.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
TPL = os.path.join(ROOT, "templates")
DIST = os.environ.get("WREN_BRAND_DIST") or ROOT

LOGO_JSON = os.path.join(SRC, "logo.json")
ARCH_JSON = os.path.join(SRC, "arch.json")

OUT = {
    "brand.html": "brand-system.html",
    "social.html": "social-pack.html",
}


def _regen(script, dest, args=(), why=""):
    """Run a generator and capture stdout to `dest`. If it fails, keep the last
    good output rather than failing the whole build — the geometry and the
    plugin graph only change when the project does, and a brand rebuild should
    not be blocked by a missing font or a moved checkout."""
    try:
        out = subprocess.run([sys.executable, os.path.join(SRC, script), *args],
                             check=True, capture_output=True, text=True).stdout
        if not out.strip():
            raise RuntimeError("no output")
        with open(dest, "w") as f:
            f.write(out)
        return True
    except Exception as e:
        if not os.path.exists(dest):
            raise SystemExit(
                "%s failed and there is no %s to fall back on: %s\n%s"
                % (script, os.path.basename(dest), e, why))
        detail = getattr(e, "stderr", "") or ""
        print("%s failed (%s) — reusing existing %s%s"
              % (script, type(e).__name__, os.path.basename(dest),
                 "\n  " + detail.strip().splitlines()[-1] if detail.strip() else ""))
        return False


_regen("logo.py", LOGO_JSON,
       why="logo.py needs fonttools and TeX Gyre Adventor for the wordmark.")
# The repo lives one level up from Brand/. gen_arch.py measures it.
_regen("gen_arch.py", ARCH_JSON, args=[os.path.join(ROOT, "..")],
       why="gen_arch.py must be able to see ../wren/.")

tokens = open(os.path.join(SRC, "tokens.css")).read()
logo = open(LOGO_JSON).read().strip()
arch = open(ARCH_JSON).read().strip()
contrast = open(os.path.join(SRC, "contrast.js")).read()

os.makedirs(DIST, exist_ok=True)
built = 0
for tpl, out in OUT.items():
    p = os.path.join(TPL, tpl)
    if not os.path.exists(p):
        print("skip (no template):", tpl)
        continue
    s = open(p).read()
    s = s.replace("/*__TOKENS__*/", tokens)
    s = s.replace("/*__LOGO__*/", "const LOGO = " + logo + ";")
    s = s.replace("/*__ARCH__*/", "const ARCH = " + arch + ";")
    s = s.replace("/*__CONTRAST__*/", contrast)
    dest = os.path.join(DIST, out)
    with open(dest, "w") as f:
        f.write(s)
    built += 1
    print("wrote %-22s %6.1f KB" % (out, len(s) / 1024))

if not built:
    raise SystemExit("nothing built — templates/ is empty?")

t = json.loads(arch)["totals"]
print("architecture measured: %d chat + %d input plugins (%d stub), "
      "%d skills, %d intents" % (t["chat"], t["input"], t["stubs"],
                                 t["skills"], t["intents"]))
