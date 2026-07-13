import os, json, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("HUSBAND_ID", "2")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio,ollama")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test-primary")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model-1")
os.environ.setdefault("OLLAMA_MODEL", "test-model-2")

from unittest.mock import patch, MagicMock
from wren import brain

def _mock_completion(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp

def _client_returning(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_completion(content)
    return client

def _client_raising(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.side_effect = exc
    return client

def test_detect_intent_save():
    payload = json.dumps({
        "intent": "save_note",
        "content": "buy milk",
        "tags": ["grocery"],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "remind me to buy milk")
    assert result["intent"] == "save_note"
    assert result["tags"] == ["grocery"]

def test_detect_intent_bad_json_falls_back_to_chat():
    with patch.object(brain, "_get_client", return_value=_client_returning("not json")):
        result = brain.detect_intent(1, "hello")
    assert result["intent"] == "chat"

def test_detect_intent_all_providers_fail_falls_back_to_chat():
    with patch.object(brain, "_get_client", return_value=_client_raising(RuntimeError("down"))):
        result = brain.detect_intent(1, "hello")
    assert result["intent"] == "chat"

def test_recall_returns_string():
    sample_notes = [{"content": "buy eggs", "tags": "grocery", "created_at": "2026-06-25T10:00:00+00:00"}]
    with patch.object(brain, "_get_client", return_value=_client_returning("You need eggs.")):
        result = brain.recall(sample_notes, "what groceries do I need?")
    assert isinstance(result, str)
    assert len(result) > 0

def test_chat_returns_string():
    with patch.object(brain, "_get_client", return_value=_client_returning("Hello.")):
        result = brain.chat("hey")
    assert isinstance(result, str)

def test_chat_falls_back_to_second_provider_on_failure():
    primary = _client_raising(RuntimeError("primary down"))
    secondary = _client_returning("fallback reply")

    def fake_get_client(provider):
        return primary if provider["name"] == "lmstudio" else secondary

    with patch.object(brain, "_get_client", side_effect=fake_get_client):
        result = brain.chat("hey")
    assert result == "fallback reply"

def test_chat_raises_when_all_providers_fail():
    with patch.object(brain, "_get_client", return_value=_client_raising(RuntimeError("down"))):
        with pytest.raises(RuntimeError):
            brain.chat("hey")

def test_detect_intent_add_shopping_item():
    payload = json.dumps({
        "intent": "add_shopping_item",
        "content": "potatoes",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "add potatoes to shopping")
    assert result["intent"] == "add_shopping_item"
    assert result["content"] == "potatoes"

def test_detect_intent_remove_shopping_item():
    payload = json.dumps({
        "intent": "remove_shopping_item",
        "content": "potatoes",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "got the potatoes")
    assert result["intent"] == "remove_shopping_item"
    assert result["content"] == "potatoes"

def test_detect_intent_recall_shopping():
    payload = json.dumps({
        "intent": "recall_shopping",
        "content": "",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "what's on the shopping list")
    assert result["intent"] == "recall_shopping"

def test_detect_intent_send_shopping_list():
    payload = json.dumps({
        "intent": "send_shopping_list",
        "content": "",
        "tags": [],
        "person": "husband"
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "send shopping to husband")
    assert result["intent"] == "send_shopping_list"
    assert result["person"] == "husband"

def test_detect_intent_save_idea():
    payload = json.dumps({
        "intent": "save_idea",
        "content": "build a treehouse",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "remember this idea: build a treehouse")
    assert result["intent"] == "save_idea"
    assert result["content"] == "build a treehouse"

def test_detect_intent_recall_ideas():
    payload = json.dumps({
        "intent": "recall_ideas",
        "content": "",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "what ideas have I saved")
    assert result["intent"] == "recall_ideas"

def test_detect_intent_discard_idea():
    payload = json.dumps({
        "intent": "discard_idea",
        "content": "treehouse",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "discard the treehouse idea")
    assert result["intent"] == "discard_idea"
    assert result["content"] == "treehouse"

def test_detect_intent_expand_idea():
    payload = json.dumps({
        "intent": "expand_idea",
        "content": "treehouse",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "expand on the treehouse idea")
    assert result["intent"] == "expand_idea"
    assert result["content"] == "treehouse"

def test_expand_returns_string():
    with patch.object(brain, "_get_client", return_value=_client_returning("Here's how to build it...")):
        result = brain.expand("build a treehouse")
    assert isinstance(result, str)
    assert len(result) > 0

def test_register_plugins_included_in_prompt():
    brain.register_plugins(["custom_intent"], "- custom_intent: does a custom thing")
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))

    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")

    system_content = captured["messages"][0]["content"]
    assert "custom_intent" in system_content
    assert "does a custom thing" in system_content
    brain.register_plugins([], "")  # reset so later tests aren't affected
