import os, json, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio,ollama")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test-primary")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model-1")
os.environ.setdefault("OLLAMA_MODEL", "test-model-2")
os.environ.setdefault("TIMEZONE", "UTC")

import pytest
from unittest.mock import patch, MagicMock
from wren import brain, config
from wren import contacts

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # The ollama provider posts to a real server (providers.ollama_chat), and
    # the chain in this file names it. A test that wants that path patches
    # providers.httpx.post or providers.ollama_chat itself; nothing may hit
    # 192.168.x.x from the suite.
    from wren import providers as _providers
    def _refuse(*a, **k):
        raise AssertionError("test reached the network via providers.httpx.post")
    monkeypatch.setattr(_providers.httpx, "post", _refuse)

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    contacts.init_db()

def _mock_completion(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
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
    records = [{"source": "note", "when": "2026-06-25", "content": "buy eggs (tags: grocery)"}]
    with patch.object(brain, "_get_client", return_value=_client_returning("You need eggs.")):
        result = brain.recall(records, "what groceries do I need?")
    assert isinstance(result, str)
    assert len(result) > 0

def test_recall_prompt_labels_each_record_with_its_source_and_when():
    records = [{"source": "note", "when": "2026-06-25", "content": "dentist is dr patel"},
               {"source": "reminder", "when": "2026-10-01 09:00 UTC, every week", "content": "dentist cleaning"}]
    client = _client_returning("March.")
    with patch.object(brain, "_get_client", return_value=client):
        brain.recall(records, "when is the dentist")
    prompt = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
    assert "[note 2026-06-25] dentist is dr patel" in prompt
    assert "[reminder 2026-10-01 09:00 UTC, every week] dentist cleaning" in prompt

def test_chat_returns_string():
    with patch.object(brain, "_get_client", return_value=_client_returning("Hello.")):
        result = brain.chat("hey")
    assert isinstance(result, str)

def test_chat_falls_back_to_second_provider_on_failure():
    # the chain here is lmstudio then ollama; ollama has its own transport
    with patch.object(brain, "_get_client", return_value=_client_raising(RuntimeError("primary down"))), \
         patch.object(brain.providers, "ollama_chat", return_value=("fallback reply", {})):
        result = brain.chat("hey")
    assert result == "fallback reply"

def test_chat_raises_when_all_providers_fail():
    with patch.object(brain, "_get_client", return_value=_client_raising(RuntimeError("down"))), \
         patch.object(brain.providers, "ollama_chat", side_effect=RuntimeError("down too")):
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

def test_detect_intent_help():
    payload = json.dumps({
        "intent": "help",
        "content": "",
        "tags": [],
        "person": None
    })
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        result = brain.detect_intent(1, "show commands")
    assert result["intent"] == "help"

def test_detect_intent_caps_max_tokens():
    payload = json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None})
    client = _client_returning(payload)
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")
    assert client.chat.completions.create.call_args.kwargs["max_tokens"] == 200

def test_chat_caps_max_tokens():
    client = _client_returning("Hello.")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hey")
    assert client.chat.completions.create.call_args.kwargs["max_tokens"] == 400

def test_chat_without_history_matches_original_shape():
    client = _client_returning("Hello.")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hey")
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "hey"}

def test_chat_with_history_inserts_between_system_and_final_user_message():
    client = _client_returning("Hello.")
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("follow-up question", history=history)
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "earlier question"}
    assert messages[2] == {"role": "assistant", "content": "earlier reply"}
    assert messages[3] == {"role": "user", "content": "follow-up question"}

def test_chat_with_empty_history_list_matches_no_history_shape():
    client = _client_returning("Hello.")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hey", history=[])
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2

def test_detect_intent_without_history_matches_original_shape():
    client = _client_returning(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "hello"}

def test_detect_intent_with_history_inserts_between_system_and_final_user_message():
    client = _client_returning(json.dumps({"intent": "chat", "content": "follow-up", "tags": [], "person": None}))
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "follow-up", history=history)
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "earlier question"}
    assert messages[2] == {"role": "assistant", "content": "earlier reply"}
    assert messages[3] == {"role": "user", "content": "follow-up"}

def test_detect_intent_with_empty_history_list_matches_no_history_shape():
    client = _client_returning(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello", history=[])
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(messages) == 2

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

def test_detect_intent_prompt_includes_when_field():
    payload = json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None, "when": None})
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(payload)

    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")

    system_content = captured["messages"][0]["content"]
    assert '"when"' in system_content
    assert "set_reminder" in system_content

def test_complete_tracks_token_usage(monkeypatch):
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    monkeypatch.setattr(brain, "_last_provider", None)
    resp = _mock_completion("hi")
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hello")
    assert brain._token_usage == {"prompt": 10, "completion": 5, "total": 15}

def test_complete_handles_missing_usage_gracefully(monkeypatch):
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    monkeypatch.setattr(brain, "_last_provider", None)
    with patch.object(brain, "_get_client", return_value=_client_returning("hi")):
        brain.chat("hello")
    assert brain._token_usage == {"prompt": 0, "completion": 0, "total": 0}

def test_complete_updates_last_provider_on_success(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    with patch.object(brain, "_get_client", return_value=_client_returning("hi")):
        brain.chat("hello")
    assert brain._last_provider["name"] == "lmstudio"

def test_complete_updates_last_provider_after_fallback(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    with patch.object(brain, "_get_client", return_value=_client_raising(RuntimeError("primary down"))), \
         patch.object(brain.providers, "ollama_chat", return_value=("fallback reply", {})):
        brain.chat("hey")
    assert brain._last_provider["name"] == "ollama"

def test_status_before_any_call_uses_configured_primary(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    result = brain.status()
    assert result["provider"]["name"] == config.LLM_CHAIN[0]["name"]
    assert result["tokens"] == {"prompt": 0, "completion": 0, "total": 0}

def test_status_after_call_uses_last_provider(monkeypatch):
    monkeypatch.setattr(brain, "_last_provider", None)
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    with patch.object(brain, "_get_client", return_value=_client_returning("hi")):
        brain.chat("hello")
    result = brain.status()
    assert result["provider"]["name"] == "lmstudio"

def test_detect_intent_prompt_includes_status_intent():
    payload = json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None})
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(payload)

    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hello")

    system_content = captured["messages"][0]["content"]
    assert '"status"' in system_content

def test_now_uses_utc_by_default():
    assert "UTC" in brain._now()

def test_now_reflects_configured_timezone(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/Chicago")
    result = brain._now()
    assert "UTC" not in result

def test_summarize_web_from_results_returns_string():
    results = [{"title": "T", "snippet": "it is sunny", "url": "http://a"}]
    with patch.object(brain, "_get_client", return_value=_client_returning("It's sunny.")):
        out = brain.summarize_web("weather", results)
    assert isinstance(out, str) and len(out) > 0

def test_summarize_web_from_markdown_returns_string():
    with patch.object(brain, "_get_client", return_value=_client_returning("Summary.")):
        out = brain.summarize_web("read it", "# Article\nlong body text")
    assert isinstance(out, str) and len(out) > 0

def test_summarize_web_puts_content_in_prompt():
    results = [{"title": "Port news", "snippet": "strike ended", "url": "http://a"}]
    captured = {}
    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion("ok")
    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.summarize_web("port strike", results)
    user_msg = captured["messages"][1]["content"]
    assert "strike ended" in user_msg
    assert "port strike" in user_msg

def test_chat_guideline_distinguishes_conversation_meta_questions_from_recall():
    # Regression: "do you retain context from the previous message?" was
    # misclassified as recall_notes (lexical overlap: "previous"/"past",
    # "context"/"notes") instead of chat, since chat's guideline had no
    # positive signal for meta-questions about Wren's own memory.
    captured = {}
    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))
    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "do you remember what I said")
    system_content = captured["messages"][0]["content"]
    # Tightened: memory_skill's recall_memories guideline also mentions
    # "remembers", so a loose "memory" or "remember" substring check now
    # passes even with the chat carve-out deleted. Assert on the carve-out's
    # own wording instead -- deleting it must fail this test.
    assert "do you remember what I said" in system_content


def test_pleasantries_are_named_as_chat_in_the_prompt():
    """A guard on prompt wording, because the behaviour it protects can only be
    measured against a live model.

    "Wren" appears in the help, status and list_plugins guidelines and nowhere
    else, so a bare pleasantry that names Wren has no positive example to land
    on and a small model pulls it to one of those: qwen2.5:7b classified "thank
    you wren" as `status` three times out of three, and "thanks" as `help`.
    Naming pleasantries in the chat guideline fixed all nine phrases tested
    with no change to the nine real intents. Telegram feels this worst — its
    `history` is None by protocol, and history is what otherwise supplies the
    missing context.
    """
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _mock_completion(json.dumps({"intent": "chat", "content": "hi", "tags": [], "person": None}))

    client = MagicMock()
    client.chat.completions.create.side_effect = fake_create
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "thank you wren")

    guidelines = captured["messages"][0]["content"]
    chat_line = guidelines.split("- chat:")[1].split("\n- ")[0]
    assert "pleasantry" in chat_line
    assert "thank you wren" in chat_line


def test_detect_intent_asks_the_provider_for_json():
    # without this a small model answers the *user* in prose instead of
    # classifying, and the request is silently dropped as "chat"
    payload = json.dumps({"intent": "set_reminder", "content": "x", "tags": [], "when": None})
    client = _client_returning(payload)
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "remind me in 3 minutes")
    assert client.chat.completions.create.call_args.kwargs["response_format"] == {"type": "json_object"}


def test_detect_intent_retries_without_json_mode_if_provider_rejects_it():
    payload = json.dumps({"intent": "chat", "content": "hi", "tags": []})
    client = MagicMock()
    client.chat.completions.create.side_effect = [TypeError("unexpected response_format"),
                                                  _mock_completion(payload)]
    with patch.object(brain, "_get_client", return_value=client):
        result = brain.detect_intent(1, "hi")
    assert result["intent"] == "chat"
    assert "response_format" not in client.chat.completions.create.call_args.kwargs

def test_extract_facts_returns_raw_model_output():
    payload = '{"facts": [{"category": "preference", "fact": "dislikes cilantro"}]}'
    with patch.object(brain, "_get_client", return_value=_client_returning(payload)):
        assert brain.extract_facts("I can't stand cilantro") == payload

def test_extract_facts_asks_for_json_and_sends_no_history():
    client = _client_returning('{"facts": []}')
    with patch.object(brain, "_get_client", return_value=client):
        brain.extract_facts("thanks Wren")
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    # one system prompt + the message under test, nothing else: conversation
    # history is what taught the classifier to answer in prose instead of JSON
    assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]

def test_extract_facts_prompt_shows_negative_examples():
    # small models overfire; the empty answers are the important half
    assert brain._EXTRACT.count('{"facts": []}') >= 2


# --- memory injection ---------------------------------------------------

def test_chat_injects_memories_into_the_system_prompt():
    client = _client_returning("sure")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("what should I cook", memories=["dislikes cilantro"])
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "dislikes cilantro" in system

def test_chat_without_memories_is_unchanged():
    client = _client_returning("sure")
    with patch.object(brain, "_get_client", return_value=client):
        brain.chat("hello", memories=[])
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "know about" not in system

def test_detect_intent_never_sees_memories():
    # the 2026-08-18 regression, pinned: nothing may grow this prompt
    client = _client_returning('{"intent": "chat", "content": "hi", "tags": []}')
    with patch.object(brain, "_get_client", return_value=client):
        brain.detect_intent(1, "hi")
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "know about" not in system


# --- Ollama speaks its native endpoint, thinking off (2026-09-26) -----------
# Gemma 4 reasons for a few hundred characters before every answer, and
# Ollama 0.34 ignores the thinking switch on its OpenAI-compatible endpoint;
# the classifier's 200-token cap then cuts the JSON off. The native /api/chat
# honours think=false, so the ollama provider uses it. Everything else keeps
# the OpenAI-shaped path.

from wren import providers

_OLLAMA = {"name": "ollama", "base_url": "http://h:1/v1", "api_key": "not-needed", "model": "gemma4"}

class _FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status
    def json(self):
        return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

def _capture_post(body):
    calls = []
    def post(url, json=None, timeout=None):
        calls.append((url, json))
        return _FakeResp(body)
    return calls, post

def test_ollama_uses_the_native_endpoint_with_thinking_off(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [_OLLAMA])
    monkeypatch.setattr(brain, "_token_usage", {"prompt": 0, "completion": 0, "total": 0})
    calls, post = _capture_post({"message": {"content": '{"intent": "help"}'},
                                 "prompt_eval_count": 40, "eval_count": 6})
    monkeypatch.setattr(providers.httpx, "post", post)
    with patch.object(brain, "_get_client", side_effect=AssertionError("must not use the OpenAI client")):
        out = brain._complete([{"role": "user", "content": "help"}], temperature=0.1, max_tokens=200, json_mode=True)
    assert out == '{"intent": "help"}'
    url, body = calls[0]
    assert url == "http://h:1/api/chat"
    assert body["model"] == "gemma4" and body["stream"] is False and body["think"] is False
    assert body["format"] == "json"
    assert body["options"] == {"temperature": 0.1, "num_predict": 200}
    assert body["messages"] == [{"role": "user", "content": "help"}]
    assert brain._token_usage == {"prompt": 40, "completion": 6, "total": 46}
    assert brain._last_provider["name"] == "ollama"

def test_ollama_prose_calls_do_not_force_json(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [_OLLAMA])
    calls, post = _capture_post({"message": {"content": "Hello."}})
    monkeypatch.setattr(providers.httpx, "post", post)
    assert brain._complete([{"role": "user", "content": "hi"}], temperature=0.7) == "Hello."
    assert "format" not in calls[0][1]

def test_ollama_base_url_without_v1_still_reaches_api_chat(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [dict(_OLLAMA, base_url="http://h:1")])
    calls, post = _capture_post({"message": {"content": "x"}})
    monkeypatch.setattr(providers.httpx, "post", post)
    brain._complete([{"role": "user", "content": "hi"}], temperature=0.7)
    assert calls[0][0] == "http://h:1/api/chat"

def test_ollama_failure_falls_through_to_the_next_provider(monkeypatch):
    lmstudio = {"name": "lmstudio", "base_url": "http://l:1/v1", "api_key": "not-needed", "model": "qwen"}
    monkeypatch.setattr(config, "LLM_CHAIN", [_OLLAMA, lmstudio])
    def post(url, json=None, timeout=None):
        return _FakeResp({"error": "model not found"}, status=404)
    monkeypatch.setattr(providers.httpx, "post", post)
    with patch.object(brain, "_get_client", return_value=_client_returning("from lmstudio")):
        assert brain._complete([{"role": "user", "content": "hi"}], temperature=0.7) == "from lmstudio"
    assert brain._last_provider["name"] == "lmstudio"


# --- vision: pictures go to the model on the native transport --------------

from wren.channel import Inbound

_JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 8
_PDF = b"%PDF-1.4 stub"

def _files():
    return [Inbound(filename="a.jpg", mime="image/jpeg", data=_JPEG),
            Inbound(filename="doc.pdf", mime="application/pdf", data=_PDF)]

def test_describe_sends_only_the_images_base64_on_the_user_turn(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [_OLLAMA])
    seen = {}
    def fake(provider, messages, temperature, max_tokens, json_mode):
        seen.update(provider=provider, messages=messages, json_mode=json_mode)
        return "A red mug on a desk.", {}
    monkeypatch.setattr(providers, "ollama_chat", fake)
    out = brain.describe(_files(), "what's in this?")
    assert out == "A red mug on a desk."
    assert seen["provider"]["name"] == "ollama" and seen["json_mode"] is False
    user = seen["messages"][-1]
    assert user["role"] == "user" and user["content"] == "what's in this?"
    import base64
    assert user["images"] == [base64.b64encode(_JPEG).decode()]     # the PDF is not sent

def test_describe_without_an_ollama_provider_says_so(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [{"name": "lmstudio", "base_url": "http://l/v1", "api_key": "x", "model": "q"}])
    assert brain.describe(_files(), "what's this") == "This model can't see pictures."

def test_describe_with_no_image_among_the_files_says_pictures_only(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [_OLLAMA])
    assert brain.describe([Inbound(filename="doc.pdf", mime="application/pdf", data=_PDF)], "summarise") \
        == "I can read pictures, not PDFs yet."

def test_items_in_parses_the_json_list(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [_OLLAMA])
    seen = {}
    def fake(provider, messages, temperature, max_tokens, json_mode):
        seen.update(json_mode=json_mode, images=messages[-1].get("images"))
        return '{"items": ["milk", "eggs", "bread"]}', {}
    monkeypatch.setattr(providers, "ollama_chat", fake)
    assert brain.items_in(_files(), "add these to the list") == ["milk", "eggs", "bread"]
    assert seen["json_mode"] is True and len(seen["images"]) == 1

def test_items_in_returns_nothing_for_garbage_or_no_images(monkeypatch):
    monkeypatch.setattr(config, "LLM_CHAIN", [_OLLAMA])
    monkeypatch.setattr(providers, "ollama_chat", lambda *a, **k: ("not json", {}))
    assert brain.items_in(_files(), "") == []
    assert brain.items_in([], "") == []
