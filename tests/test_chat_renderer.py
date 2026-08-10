"""The chat page's markdown renderer, exercised with node.

This is JS living inside chat.html, but it is a security boundary rather than
decoration: reply text is not always written by Wren. `web_skill` summarises
scraped web pages and `send_to_person` relays other users' text, so a reply can
carry hostile input. md() escapes first and then applies a fixed whitelist to
the already-escaped string; these tests are what stop that ordering from being
"simplified" later.

Skipped when node is absent — it is a dev-time check, not a runtime dependency.
"""
import os, shutil, subprocess, pathlib, textwrap
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import pytest

import wren.communication

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Resolved through the package rather than spelled out from the repo root, so
# this tracks webchat.py's own `Path(__file__).with_name("chat.html")` instead
# of being a second copy of the location that can rot when the page moves.
PAGE = pathlib.Path(wren.communication.__file__).with_name("chat.html")


def _renderer_js() -> str:
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("function esc(s)")
    end = src.index("async function api")
    js = src[start:end]
    # A path that pointed somewhere plausible but wrong, or a page that stopped
    # carrying the renderer, would hand node an empty script and every
    # assertion below would pass against nothing.
    assert "function md(" in js, f"no md() renderer found in {PAGE}"
    return js


def render(markdown: str, tmp_path) -> str:
    """Run md() on one input and return the HTML it produced."""
    js = tmp_path / "r.mjs"
    js.write_text(
        _renderer_js()
        + "\nconst input = JSON.parse(process.argv[2]);"
        + "\nprocess.stdout.write(md(input));",
        encoding="utf-8",
    )
    import json
    out = subprocess.run(
        ["node", str(js), json.dumps(markdown)],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


# ── the escaping boundary ───────────────────────────────────────────────────

import re

# the only tags md() is ever allowed to emit
ALLOWED_TAGS = {"strong", "em", "code", "pre", "a", "ul", "li", "br"}


def tags_in(html: str) -> set[str]:
    return {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)", html)}


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    '"><script>alert(1)</script>',
    "<iframe src=javascript:alert(1)>",
    "<body onload=alert(1)>",
    "<a href='javascript:alert(1)'>x</a>",
    "<!--<script>alert(1)</script>-->",
    "<STYLE>@import'http://evil'</STYLE>",
])
def test_hostile_html_produces_no_tag_outside_the_whitelist(hostile, tmp_path):
    # Substring checks like `"onload=" not in out` are the wrong test: escaped
    # text such as "&lt;svg/onload=alert(1)&gt;" contains that substring and is
    # completely inert. What matters is whether a TAG was formed.
    out = render(hostile, tmp_path)
    assert tags_in(out) <= ALLOWED_TAGS, f"escaped output produced tags: {tags_in(out)}"
    assert "&lt;" in out, "the input's '<' should have been escaped"


@pytest.mark.parametrize("scheme_attempt", [
    "[click](javascript:alert(1))",
    "[click](data:text/html,<script>alert(1)</script>)",
    "[click](vbscript:msgbox(1))",
    "[click](JaVaScRiPt:alert(1))",
])
def test_only_http_schemes_reach_an_href(scheme_attempt, tmp_path):
    out = render(scheme_attempt, tmp_path).lower()
    assert 'href="javascript' not in out
    assert 'href="data:' not in out
    assert 'href="vbscript' not in out


def test_attribute_break_attempt_is_inert(tmp_path):
    out = render('" onmouseover="alert(1)', tmp_path)
    assert 'onmouseover="alert' not in out


def test_html_inside_a_code_fence_is_still_escaped(tmp_path):
    out = render("```\n<script>alert(1)</script>\n```", tmp_path)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ── the placeholder-collision regression ────────────────────────────────────

def test_standalone_numbers_are_not_eaten_as_code_placeholders(tmp_path):
    # Code fences are parked behind a placeholder while the other rules run.
    # An earlier version delimited that placeholder with spaces (" 3 "), so any
    # standalone number in ordinary prose matched the restore regex and
    # rendered as "undefined".
    assert render("step 3 of the process", tmp_path) == "step 3 of the process"
    assert render("1 2 3 4 5", tmp_path) == "1 2 3 4 5"
    assert "undefined" not in render("I have 7 apples and 42 pears", tmp_path)


def test_fence_restores_correctly_alongside_numbers(tmp_path):
    out = render("step 3\n```\ncode here\n```\nstep 4", tmp_path)
    assert "<pre><code>code here" in out
    assert "step 3" in out and "step 4" in out
    assert "undefined" not in out


# ── the whitelist actually works ────────────────────────────────────────────

def test_bold_and_inline_code(tmp_path):
    assert render("**hi**", tmp_path) == "<strong>hi</strong>"
    assert render("use `foo`", tmp_path) == "use <code>foo</code>"


def test_bare_and_markdown_links(tmp_path):
    assert 'href="https://example.com"' in render("see https://example.com now", tmp_path)
    out = render("[Wren](https://example.com)", tmp_path)
    assert 'href="https://example.com"' in out and ">Wren</a>" in out


def test_links_open_safely(tmp_path):
    out = render("https://example.com", tmp_path)
    assert 'rel="noopener noreferrer"' in out


def test_unordered_list(tmp_path):
    out = render("- a\n- b", tmp_path)
    assert "<ul>" in out and "<li>a</li>" in out and "<li>b</li>" in out


def test_plain_text_passes_through_unchanged(tmp_path):
    assert render("Saved.", tmp_path) == "Saved."


# ── the plugins panel ────────────────────────────────────────────────────────

def test_page_has_the_plugins_panel_and_its_gear():
    src = PAGE.read_text(encoding="utf-8")
    assert 'id="gear"' in src
    assert 'id="plugins"' in src
    assert "/api/plugins" in src


def test_panel_wires_every_endpoint_it_needs():
    src = PAGE.read_text(encoding="utf-8")
    for fragment in ('"/api/settings"', '"/api/plugins"', "/api/plugins/${s.module}"):
        assert fragment in src, f"panel never calls {fragment}"


def test_panel_renders_server_text_without_building_html():
    # Plugin names and inactive reasons come from the server. The page's
    # standing rule is escape-first, never build HTML from a value -- so these
    # go in via textContent, not interpolation.
    src = PAGE.read_text(encoding="utf-8")
    assert "textContent = s.name" in src
    assert "textContent = c.name" not in src or "innerHTML = `${c.name}" not in src


# ── branding ─────────────────────────────────────────────────────────────

def test_page_carries_the_wren_favicon_inline():
    src = PAGE.read_text(encoding="utf-8")
    assert 'rel="icon"' in src
    assert "data:image/svg+xml" in src, "favicon must be inline, not a separate request"
    assert 'name="theme-color"' in src


def test_login_screen_shows_the_lockup():
    src = PAGE.read_text(encoding="utf-8")
    assert 'id="brandmark"' in src


def test_lockup_wordmark_uses_currentcolor_not_hardcoded_ink():
    # A substring check, like its neighbours -- it proves the literal string
    # fill="currentColor" is present inside the brandmark svg's markup, and
    # that the hardcoded ink is not, nothing more. It cannot see actual
    # rendered contrast; it only stops a future edit from silently reverting
    # to fill="#3A322B", which measures ~1.40:1 (invisible) against the
    # dark-mode --bg.
    src = PAGE.read_text(encoding="utf-8")
    block = src[src.index('<svg id="brandmark"'):]
    block = block[:block.index("</svg>") + len("</svg>")]
    # Strip comments first: the comment above the path explains WHY the fill
    # is currentColor and contains that word, which would satisfy a naive
    # substring check even if the attribute itself were reverted.
    markup = re.sub(r"<!--.*?-->", "", block, flags=re.S)
    assert 'fill="currentColor"' in markup
    assert 'fill="#3A322B"' not in markup, "wordmark ink is invisible on the dark theme"


def test_sidebar_uses_the_simple_mark_not_the_full_one():
    # Below ~24px the full mark's wing and eye stop resolving and read as dirt.
    # The sidebar icon is ~20px, so it must be the one-colour silhouette.
    src = PAGE.read_text(encoding="utf-8")
    assert 'class="sidemark"' in src


def test_page_makes_no_external_requests():
    # The whole point of inlining: one file, no network. Guard it.
    src = PAGE.read_text(encoding="utf-8")
    for scheme in ("http://", "https://"):
        for tag in ("src=", "href="):
            assert f'{tag}"{scheme}' not in src, f"external {tag} reference found"
