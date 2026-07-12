import os
from unittest.mock import patch
import providers

def test_resolve_unknown_provider_returns_none():
    assert providers.resolve("not-a-real-provider") is None

def test_resolve_lmstudio_requires_base_url_and_model():
    with patch.dict(os.environ, {}, clear=True):
        assert providers.resolve("lmstudio") is None

def test_resolve_lmstudio_success():
    env = {
        "LMSTUDIO_BASE_URL": "http://192.168.1.70:30068/v1",
        "LMSTUDIO_MODEL": "gemma4-e4b-131k:latest",
    }
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("lmstudio")
    assert result == {
        "name": "lmstudio",
        "base_url": "http://192.168.1.70:30068/v1",
        "api_key": "not-needed",
        "model": "gemma4-e4b-131k:latest",
    }

def test_resolve_ollama_uses_default_base_url():
    with patch.dict(os.environ, {"OLLAMA_MODEL": "llama3"}, clear=True):
        result = providers.resolve("ollama")
    assert result["base_url"] == "http://localhost:11434/v1"
    assert result["model"] == "llama3"
    assert result["api_key"] == "not-needed"

def test_resolve_openai_requires_api_key():
    with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-4o-mini"}, clear=True):
        assert providers.resolve("openai") is None

def test_resolve_openai_success():
    env = {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4o-mini"}
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("openai")
    assert result == {
        "name": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test",
        "model": "gpt-4o-mini",
    }

def test_resolve_claude_success():
    env = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_MODEL": "claude-sonnet-5"}
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("claude")
    assert result["base_url"] == "https://api.anthropic.com/v1"
    assert result["api_key"] == "sk-ant-test"
    assert result["model"] == "claude-sonnet-5"

def test_resolve_openrouter_success():
    env = {"OPENROUTER_API_KEY": "sk-or-test", "OPENROUTER_MODEL": "meta-llama/llama-3"}
    with patch.dict(os.environ, env, clear=True):
        result = providers.resolve("openrouter")
    assert result["base_url"] == "https://openrouter.ai/api/v1"
    assert result["api_key"] == "sk-or-test"
    assert result["model"] == "meta-llama/llama-3"

def test_resolve_missing_model_returns_none():
    env = {"OPENAI_API_KEY": "sk-test"}
    with patch.dict(os.environ, env, clear=True):
        assert providers.resolve("openai") is None
