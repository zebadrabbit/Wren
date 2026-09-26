import os

import httpx

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


def ollama_chat(provider: dict, messages: list[dict], temperature: float,
                max_tokens: int, json_mode: bool) -> tuple[str, dict]:
    """One completion through Ollama's native /api/chat, thinking OFF.

    Ollama's OpenAI-compatible endpoint is what every other provider speaks,
    but on Ollama 0.34 it ignores the thinking switch: Gemma 4 then reasons
    for a few hundred characters before answering, the classifier's 200-token
    cap cuts the JSON off, and the turn falls back to chat (probe 2026-09-26:
    27/30 there, 30/30 here at the same speed as qwen). The native endpoint
    honours think=false and takes `format: json` directly, so the ollama
    provider uses it; the base URL's /v1 is the OpenAI-shaped prefix and is
    stripped. Images ride the same endpoint later (message["images"]).

    Returns (content, usage) with usage in the OpenAI names the caller sums.
    """
    body = {
        "model": provider["model"],
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if json_mode:
        body["format"] = "json"
    root = provider["base_url"].rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    resp = httpx.post(f"{root}/api/chat", json=body, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ollama: {data['error']}")
    usage = {"prompt_tokens": data.get("prompt_eval_count", 0),
             "completion_tokens": data.get("eval_count", 0)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return data["message"]["content"], usage
