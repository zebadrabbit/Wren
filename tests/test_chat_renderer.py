"""The chat page's markdown renderer, exercised with node.

This is JS living inside chat.html, but it is a security boundary rather than
decoration: reply text is not always written by Wren. `web_plugin` summarises
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

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

PAGE = pathlib.Path(__file__).resolve().parent.parent / "wren" / "surfaces" / "chat.html"


def _renderer_js() -> str:
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("function esc(s)")
    end = src.index("async function api")
    return src[start:end]


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
