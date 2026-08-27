import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from wren.skills import memory_skill as memory_plugin
from wren.skills import memory_store as memory

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    memory.init_db()

@pytest.mark.parametrize("text", [
    "I hate cilantro",                       # first person
    "my sister Kate lives in Denver",        # first person + entity
    "we're moving to Denver",                # first person plural
    "the standup is tomorrow at 9am",        # date/time
    "the deploy pipeline for that service keeps failing whenever the cache is "
    "cold and nobody has worked out why yet",  # over the length threshold
])
def test_gate_fires(text):
    assert memory_plugin.should_extract(text) is True

@pytest.mark.parametrize("text", ["thanks", "ok", "", "   ", "what's the weather",
                                  "sounds good", "no worries"])
def test_gate_skips(text):
    assert memory_plugin.should_extract(text) is False

def test_gate_ignores_a_leading_capital():
    # every sentence starts with one; only a capital *inside* the sentence is
    # evidence of a name
    assert memory_plugin.should_extract("Sounds fine") is False

def test_parse_extracts_valid_facts():
    raw = '{"facts": [{"category": "preference", "fact": "dislikes cilantro"}]}'
    assert memory_plugin.parse_facts(raw) == [
        {"category": "preference", "fact": "dislikes cilantro"}]

def test_parse_empty_list_is_none():
    assert memory_plugin.parse_facts('{"facts": []}') == []

@pytest.mark.parametrize("raw", [
    "",
    "NONE",
    "Sure! Here's what I found:",           # the model answering instead of classifying
    '{"facts": "dislikes cilantro"}',       # right key, wrong type
    '{"nope": []}',                         # right shape, wrong key
    '{"facts": [{"category": "preference"',  # truncated at max_tokens
])
def test_parse_malformed_is_none(raw):
    assert memory_plugin.parse_facts(raw) == []

def test_parse_drops_unknown_categories():
    raw = ('{"facts": [{"category": "vibe", "fact": "seems tired"},'
           ' {"category": "fact", "fact": "runs a home server"}]}')
    assert memory_plugin.parse_facts(raw) == [
        {"category": "fact", "fact": "runs a home server"}]

def test_parse_drops_empty_fact_text():
    assert memory_plugin.parse_facts('{"facts": [{"category": "fact", "fact": "  "}]}') == []

def test_parse_tolerates_a_code_fence():
    raw = '```json\n{"facts": [{"category": "fact", "fact": "owns a kayak"}]}\n```'
    assert memory_plugin.parse_facts(raw) == [{"category": "fact", "fact": "owns a kayak"}]
