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
os.environ.setdefault("WREN_OWNER_ID", "1")
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


def test_the_global_button_rule_sets_a_background():
    # A button that inherits no background gets the browser default -- white --
    # while inheriting the dark theme's near-white text, so its label is
    # painted white on white. This shipped three times: the panel toggles, the
    # close button, and the sidebar gear.
    #
    # An earlier version of this test enumerated the buttons it knew about,
    # which is exactly why the gear got through. Guard the global rule instead:
    # with a background there, no future button can render as a blank box, and
    # nobody has to remember to extend a list.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("button { font: inherit")
    rule = src[start:src.index("}", start)]
    assert "background" in rule, (
        "the global button rule sets no background; any button without one of "
        "its own will render as a white box with an invisible label"
    )


def test_plugin_rows_can_wrap_so_the_reason_gets_its_own_line():
    # .why is flex-basis:100%, which only takes a new line if the row wraps.
    # Without this the reason sits inline and squeezes the plugin name.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index(".prow {")
    assert "flex-wrap: wrap" in src[start:src.index("}", start)]


# ── landing screen ───────────────────────────────────────────────────────

def test_the_mark_is_defined_once_and_referenced():
    # Branding inlined the silhouette in the sidebar; the landing header needs
    # it too. Define it once as a <symbol> and <use> it, rather than pasting
    # the geometry a third time.
    src = PAGE.read_text(encoding="utf-8")
    assert src.count('<symbol id="wren-mark"') == 1
    assert src.count('href="#wren-mark"') >= 2


def test_landing_greets_and_offers_starters():
    src = PAGE.read_text(encoding="utf-8")
    assert "/api/me" in src
    assert "Good " in src            # the greeting template
    assert "STARTERS" in src


def test_every_pill_starter_maps_to_a_real_skill_module():
    # A renamed module would otherwise leave a pill that silently does nothing.
    from wren import registry
    src = PAGE.read_text(encoding="utf-8")
    block = src[src.index("const STARTERS"):]
    block = block[:block.index("}")]
    real = {registry.skill_key(p) for p in registry.PLUGINS}
    for line in block.splitlines():
        if ":" not in line or "_skill" not in line:
            continue
        module = line.split(":")[0].strip().strip('"\',')
        assert module in real, f"{module} is not a registered skill module"


def test_model_selector_is_wired_to_its_endpoint():
    src = PAGE.read_text(encoding="utf-8")
    assert "/api/models" in src


def test_model_dropdown_always_has_an_option_matching_the_current_model():
    # Finding 1: `o.selected = (m === d.current)` only ever selects an EXISTING
    # option -- if d.current is not in d.models (a model removed from the
    # ollama host, or LM Studio listing only loaded models), nothing gets
    # selected and the browser silently defaults to d.models[0], displaying a
    # model that is not the one actually answering, with no sign anything is
    # wrong. This is a substring test: it cannot execute the page, observe
    # what a real <select> renders, or prove the browser never falls back to
    # index 0 -- it can only prove the source contains a branch that adds an
    # option for d.current before the main loop runs, closing the gap that
    # loop leaves.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("async function loadModels")
    end = src.index("\nasync function start")
    block = src[start:end]
    assert "d.models.includes(d.current)" in block, (
        "no guard for d.current missing from d.models -- the dropdown can "
        "silently default to models[0] while a different model actually answers"
    )
    # The value PATCHed back must stay the bare model id even though the label
    # explains the mismatch -- confusing the two would PATCH the decorated text.
    assert "o.value = d.current" in block


def test_composer_explains_an_empty_model_list_instead_of_going_silent():
    # Finding 2: /api/models returns a `reason` (provider unreachable, or
    # unconfigured) whenever the list comes back empty, and it used to be
    # discarded -- leaving "the list failed" and "this is the model"
    # indistinguishable static text. Substring-only: cannot prove #modeltext's
    # rendered contents in a browser, only that the empty-list branch reads
    # d.reason at all.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("async function loadModels")
    end = src.index("\nasync function start")
    block = src[start:end]
    empty_branch = block[block.index("!d.models.length"):block.index("$(\"#model\").innerHTML")]
    assert "d.reason" in empty_branch


def test_showEmpty_bails_if_its_own_landing_was_superseded():
    # Finding 3: showEmpty() is async but every call site is unawaited, so its
    # /api/me continuation can resolve after #landing (or #inner) has already
    # been rebuilt by a later, faster caller -- deterministically on the first
    # message of a fresh install (send() removes #landing synchronously while
    # showEmpty()'s fetch is still in flight) and on a double-clicked "New".
    # Substring-only: cannot execute the page or prove the unhandled rejection
    # is actually gone at runtime, only that the source captures the landing
    # node up front and checks it is still connected before writing into it.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("async function showEmpty")
    end = src.index("\nasync function openConvo")
    block = src[start:end]
    assert "landing.isConnected" in block, (
        "no guard against a stale #landing -- the /api/me continuation can "
        "write into a subtree a faster caller already tore down"
    )
    # Must not become the alternative "fix" the review explicitly rejected:
    # awaiting showEmpty() at every call site would make send() wait on a
    # greeting fetch just to post a message.
    assert "await showEmpty" not in src


def test_api_me_is_fetched_once_per_page_load_and_shared():
    # Finding 6: showEmpty() needs /api/me for the greeting/pills, and the
    # model selector needs it as a fallback -- start() used to fire two
    # independent requests for the same page load. Substring-only: cannot
    # observe actual network traffic, only that start()'s source calls
    # api("/api/me") exactly once and forwards that value into showEmpty(...)
    # rather than the bare call that would fetch a second time.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("async function start()")
    end = src.index("\nif (token)")
    block = src[start:end]
    assert block.count('api("/api/me")') == 1, (
        "start() should fetch /api/me exactly once per page load"
    )
    assert "showEmpty(me)" in block, (
        "start() should forward its own /api/me fetch into showEmpty(...) "
        "instead of calling showEmpty() with no argument, which would fetch "
        "/api/me a second time"
    )


def test_model_select_does_not_stretch_or_overflow_the_composer():
    # Finding 7: #composer is display:flex with the default align-items:stretch,
    # so without align-self #model grows to match #text's height (up to 180px)
    # as you type, and without max-width a long model id (e.g. a full
    # openrouter slug) squeezes #text instead of truncating.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("#model {")
    end = src.index("}", start)
    rule = src[start:end]
    assert "align-self" in rule
    assert "max-width" in rule


def test_model_change_handler_has_error_handling():
    # This is a substring grep, not an execution of the JS -- it cannot prove
    # the try/catch actually runs at runtime, that #modeltext genuinely becomes
    # visible in a browser, or that the select's displayed value truly reverts.
    # It can only prove the handler's *source* contains the shape of error
    # handling (a catch, something touching #modeltext, a revert of the
    # select's value) rather than a bare unguarded await -- which is exactly
    # the silent-failure bug code review caught here, and exactly the class of
    # bug this kind of test cannot catch again if a future edit keeps these
    # substrings but rewires the logic behind them (e.g. swaps which branch
    # reverts vs. which reports, or reverts to the wrong value).
    src = PAGE.read_text(encoding="utf-8")
    start = src.index('$("#model").onchange = async')
    end = src.index("};", start)
    block = src[start:end]
    assert "catch" in block, "the PATCH is unguarded -- a 400 becomes an unhandled rejection"
    assert '$("#modeltext")' in block, "a failed change is never surfaced to the user"
    assert '$("#model").value = d.current' in block, (
        "a failed change leaves the dropdown showing a selection the server never saved"
    )


def test_card_renderer_escapes_every_value_it_draws():
    # card data is user-typed ("add <script> to shopping"), so every
    # interpolation into innerHTML must go through esc()
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("function shoppingCard(")
    block = src[start:src.index("\nfunction ", start + 1)]
    assert "esc(" in block
    # the item text must never reach innerHTML raw
    assert "+ i.text +" not in block


def test_card_controls_dispatch_intents_not_sentences():
    # the whole point of /api/dispatch: a button sends an intent, not English
    # for the classifier to re-interpret
    src = PAGE.read_text(encoding="utf-8")
    assert "/api/dispatch" in src
    assert "remove_shopping_item" in src
    assert "add_shopping_item" in src


def test_a_card_message_renders_a_card_and_not_just_prose():
    src = PAGE.read_text(encoding="utf-8")
    # both paths must honour m.card: the live reply and a reloaded transcript
    assert src.count("m.card") >= 1
    assert "cards" in src


# ── card fixes: params, failure paths, serialisation (final review wave) ────
#
# All substring checks, like their neighbours above -- none of these execute
# the page. They can prove the *shape* the fix requires is present in the
# source (a try wraps the awaits, params is spread rather than narrowed, the
# busy flag is checked and set); none of them can prove a catch block
# actually runs at runtime, that a button truly re-enables in a browser, or
# that two real overlapping clicks are actually serialised. That needs a
# browser or a JS test runner, neither of which this repo has.

def test_dispatch_forwards_the_whole_params_object_not_just_content():
    # F1: dispatch() used to send only {intent, content}, and every call site
    # narrowed its params down to a bare content string before calling it.
    # That silently drops any other Ctx field (tags, person, when) a future
    # card needs for a filtered re-dispatch.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("async function dispatch(")
    end = src.index("\n}", start)
    block = src[start:end]
    assert "async function dispatch(intent, params)" in block
    assert "...(params || {})" in block
    assert "content: content" not in block, "dispatch still narrows params to content"


def test_mount_stored_card_passes_the_whole_params_object():
    # F1: this call site used to be
    # dispatch(card.intent, (card.params || {}).content || ""), which is the
    # concrete case the design doc calls out -- a tag-filtered notes card
    # would lose its filter the moment it re-renders from a stored message.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("async function mountStoredCard")
    end = src.index("\n}", start)
    block = src[start:end]
    assert "dispatch(card.intent, card.params || {})" in block
    assert ".content" not in block, "mountStoredCard still narrows params down to .content"


def _shopping_card_source() -> str:
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("function shoppingCard(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def test_card_remove_handler_has_a_full_failure_path():
    # F2 + F3: a try/catch/finally around both dispatches, the button
    # re-enabled in finally (not just on the happy path), the skill's own
    # message surfaced via cardStatus, and the in-flight guard from F3.
    block = _shopping_card_source()
    cx_start = block.index('mount.querySelectorAll(".cx")')
    cx_end = block.index('mount.querySelector(".cadd")')
    cx_block = block[cx_start:cx_end]
    assert "try {" in cx_block
    assert "catch" in cx_block
    assert "finally" in cx_block
    assert "b.disabled = false" in cx_block, "the ✕ button is never re-enabled on failure"
    assert "cardStatus(mount" in cx_block, "neither success nor failure is surfaced in the card"
    assert "if (mount._busy) return;" in cx_block, "no guard against a second click mid-dispatch"
    assert "mount._busy = true" in cx_block and "mount._busy = false" in cx_block


def test_card_add_handler_restores_typed_text_on_failure():
    # F2: input.value used to be cleared before the await and never restored,
    # so a failed add silently destroyed what the user typed.
    block = _shopping_card_source()
    add_start = block.index('mount.querySelector(".cadd")')
    add_block = block[add_start:]
    assert "try {" in add_block
    assert "catch" in add_block
    assert "finally" in add_block
    assert "input.value = text" in add_block, "a failed add does not restore the typed text"
    assert "cardStatus(mount" in add_block
    assert "if (mount._busy) return;" in add_block


def test_card_refresh_guards_against_an_empty_cards_array():
    # F2: `fresh.cards[0].data` used to be accessed unconditionally, which
    # throws (into an unhandled rejection) the moment the server returns no
    # card at all. This proves the guard text precedes the cards[0] access in
    # source order -- it cannot prove the access is unreachable when the
    # guard is false, only that a bare `renderCard(..., fresh.cards[0]...)`
    # with no preceding check is no longer what the source contains.
    block = _shopping_card_source()
    guard_at = block.index("fresh.cards && fresh.cards.length")
    access_at = block.index("fresh.cards[0]")
    assert guard_at < access_at, "cards[0] is reached before the emptiness guard"


def test_card_handlers_serialise_on_an_in_flight_flag():
    # F3: two quick clicks must not both be in flight at once. The busy flag
    # lives on the mount node (not on a button), so it serialises across BOTH
    # controls sharing one card -- a remove mid-flight also blocks a submit.
    block = _shopping_card_source()
    assert block.count("mount._busy") >= 4, (
        "expected the busy flag to be checked and reset in both the remove "
        "and add handlers"
    )
def test_bubbles_carry_a_copyable_speaker_label():
    # Substring check on bubble()'s source, not a rendered DOM: it proves the
    # page still emits the labels and still hides them with a technique that
    # survives copy/paste (display:none would not), which is the whole point.
    src = PAGE.read_text(encoding="utf-8")
    start = src.index("function bubble(role, html)")
    block = src[start:src.index("}", src.index("d.innerHTML", start))]
    assert '"Wren: "' in block and '"You: "' in block, "copied transcripts lose attribution"
    assert 'class="sr"' in block
    assert "display: none" not in src[src.index(".sr {"):src.index(".sr {") + 200]
