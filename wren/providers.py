import os

PROVIDER_DEFAULTS = {
    "lmstudio": {
        "default_base_url": None,
        "base_url_env": "LMSTUDIO_BASE_URL",
        "api_key_env": None,
        "model_env": "LMSTUDIO_MODEL",
    },
    "ollama": {
        "default_base_url": "http://localhost:11434/v1",
        "base_url_env": "OLLAMA_BASE_URL",
        "api_key_env": None,
        "model_env": "OLLAMA_MODEL",
    },
    "openai": {
        "default_base_url": "https://api.openai.com/v1",
        "base_url_env": "OPENAI_BASE_URL",
        "api_key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
    },
    "claude": {
        "default_base_url": "https://api.anthropic.com/v1",
        "base_url_env": "CLAUDE_BASE_URL",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model_env": "CLAUDE_MODEL",
    },
    "openrouter": {
        "default_base_url": "https://openrouter.ai/api/v1",
        "base_url_env": "OPENROUTER_BASE_URL",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
    },
}

def resolve(name: str) -> dict | None:
    spec = PROVIDER_DEFAULTS.get(name)
    if spec is None:
        return None

    base_url = os.environ.get(spec["base_url_env"]) or spec["default_base_url"]
    if not base_url:
        return None

    if spec["api_key_env"]:
        api_key = os.environ.get(spec["api_key_env"])
        if not api_key:
            return None
    else:
        api_key = "not-needed"

    model = os.environ.get(spec["model_env"])
    if not model:
        return None

    return {"name": name, "base_url": base_url, "api_key": api_key, "model": model}
