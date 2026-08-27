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
    "Kate called about the car",             # capitalised entity
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
