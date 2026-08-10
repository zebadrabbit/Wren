# Chat Landing Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the web chat's one-line empty state with a landing screen — Wren mark, a greeting that knows your name, prompt-starter pills per skill, and a model selector in the composer.

**Architecture:** Four independent pieces. `config.SETTABLE` collapses from three parallel maps into one `Setting` record per key, which then absorbs a fourth per-key behaviour (`apply`) so model keys can write `os.environ` and rebuild `LLM_CHAIN`. A new `GET /api/me` (any whitelisted user, unlike the owner-only `/api/plugins`) supplies the greeting name and the enabled skill list. A new owner-only `GET /api/models` proxies the active provider's model list. `chat.html` rewrites `showEmpty()` into the landing state.

**Tech Stack:** Python 3.12, stdlib, `aiohttp`, `pytest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-09-chat-landing-redesign-design.md`

## Global Constraints

- **No new dependencies.** stdlib plus what is already in `requirements.txt`.
- **Hard rule 2:** a communication plugin contains no domain logic. `webchat.py` authenticates, checks ownership, calls into `config`/`registry`, returns JSON.
- **`SETTABLE` is a positive allowlist and the security boundary.** `GET /api/plugins` returns the values of everything in it. Model *names* may be added; `*_API_KEY` entries are credentials and must never be.
- **Never run an ad-hoc script against the real `wren.db`.** Set `WREN_DB=$(mktemp -d)/scratch.db` before importing anything from `wren`. `pytest` is safe — `tests/conftest.py` sets it per test.
- **Owner id is `config.WHITELIST["owner"]`** (an `int`). There is no `config.OWNER_ID` attribute.
- **`chat.html` hazard:** the markdown renderer uses U+E000 as a code-fence placeholder written as a six-character JS escape. The Edit tool has corrupted it into a raw byte before. Do not touch the `esc()`/`md()` region; add new blocks. `pytest -q tests/test_chat_renderer.py` executes the real JS through node and is the canary. Restore with `git show HEAD:wren/communication/chat.html` if damaged.
- **Every button must inherit a background.** The global `button` rule now sets `background: transparent` — do not remove it; three separate white-box bugs came from its absence.
- Comments explain *why*. `# ponytail:` / `// ponytail:` for deliberate simplifications naming the ceiling.
- Test style: build a fake, call the thing, assert on plain values.
- Suite is green at **632 passed** before this plan starts.
- **Known, pre-existing, not yours:** python-dotenv searches parent directories, so tests can pick up the live `.env`. `pytest tests/test_config_overrides.py tests/test_config.py` in that literal order fails 5 tests for that reason. A normal full-suite run is green — use that as signal.

---

## File Structure

| File | Responsibility |
|---|---|
| `wren/config.py` | `Setting` record replacing `SETTABLE`/`_BOOT_COERCERS`/`_SERIALIZE`; model keys; `reload_llm_chain()` |
| `wren/communication/webchat.py` | `GET /api/me`, `GET /api/models` |
| `wren/communication/chat.html` | `<symbol>` mark, landing state, pills, model selector |
| `tests/test_config_overrides.py` | consolidation + model-switch coverage |
| `tests/test_webchat_plugins.py` | the two new endpoints |
| `tests/test_chat_renderer.py` | landing-state page guards |

Task order matters: Task 1 reshapes `SETTABLE`, Task 2 builds on it for models, Tasks 3–4 add endpoints, Task 5 is the UI, Task 6 documents.

---

### Task 1: Collapse SETTABLE's three maps into one Setting record

**Files:**
- Modify: `wren/config.py` (`SETTABLE`, `_SERIALIZE`, `serialize_setting`, `_BOOT_COERCERS`, `apply_overrides`, `set_override`, `clear_override`)
- Test: `tests/test_config_overrides.py` (append)

**Interfaces:**
- Consumes: nothing new
- Produces: `config.Setting` (NamedTuple with `coerce`, `boot_coerce`, `serialize`, `apply`); `SETTABLE: dict[str, Setting]`; unchanged public `set_override(key, raw)`, `clear_override(key)`, `apply_overrides()`, `serialize_setting(key)`

**Why:** three maps are keyed by the same names with nothing enforcing they agree — a review flagged exactly that. Task 2 needs a fourth behaviour; consolidating first means it lands in one place.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_overrides.py`:

```python
def test_every_settable_is_a_setting_record():
    for key, spec in config.SETTABLE.items():
        assert isinstance(spec, config.Setting), f"{key} is not a Setting"
        assert callable(spec.coerce), f"{key}.coerce is not callable"


def test_every_settable_round_trips_through_serialize():
    # serialize_setting(key) must produce a string that set_override(key, ...)
    # turns back into the identical value. This is the property that broke when
    # GITHUB_WATCH was served as a Python repr.
    settings.init_db()
    for key in config.SETTABLE:
        if key == "NOTIFY_VIA":
            continue  # validated against the router, which is empty in tests
        original = getattr(config, key)
        text = config.serialize_setting(key)
        try:
            config.set_override(key, text)
            assert getattr(config, key) == original, f"{key} did not round-trip"
        finally:
            config.clear_override(key)


def test_notify_via_still_validates_strictly_on_the_interactive_path():
    settings.init_db()
    router.reset()
    with pytest.raises(ValueError):
        config.set_override("NOTIFY_VIA", "telegram")


def test_notify_via_still_applies_leniently_at_boot():
    # apply_overrides runs before any plugin registers, so the router is empty
    # and a strict check would discard every stored value.
    settings.init_db()
    router.reset()
    settings.set("NOTIFY_VIA", "telegram")
    try:
        config.apply_overrides()
        assert config.NOTIFY_VIA == "telegram"
    finally:
        settings.unset("NOTIFY_VIA")
        config.apply_overrides()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_config_overrides.py -k "setting_record or round_trips"`
Expected: FAIL — `AttributeError: module 'wren.config' has no attribute 'Setting'`

- [ ] **Step 3: Define the record and rebuild SETTABLE**

In `wren/config.py`, replace the `SETTABLE` dict, the `_SERIALIZE` dict and the `_BOOT_COERCERS` dict with:

```python
def _apply_attr(key: str, value) -> None:
    """Default apply: assign onto this module. Works because every ordinary
    consumer reads config.X at call time."""
    globals()[key] = value


class Setting(NamedTuple):
    """Everything the system needs to know about one runtime-editable setting.

    Previously three dicts keyed by the same names -- SETTABLE, _SERIALIZE and
    _BOOT_COERCERS -- with nothing enforcing they stayed in step. One record
    means adding a setting is one edit in one place.
    """
    coerce: Callable[[str], object]
    # Used by apply_overrides instead of coerce when boot cannot do the full
    # check. NOTIFY_VIA validates against the router, which is empty at boot.
    boot_coerce: Callable[[str], object] | None = None
    serialize: Callable[[object], str] = str
    apply: Callable[[str, object], None] = _apply_attr


SETTABLE = {
    "TIMEZONE":              Setting(_validate_timezone),
    "REMINDER_POLL_SECONDS": Setting(_coerce_poll_seconds),
    "EMAIL_POLL_SECONDS":    Setting(_coerce_poll_seconds),
    "GITHUB_POLL_SECONDS":   Setting(_coerce_poll_seconds),
    "SEARXNG_URL":           Setting(str.strip),
    "FIRECRAWL_URL":         Setting(str.strip),
    "GITHUB_WATCH":          Setting(_parse_github_watch, serialize=_serialize_github_watch),
    "EMAIL_WATCH":           Setting(_parse_email_watch, serialize=_serialize_email_watch),
    "NOTIFY_VIA":            Setting(_coerce_notify_via, boot_coerce=str.strip),
}
```

Add `from typing import Callable, NamedTuple` to the imports at the top of the file.

- [ ] **Step 4: Update the three consumers**

`serialize_setting`:

```python
def serialize_setting(key: str) -> str:
    return SETTABLE[key].serialize(globals()[key])
```

In `apply_overrides`, replace the coercer lookup and assignment (keep the
`skill.` skip and both warning branches exactly as they are):

```python
        spec = SETTABLE.get(key)
        if spec is None:
            logging.warning(f"ignoring unknown stored setting '{key}'")
            continue
        coerce = spec.boot_coerce or spec.coerce
        try:
            spec.apply(key, coerce(raw))
        except (ValueError, RuntimeError) as e:
            logging.warning(f"ignoring invalid stored setting '{key}'={raw!r}: {e}")
```

In `set_override`, replace the coerce-and-assign pair:

```python
    spec = SETTABLE[key]
    value = spec.coerce(raw)         # raises ValueError/RuntimeError if bad
    settings.set(key, raw)
    spec.apply(key, value)
    return value
```

`clear_override` keeps restoring from `_DEFAULTS`, but through `apply` so a
non-default applier is honoured:

```python
    settings.unset(key)
    SETTABLE[key].apply(key, _DEFAULTS[key])
```

- [ ] **Step 5: Run the new tests**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_config_overrides.py`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q`
Expected: PASS, roughly 636. Every pre-existing test in `tests/test_config_overrides.py` must still pass unchanged — that is the proof the consolidation preserved behaviour.

- [ ] **Step 7: Commit**

```bash
git add wren/config.py tests/test_config_overrides.py
git commit -m "refactor(config): one Setting record instead of three parallel maps"
```

---

### Task 2: Model keys and the LLM_CHAIN rebuild

**Files:**
- Modify: `wren/config.py` (model settables, `reload_llm_chain`)
- Test: `tests/test_config_overrides.py` (append)

**Interfaces:**
- Consumes: `config.Setting` from Task 1
- Produces: `config.reload_llm_chain() -> None`; `SETTABLE` entries for `OLLAMA_MODEL`, `LMSTUDIO_MODEL`, `OPENAI_MODEL`, `CLAUDE_MODEL`, `OPENROUTER_MODEL`

**The thing to understand before writing code:** the ordinary live-apply
mechanism does not reach the model. `providers.py` reads
`os.environ.get(spec["model_env"])`, not `config`. `config.LLM_CHAIN` is
resolved once at import (`config.py:67`) into dicts with the model baked in.
There is no `config.OLLAMA_MODEL` attribute at all. So these keys need an
`apply` that writes `os.environ` and rebuilds the chain.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_overrides.py`:

```python
def test_model_keys_are_settable_but_api_keys_are_not():
    for key in ("OLLAMA_MODEL", "LMSTUDIO_MODEL", "OPENAI_MODEL",
                "CLAUDE_MODEL", "OPENROUTER_MODEL"):
        assert key in config.SETTABLE
    for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        assert secret not in config.SETTABLE


def test_setting_a_model_changes_what_the_chain_will_use(monkeypatch):
    # The chain is resolved at import with the model baked in, so this proves
    # the rebuild actually happened rather than just an attribute being set.
    settings.init_db()
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://test")
    monkeypatch.setenv("LMSTUDIO_MODEL", "before-model")
    config.reload_llm_chain()
    assert any(c["model"] == "before-model" for c in config.LLM_CHAIN)

    config.set_override("LMSTUDIO_MODEL", "after-model")
    try:
        assert any(c["model"] == "after-model" for c in config.LLM_CHAIN)
        assert not any(c["model"] == "before-model" for c in config.LLM_CHAIN)
        assert os.environ["LMSTUDIO_MODEL"] == "after-model"
    finally:
        config.clear_override("LMSTUDIO_MODEL")


def test_a_rebuild_that_would_empty_the_chain_is_refused(monkeypatch):
    # Wren failing to boot with no provider is a loud, clear error. Wren
    # silently losing its last provider at runtime because someone touched a
    # dropdown is not.
    settings.init_db()
    before = list(config.LLM_CHAIN)
    monkeypatch.setattr(config, "_provider_names", [])
    with pytest.raises(RuntimeError):
        config.reload_llm_chain()
    assert config.LLM_CHAIN == before
```

Add `import os` to that test file if it is not already imported.

- [ ] **Step 2: Run it and confirm it fails**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_config_overrides.py -k "model or chain"`
Expected: FAIL — `AttributeError: module 'wren.config' has no attribute 'reload_llm_chain'`

- [ ] **Step 3: Write the rebuild and the applier**

Add to `wren/config.py`, after `LLM_CHAIN` is defined:

```python
def reload_llm_chain() -> None:
    """Re-resolve LLM_CHAIN from the current environment.

    Deliberately re-runs the same expression import does, so a runtime model
    switch and a restart cannot diverge.

    Refuses a rebuild that would leave no usable provider: an empty chain at
    boot is a loud startup error, but an empty chain at runtime would mean Wren
    silently stops being able to answer because someone touched a dropdown.
    """
    global LLM_CHAIN
    rebuilt = [c for c in (providers.resolve(n) for n in _provider_names) if c is not None]
    if not rebuilt:
        raise RuntimeError(
            "that change would leave no usable LLM provider; keeping the current one"
        )
    LLM_CHAIN = rebuilt


def _apply_model(key: str, value) -> None:
    """Model names live in os.environ, not on this module: providers.resolve()
    reads the environment, and LLM_CHAIN caches the resolved model. Setting a
    config attribute would do nothing at all."""
    os.environ[key] = value
    reload_llm_chain()
```

Then add the five model entries to `SETTABLE`:

```python
    # Model NAMES are not credentials, so they belong in the allowlist; the
    # matching *_API_KEY values are and never will. Listed literally rather
    # than derived from LLM_PROVIDERS so the allowlist stays readable in one
    # place. Note a side effect worth knowing: providers.resolve() returns None
    # when a provider's model is unset, so setting one here can REVIVE that
    # provider into the fallback chain if it is already named in LLM_PROVIDERS
    # -- but reload_llm_chain() only re-resolves _provider_names (captured once
    # at import from LLM_PROVIDERS, itself not in SETTABLE), so this can never
    # add a provider LLM_PROVIDERS never named; that write just silently does
    # nothing.
    "OLLAMA_MODEL":     Setting(str.strip, apply=_apply_model),
    "LMSTUDIO_MODEL":   Setting(str.strip, apply=_apply_model),
    "OPENAI_MODEL":     Setting(str.strip, apply=_apply_model),
    "CLAUDE_MODEL":     Setting(str.strip, apply=_apply_model),
    "OPENROUTER_MODEL": Setting(str.strip, apply=_apply_model),
```

**`_DEFAULTS` needs care.** It is built as `{key: globals()[key] for key in SETTABLE}`, and these five have no module attribute — that comprehension will `KeyError`. Change it to fall back to the environment:

```python
_DEFAULTS = {key: globals().get(key, os.environ.get(key, "")) for key in SETTABLE}
```

- [ ] **Step 4: Run the new tests**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_config_overrides.py`
Expected: PASS

- [ ] **Step 5: Full suite**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q`
Expected: PASS, roughly 639

- [ ] **Step 6: Commit**

```bash
git add wren/config.py tests/test_config_overrides.py
git commit -m "feat(config): switch the LLM model at runtime by rebuilding the chain"
```

---

### Task 3: GET /api/me

**Files:**
- Modify: `wren/communication/webchat.py` (handler + route registration)
- Test: `tests/test_webchat_plugins.py` (append)

**Interfaces:**
- Consumes: `registry.PLUGINS`, `registry.is_enabled`, `registry.skill_key`, `config.WHITELIST`, `config.id_to_name()`, `config.SETTABLE`
- Produces: `GET /api/me` → `{"name": str | None, "skills": [str, ...], "model": str | None}`

`model` is here as well as in `/api/models` on purpose. `/api/models` is
owner-only, so without this a household member would see no model indicator at
all — and the spec's failure table says the selector degrades to *static text*
for exactly that case. A model name is not sensitive; the API keys are, and
they are not exposed anywhere.

**Why it is not owner-only:** `/api/plugins` is, so a household member loading
the page would get a 403 and no greeting at all. This endpoint is for any
authenticated whitelisted user and returns only that caller's own name plus the
enabled skill list — the same information Wren already gives anyone who asks
"what plugins do you have".

- [ ] **Step 1: Write the failing test**

Append to `tests/test_webchat_plugins.py`:

```python
def test_me_returns_the_owner_name_setting():
    config.set_override("OWNER_NAME", "Erin")
    try:
        status, body = call("get", "/api/me", token=TOKEN_OWNER)
        assert status == 200
        assert body["name"] == "Erin"
    finally:
        config.clear_override("OWNER_NAME")


def test_me_omits_the_name_when_owner_name_is_unset():
    # Better no name than greeting somebody as "owner".
    status, body = call("get", "/api/me", token=TOKEN_OWNER)
    assert status == 200
    assert body["name"] is None


def test_me_is_not_owner_only():
    # The whole reason this endpoint exists: /api/plugins 403s for a household
    # member, so the greeting cannot come from there.
    status, body = call("get", "/api/me", token=TOKEN_OTHER)
    assert status == 200
    assert "skills" in body


def test_me_titlecases_a_contact_alias():
    from wren import contacts
    contacts.init_db()
    contacts.add("bob", USER_OTHER)
    status, body = call("get", "/api/me", token=TOKEN_OTHER)
    assert body["name"] == "Bob"


def test_me_requires_a_token():
    status, _ = call("get", "/api/me")
    assert status == 401


def test_me_reports_the_active_model_to_a_non_owner(monkeypatch):
    # /api/models is owner-only, so this is the only way a household member's
    # composer can show which model is answering.
    monkeypatch.setattr(config, "LLM_CHAIN", [
        {"name": "ollama", "base_url": "http://test", "api_key": "x", "model": "a-model"}])
    status, body = call("get", "/api/me", token=TOKEN_OTHER)
    assert status == 200
    assert body["model"] == "a-model"


def test_me_lists_only_enabled_skills():
    from wren.skills import notes_skill
    status, body = call("get", "/api/me", token=TOKEN_OWNER)
    assert "notes_skill" in body["skills"]
    registry.set_enabled(notes_skill, False)
    try:
        _, body = call("get", "/api/me", token=TOKEN_OWNER)
        assert "notes_skill" not in body["skills"]
    finally:
        registry.set_enabled(notes_skill, True)
```

The `tokens` fixture in that file monkeypatches `config.id_to_name`, so
`test_me_titlecases_a_contact_alias` must also make the alias visible — extend
that test to `monkeypatch.setattr(config, "id_to_name", lambda: {USER_OWNER: "ann", USER_OTHER: "bob"})`
if the fixture's mapping does not already provide it. Read the fixture first.

- [ ] **Step 2: Run it and confirm it fails**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_webchat_plugins.py -k "me_"`
Expected: FAIL — 404, the route does not exist

- [ ] **Step 3: Add OWNER_NAME to SETTABLE**

In `wren/config.py`, alongside the other settables:

```python
    # Shown in the web chat's greeting. The whitelist alias for the owner is
    # the literal string "owner", which is a placeholder, not a name -- so
    # rather than greeting somebody as "owner" the greeting omits the name
    # entirely until this is set. Contacts already carry a real alias.
    "OWNER_NAME": Setting(str.strip),
```

Add a module-level default so `_DEFAULTS` and `getattr` work:

```python
OWNER_NAME = os.environ.get("OWNER_NAME", "")
```

Place it near the other simple `os.environ.get` settings, before `_DEFAULTS`.

- [ ] **Step 4: Write the handler**

In `webchat.py`'s `register_routes`, beside the other handlers:

```python
    async def get_me(request):
        user_id = _auth(request)          # authn + whitelist, NOT owner-only
        from .. import registry

        if user_id == config.WHITELIST["owner"]:
            name = config.OWNER_NAME or None
        else:
            alias = config.id_to_name().get(user_id)
            name = alias.title() if alias else None
        return web.json_response({
            "name": name,
            "skills": [registry.skill_key(p) for p in registry.PLUGINS
                       if registry.is_enabled(p)],
            # Also here, not just in the owner-only /api/models, so a household
            # member's composer can show which model is answering as plain text.
            "model": config.LLM_CHAIN[0]["model"] if config.LLM_CHAIN else None,
        })
```

and register it:

```python
    app.router.add_get("/api/me", get_me)
```

- [ ] **Step 5: Run the tests**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_webchat_plugins.py`
Expected: PASS

- [ ] **Step 6: Full suite, then commit**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q`  (expect roughly 645)

```bash
git add wren/config.py wren/communication/webchat.py tests/test_webchat_plugins.py
git commit -m "feat(webchat): GET /api/me for the greeting name and enabled skills"
```

---

### Task 4: GET /api/models

**Files:**
- Modify: `wren/communication/webchat.py`
- Test: `tests/test_webchat_plugins.py` (append)

**Interfaces:**
- Consumes: `config.LLM_CHAIN`
- Produces: `GET /api/models` → `{"provider": str, "current": str, "models": [str, ...], "reason": str | None}`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_webchat_plugins.py`:

```python
def test_models_is_owner_only():
    status, _ = call("get", "/api/models", token=TOKEN_OTHER)
    assert status == 403


def test_models_reports_the_current_model(monkeypatch):
    async def fake_fetch(base_url, api_key):
        return ["a-model", "b-model"]
    monkeypatch.setattr(webchat, "_fetch_models", fake_fetch)
    monkeypatch.setattr(config, "LLM_CHAIN", [
        {"name": "ollama", "base_url": "http://test", "api_key": "x", "model": "a-model"}])
    status, body = call("get", "/api/models", token=TOKEN_OWNER)
    assert status == 200
    assert body["provider"] == "ollama"
    assert body["current"] == "a-model"
    assert body["models"] == ["a-model", "b-model"]


def test_models_degrades_to_an_empty_list_when_the_provider_is_down(monkeypatch):
    # The landing screen must not break because the LLM host is rebooting.
    async def boom(base_url, api_key):
        raise OSError("connection refused")
    monkeypatch.setattr(webchat, "_fetch_models", boom)
    monkeypatch.setattr(config, "LLM_CHAIN", [
        {"name": "ollama", "base_url": "http://test", "api_key": "x", "model": "a-model"}])
    status, body = call("get", "/api/models", token=TOKEN_OWNER)
    assert status == 200
    assert body["models"] == []
    assert body["reason"]
    assert body["current"] == "a-model"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_webchat_plugins.py -k models`
Expected: FAIL — 404

- [ ] **Step 3: Write the fetch helper and handler**

At module level in `webchat.py`:

```python
async def _fetch_models(base_url: str, api_key: str) -> list[str]:
    """The active provider's /v1/models, as plain ids.

    Proxied rather than fetched by the browser: once Wren is bound to
    127.0.0.1 the browser can only reach the reverse proxy, and this keeps the
    internal LLM endpoint out of the page source.
    """
    import aiohttp

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.json()
    return [m["id"] for m in body.get("data", []) if m.get("id")]
```

In `register_routes`:

```python
    async def get_models(request):
        _owner(request)
        if not config.LLM_CHAIN:
            return web.json_response(
                {"provider": None, "current": None, "models": [],
                 "reason": "no LLM provider is configured"})
        active = config.LLM_CHAIN[0]
        try:
            models = await _fetch_models(active["base_url"], active["api_key"])
            reason = None
        except Exception as e:
            # 200 with an empty list, never a 5xx: this feeds the landing
            # screen, which must render even when the LLM host is down. The
            # dropdown degrades to the current model as static text.
            logging.warning(f"could not list models from {active['name']}: {e}")
            models, reason = [], f"{active['name']} is not reachable right now"
        return web.json_response({
            "provider": active["name"],
            "current": active["model"],
            "models": models,
            "reason": reason,
        })

    app.router.add_get("/api/models", get_models)
```

Add `import logging` to `webchat.py` if it is not already imported.

- [ ] **Step 4: Run the tests, then the full suite**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_webchat_plugins.py`
Then: `/home/winter/work/Wren/venv/bin/python -m pytest -q`  (expect roughly 648)

- [ ] **Step 5: Commit**

```bash
git add wren/communication/webchat.py tests/test_webchat_plugins.py
git commit -m "feat(webchat): GET /api/models, degrading to text when the provider is down"
```

---

### Task 5: The landing screen

**Files:**
- Modify: `wren/communication/chat.html` (`<symbol>`, sidebar `<use>`, `showEmpty()`, composer, CSS)
- Test: `tests/test_chat_renderer.py` (append)

**Interfaces:**
- Consumes: `GET /api/me`, `GET /api/models`, the existing `api(path, opts = {})` helper

**Read first:** `api()` at `chat.html:300` — its signature is `api(path, opts = {})`,
a thin fetch wrapper where method and body live in `opts`. It is NOT
`api(method, path, body)`. Match it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chat_renderer.py`:

```python
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
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_chat_renderer.py -k "mark_is_defined or landing or pill_starter or model_selector"`
Expected: FAIL on all four

- [ ] **Step 3: Hoist the mark into a `<symbol>`**

Immediately after `<body>`, add:

```html
<!-- The Wren silhouette, defined once. Both the sidebar and the landing
     header <use> it, so the geometry from Brand/brand/mark-simple.svg is
     inlined once rather than per site. Regenerate with:
     cd Brand && python3 build.py && python3 render_pngs.py  -- then re-inline. -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="wren-mark" viewBox="0 0 128.0 128.0">
    <!-- CUT AND PASTE the <g>/<path> exactly as it stands inside the sidebar's
         current <svg class="sidemark"> (around chat.html:228). Do not retype
         the path data and do not re-derive it from Brand/ -- it is already
         correct in the file, and copying it by hand is how geometry drifts. -->
  </symbol>
</svg>
```

Then replace the sidebar's svg body with:

```html
      <svg class="sidemark" aria-hidden="true"><use href="#wren-mark"/></svg>
```

Run `pytest -q tests/test_chat_renderer.py` after this step alone — it is the
step most likely to disturb the file.

- [ ] **Step 4: Rewrite showEmpty()**

Replace `showEmpty()` at `chat.html:365`:

```js
const STARTERS = {
  shopping_skill: "add ",
  notes_skill:    "remember that ",
  reminder_skill: "remind me to ",
  pins_skill:     "pin: ",
  web_skill:      "look up ",
};
// contacts_skill is deliberately absent: it is owner-only administration, not
// something to invite a household member into. A module with no starter here
// simply renders no pill, which is also what a disabled skill does.

function greeting(name) {
  const h = new Date().getHours();   // the reader's clock, not the host's --
  // TIMEZONE exists so reminders fire correctly on the server, which is a
  // different question from what time it is where you are sitting.
  const part = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
  return name ? `Good ${part}, ${name}.` : `Good ${part}.`;
}

async function showEmpty() {
  const inner = $("#inner");
  inner.innerHTML = `<div id="landing">
      <svg class="landmark" aria-hidden="true"><use href="#wren-mark"/></svg>
      <h2 id="greet"></h2>
      <div id="pills"></div>
    </div>`;
  // Render the fallback greeting first so the screen is never blank or
  // waiting: /api/me only upgrades it with a name.
  $("#greet").textContent = greeting(null);
  let me;
  try {
    me = await api("/api/me");
  } catch (e) {
    return;                       // greeting stays; no pills
  }
  $("#greet").textContent = greeting(me.name);
  me.skills.filter(m => STARTERS[m]).forEach(m => {
    const b = document.createElement("button");
    b.className = "pill";
    b.textContent = m.replace("_skill", "").replace(/^./, c => c.toUpperCase());
    b.onclick = () => {
      $("#text").value = STARTERS[m];
      $("#text").focus();
      $("#text").setSelectionRange($("#text").value.length, $("#text").value.length);
    };
    $("#pills").appendChild(b);
  });
}
```

`showEmpty()` becomes `async`; check its call sites and `await` or fire-and-forget consistently with how they already work.

- [ ] **Step 5: Add the model selector to the composer**

In the `#composer` block at `chat.html:239`, before `#send`:

```html
        <select id="model" hidden></select>
        <span id="modeltext" hidden></span>
```

and in the JS, alongside the other post-sign-in initialisation:

```js
async function loadModels(fallbackModel) {
  let d;
  try {
    d = await api("/api/models");
  } catch (e) {
    // 403 for a non-owner, or the request failed. Fail closed to static text,
    // using the model /api/me already told us about.
    $("#modeltext").textContent = fallbackModel || "";
    $("#modeltext").hidden = !fallbackModel;
    return;
  }
  if (!d.models.length) {                 // owner, but the provider is down
    $("#modeltext").textContent = d.current || "";
    $("#modeltext").hidden = !d.current;
    return;
  }
  $("#model").innerHTML = "";
  d.models.forEach(m => {
    const o = document.createElement("option");
    o.value = m; o.textContent = m; o.selected = (m === d.current);
    $("#model").appendChild(o);
  });
  $("#model").hidden = false;
  $("#model").onchange = async () => {
    const key = d.provider.toUpperCase() + "_MODEL";
    await api("/api/settings", { method: "PATCH",
      body: JSON.stringify({ [key]: $("#model").value }) });
  };
}
```

- [ ] **Step 6: Add the CSS**

Beside the existing panel rules, using the file's custom properties:

```css
#landing { text-align: center; margin-top: 14vh; }
.landmark { width: 28px; height: 28px; display: block; margin: 0 auto 10px; }
#greet { font-size: 22px; font-weight: 500; margin: 0 0 4px; }
#pills { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 18px; }
.pill {
  border: 1px solid var(--line); border-radius: 999px; padding: 6px 14px;
  font-size: 13px; color: var(--muted);
}
.pill:hover { color: var(--text); border-color: var(--muted); }
#model {
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
  color: var(--muted); font: inherit; font-size: 13px; padding: 4px 6px;
}
#modeltext { color: var(--muted); font-size: 13px; align-self: center; }
```

- [ ] **Step 7: Tests, then the renderer canary**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q tests/test_chat_renderer.py tests/test_webchat.py`
Expected: PASS. If the renderer tests fail you have damaged the U+E000 region — restore and redo.

- [ ] **Step 8: Full suite, then commit**

Run: `/home/winter/work/Wren/venv/bin/python -m pytest -q`  (expect roughly 652)

```bash
git add wren/communication/chat.html tests/test_chat_renderer.py
git commit -m "feat(webchat): landing screen with greeting, skill pills and model selector"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md` (Web chat section, route table), `.env.example` (`OWNER_NAME`)

- [ ] **Step 1: Route table rows**

```markdown
| `GET /api/me` | — | `{name, skills}` — any whitelisted user |
| `GET /api/models` | — | `{provider, current, models}` — owner only |
```

- [ ] **Step 2: A short subsection under Web chat**

```markdown
### The landing screen

Opening the chat greets you by name, shows a pill per enabled skill that drops
a starter phrase into the composer, and — if you are the owner — lets you pick
which model answers.

Set your name with `OWNER_NAME` in the settings panel. Until it is set the
greeting is just the time of day: the whitelist alias for the owner is the
literal string `owner`, and being greeted as "owner" is worse than not being
greeted by name. Contacts are greeted by their own alias.

Changing the model rewrites that provider's `*_MODEL` setting and rebuilds the
provider chain, so it applies everywhere Wren answers — Discord and reminders
included — and survives a restart. The list comes from the provider itself; if
it is unreachable the selector falls back to showing the current model as text.
```

- [ ] **Step 3: `.env.example`**

```bash
# Shown in the web chat's greeting ("Good evening, Erin"). Also settable from
# the plugins panel. Unset means the greeting is just the time of day.
# OWNER_NAME=Erin
```

- [ ] **Step 4: Full suite, then commit**

```bash
git add README.md .env.example
git commit -m "docs: the landing screen, OWNER_NAME and runtime model selection"
```

---

## Notes for the implementer

- **Test counts are approximate.** A few either way is fine; a *red* suite is not.
- **`config` state leaks between tests.** `set_override` mutates module globals and `os.environ`; every test that sets one must clear it in a `finally`. Model tests also mutate `LLM_CHAIN` — restore it.
- **Do not add any `*_API_KEY` to `SETTABLE`.** `GET /api/plugins` returns the values of everything in it. If a task seems to need one, stop and raise it.
- **The landing screen must never wait on the network to render.** Greeting first, then upgrade. Every fetch fails closed to something readable.
