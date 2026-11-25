from wren.communication.http_plugin import _speakable, _SPEAK_MAX_CHARS

PARIS = """Paris is the capital and largest city of France.

**Population:** 2.04 million in an area of 105.4 km2 (40.7 sq mi) (Paris - Wikipedia).

1. Paris - Wikipedia — http:///go/v1/AbQqU1qeP7qeawYkxAFOiLAVlguXaiP4RK7
2. Paris - Wikipedia — https://en.wikipedia.org/wiki/Paris"""


def test_speakable_drops_sources_urls_citations_and_markdown():
    out = _speakable(PARIS)
    assert out == "Paris is the capital and largest city of France. Population: 2.04 million in an area of 105.4 km2."


def test_speakable_caps_a_long_answer_at_a_sentence():
    out = _speakable("One sentence here. " * 100)
    assert len(out) <= _SPEAK_MAX_CHARS + 40
    assert out.endswith("here. There is more in the chat.")


def test_speakable_leaves_a_short_answer_alone():
    assert _speakable("17:54 CST.") == "17:54 CST."
