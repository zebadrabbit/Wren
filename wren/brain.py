import json
import logging
from datetime import datetime, timezone
from openai import OpenAI
from . import config

_SYSTEM = """You are Wren, a private personal assistant. You are short, structured, and ready. No filler, no affirmations.

Today is {date}.

When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "send_to_person" | {plugin_intents} | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>"
}}

Known contacts: {contacts}

Guidelines:
- send_to_person: user wants to send a message or note to someone
{plugin_guidelines}
- chat: anything else (questions, casual conversation)
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set when the intent is about contacting or sending something to someone else, use the contact name as given
"""

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%A %B %d %Y %H:%M UTC")

def _contacts() -> str:
    return ", ".join(config.WHITELIST.keys())

_plugin_intents: list[str] = []
_plugin_guidelines: str = ""

def register_plugins(intents: list[str], guidelines: str) -> None:
    global _plugin_intents, _plugin_guidelines
    _plugin_intents = intents
    _plugin_guidelines = guidelines

_clients: dict[str, OpenAI] = {}

def _get_client(provider: dict) -> OpenAI:
    if provider["name"] not in _clients:
        _clients[provider["name"]] = OpenAI(base_url=provider["base_url"], api_key=provider["api_key"])
    return _clients[provider["name"]]

def _complete(messages: list[dict], temperature: float) -> str:
    last_exc: Exception | None = None
    for provider in config.LLM_CHAIN:
        try:
            client = _get_client(provider)
            resp = client.chat.completions.create(
                model=provider["model"],
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"LLM provider '{provider['name']}' failed: {e}")
            last_exc = e
    raise last_exc

def detect_intent(user_id: int, text: str) -> dict:
    plugin_intent_enum = " | ".join(f'"{i}"' for i in _plugin_intents)
    system = _SYSTEM.format(
        date=_now(), contacts=_contacts(),
        plugin_intents=plugin_intent_enum, plugin_guidelines=_plugin_guidelines,
    )
    try:
        raw = _complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
        )
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception:
        return {"intent": "chat", "content": text, "tags": [], "person": None}

def recall(notes: list[dict], query: str) -> str:
    notes_text = "\n".join(
        f"- [{n['created_at'][:10]}] {n['content']} (tags: {n['tags']})"
        for n in notes
    )
    prompt = f"User's notes:\n{notes_text}\n\nUser asked: {query}\n\nAnswer directly using only what's in the notes."
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )

def chat(text: str) -> str:
    return _complete(
        [
            {"role": "system", "content": f"You are Wren, a personal assistant. Short, structured, ready. No filler. Today is {_now()}."},
            {"role": "user", "content": text},
        ],
        temperature=0.7,
    )

def expand(idea_text: str) -> str:
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler. Elaborate on the user's idea with concrete next steps or angles they might not have considered."},
            {"role": "user", "content": f"Idea: {idea_text}\n\nExpand on this."},
        ],
        temperature=0.5,
    )
