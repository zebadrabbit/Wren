import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from wren.skills import memory_skill as memory_plugin
from wren.skills import memory_store as memory
from wren import config

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

def test_similarity_is_one_for_identical_text():
    assert memory_plugin.similarity("prefers oat milk", "prefers oat milk") == 1.0

def test_similarity_high_for_a_restatement():
    assert memory_plugin.similarity(
        "sister Kate lives in Denver", "sister Kate lives in Denver now") >= 0.7

def test_similarity_low_for_a_different_fact_in_the_same_shape():
    # the case a threshold must never merge: one word apart, opposite meaning
    assert memory_plugin.similarity("sister is Kate", "sister is Kim") < 0.5
    assert memory_plugin.similarity("likes coffee", "likes tea") < 0.5

def test_similarity_ignores_stopwords_and_case():
    assert memory_plugin.similarity("Owns A Kayak", "owns the kayak") == 1.0

def test_similarity_of_empty_text_is_zero():
    assert memory_plugin.similarity("", "owns a kayak") == 0.0

def test_remember_stores_a_new_fact():
    memory_plugin.remember(1, [{"category": "preference", "fact": "dislikes cilantro"}])
    assert [r["fact"] for r in memory.all_for(1)] == ["dislikes cilantro"]

def test_remember_bumps_a_near_duplicate_instead_of_storing_it():
    memory.save(1, "person", "sister Kate lives in Denver")
    memory_plugin.remember(1, [{"category": "person", "fact": "sister Kate lives in Denver now"}])
    rows = memory.all_for(1)
    assert len(rows) == 1
    assert rows[0]["mention_count"] == 2

def test_remember_keeps_a_genuinely_different_fact():
    memory.save(1, "person", "sister is Kate")
    memory_plugin.remember(1, [{"category": "person", "fact": "sister is Kim"}])
    assert len(memory.all_for(1)) == 2

def test_remember_dedups_within_one_batch():
    memory_plugin.remember(1, [{"category": "fact", "fact": "owns a kayak"},
                               {"category": "fact", "fact": "owns a kayak"}])
    rows = memory.all_for(1)
    assert len(rows) == 1
    assert rows[0]["mention_count"] == 2

def test_remember_respects_the_configured_threshold(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DEDUP_THRESHOLD", 0.99)
    memory.save(1, "person", "sister Kate lives in Denver")
    memory_plugin.remember(1, [{"category": "person", "fact": "sister Kate lives in Denver now"}])
    assert len(memory.all_for(1)) == 2      # 0.8 no longer clears the bar
