import base64
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import OpenAI
from . import config
from . import providers
from . import filetypes

_SYSTEM = """You are Wren, a private personal assistant. You are short, structured, and ready. No filler, no affirmations.

Answer directly. Do not show reasoning, planning, or a thinking process —
no <think> tags, no step-by-step deliberation, just the final output. /no_think

Today is {date}.

When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "send_to_person" | "help" | "status" | "list_plugins" | {plugin_intents} | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>",
  "when": "<ISO 8601 datetime in the local timezone shown in 'Today is' above, for set_reminder, or null>"
}}

Known contacts: {contacts}

Guidelines:
- send_to_person: user wants to send a message or note to someone
- help: user wants to know what Wren can do, asks for help, or asks to see available commands
- status: user wants to know Wren's operational status — active LLM backend/model/endpoint, uptime, token usage
- list_plugins: user wants to know what plugins/capabilities Wren currently has active (e.g. "what plugins do you have", "what's active")
{plugin_guidelines}
- chat: anything else (questions, casual conversation, and every greeting,
  thanks or other pleasantry — "hi", "thanks", "thank you wren", "good
  morning wren" are chat, and naming Wren does not make them otherwise),
  including meta-questions about Wren's own conversational memory/capabilities (e.g. "do you remember what I said", "do you have context from before") — these are NOT recall_notes, they're about this live conversation, not saved notes
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set when the intent is about contacting or sending something to someone else, use the contact name as given
- when: only set for set_reminder — an absolute ISO 8601 datetime in the SAME timezone as "today" above (do not convert to UTC yourself), computed from the user's relative/absolute time phrase; null otherwise
"""

def _now() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%A %B %d %Y %H:%M %Z")

def _contacts() -> str:
    return ", ".join(config.whitelist().keys())

_plugin_intents: list[str] = []
_plugin_guidelines: str = ""

def register_plugins(intents: list[str], guidelines: str) -> None:
    global _plugin_intents, _plugin_guidelines
    _plugin_intents = intents
    _plugin_guidelines = guidelines

_last_provider: dict | None = None
_token_usage = {"prompt": 0, "completion": 0, "total": 0}

_clients: dict[str, OpenAI] = {}

def _get_client(provider: dict) -> OpenAI:
    if provider["name"] not in _clients:
        _clients[provider["name"]] = OpenAI(base_url=provider["base_url"], api_key=provider["api_key"])
    return _clients[provider["name"]]

def _complete(messages: list[dict], temperature: float, max_tokens: int = 400,
              json_mode: bool = False) -> str:
    """json_mode is the intent classifier's guard rail, and it is load-bearing.

    Without it a small model reads the prose in `history` as "assistant turns
    look like this" and answers the user in prose instead of emitting JSON —
    it role-plays the reply it thinks Wren would give. detect_intent can only
    read that as `chat`, so the request is silently dropped. Every provider
    here is OpenAI-shaped, but not all of them accept response_format, hence
    the plain retry before falling through to the next provider.
    """
    global _last_provider
    last_exc: Exception | None = None
    attempts = [{"response_format": {"type": "json_object"}}, {}] if json_mode else [{}]
    for provider in config.LLM_CHAIN:
        if provider["name"] == "ollama":
            # native endpoint, thinking off -- see providers.ollama_chat for why
            # the OpenAI-shaped path below is not enough for Ollama
            try:
                content, usage = providers.ollama_chat(provider, messages, temperature, max_tokens, json_mode)
                _count(usage)
                _last_provider = provider
                return content.strip()
            except Exception as e:
                logging.warning(f"LLM provider 'ollama' failed: {e}")
                last_exc = e
            continue
        for extra in attempts:
            try:
                client = _get_client(provider)
                resp = client.chat.completions.create(
                    model=provider["model"],
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **extra,
                )
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    _count({"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                            "total_tokens": usage.total_tokens})
                _last_provider = provider
                return resp.choices[0].message.content.strip()
            except Exception as e:
                logging.warning(f"LLM provider '{provider['name']}' failed: {e}")
                last_exc = e
    raise last_exc

def _count(usage: dict) -> None:
    _token_usage["prompt"] += usage.get("prompt_tokens") or 0
    _token_usage["completion"] += usage.get("completion_tokens") or 0
    _token_usage["total"] += usage.get("total_tokens") or 0

def status() -> dict:
    return {
        "provider": _last_provider or config.LLM_CHAIN[0],
        "tokens": dict(_token_usage),
    }

def detect_intent(user_id: int, text: str, history: list[dict] | None = None) -> dict:
    plugin_intent_enum = " | ".join(f'"{i}"' for i in _plugin_intents)
    system = _SYSTEM.format(
        date=_now(), contacts=_contacts(),
        plugin_intents=plugin_intent_enum, plugin_guidelines=_plugin_guidelines,
    )
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    raw = ""
    try:
        raw = _complete(messages, temperature=0.1, max_tokens=200, json_mode=True)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        # Loud, because downstream this is indistinguishable from a real "chat":
        # the user gets a conversational answer and no error anywhere, so a
        # dropped reminder leaves no trace at all. Cost a reminder on 2026-08-18.
        logging.warning(f"intent detection failed, falling back to chat: {e} (raw: {raw[:200]!r})")
        return {"intent": "chat", "content": text, "tags": [], "person": None}

_EXTRACT = """You extract durable facts about a user from a single message.

A durable fact is still true next week: a preference, a person in their life,
a project they work on, or a stable fact about them. Moods, questions,
commands, plans already handled elsewhere, and small talk are NOT durable
facts. When in doubt, extract nothing.

Reply ONLY with JSON in this exact shape:
{"facts": [{"category": "preference|person|project|fact", "fact": "<short third-person statement>"}]}

Message: "I can't stand cilantro, it ruins everything"
{"facts": [{"category": "preference", "fact": "dislikes cilantro"}]}

Message: "what's the weather looking like tomorrow"
{"facts": []}

Message: "thanks Wren, you're the best"
{"facts": []}

Message: "my sister Kate just moved to Denver"
{"facts": [{"category": "person", "fact": "sister Kate lives in Denver"}]}
"""

def extract_facts(text: str) -> str:
    """One message in, raw JSON out. No history, by design — see detect_intent.

    temperature 0: this is a classification, and a creative extractor invents
    facts about people, which is the worst failure this feature can have.
    """
    return _complete(
        [{"role": "system", "content": _EXTRACT},
         {"role": "user", "content": f'Message: "{text}"'}],
        temperature=0.0,
        max_tokens=200,
        json_mode=True,
    )

def recall(notes: list[dict], query: str) -> str:
    notes_text = "\n".join(
        f"- [{n['created_at'][:10]}] {n['content']} (tags: {n['tags']})"
        for n in notes
    )
    prompt = f"User's notes:\n{notes_text}\n\nUser asked: {query}\n\nAnswer directly using only what's in the notes."
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler. Answer directly, no reasoning or thinking process shown. /no_think"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=400,
    )

def chat(text: str, history: list[dict] | None = None,
         memories: list[str] | None = None) -> str:
    system = f"You are Wren, a personal assistant. Short, structured, ready. No filler. Answer directly, no reasoning or thinking process shown. /no_think Today is {_now()}."
    if memories:
        # Plain bullets, and an explicit licence to ignore them: without it a
        # small model treats anything in its prompt as something it was just
        # asked about and works the facts into the reply whether they fit or not.
        system += ("\n\nWhat you already know about this person:\n"
                   + "\n".join(f"- {m}" for m in memories)
                   + "\nUse these only if they are relevant. Do not list them back.")
    messages = [
        {"role": "system", "content": system},
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    return _complete(messages, temperature=0.7, max_tokens=400)

def expand(idea_text: str) -> str:
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler. Answer directly, no reasoning or thinking process shown. /no_think Elaborate on the user's idea with concrete next steps or angles they might not have considered."},
            {"role": "user", "content": f"Idea: {idea_text}\n\nExpand on this."},
        ],
        temperature=0.5,
        max_tokens=600,
    )

def summarize_web(query: str, content) -> str:
    if isinstance(content, str):
        context = content
    else:
        context = "\n".join(
            f"- {r['title']}: {r['snippet']} ({r['url']})" for r in content
        )
    prompt = (
        f"Web results for '{query}':\n{context}\n\n"
        "Answer the user's query using only these results. Cite sources by "
        "title when useful. If the results don't answer it, say so plainly."
    )
    return _complete(
        [
            {"role": "system", "content": "You are Wren. Short, structured, ready. No filler. Answer directly using ONLY the provided web content — do not invent facts. No reasoning shown. /no_think"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=500,
    )


# ---------------------------------------------------------------- vision
# Pictures go to the model on the native Ollama transport only: it takes
# images per message, and Gemma 4 reads them. The OpenAI-shaped providers are
# not asked -- a text-only model handed an image is a 400 at best.

_SEE = """You are Wren, a private household assistant. Answer the question about the picture(s) in one or two plain sentences. If there is no question, say what the picture shows. No preamble."""

_ITEMS = """List the items visible in the picture(s) that someone would put on a list: products on a receipt, food in a fridge, lines of a handwritten list. Short generic names, lowercase, one entry each, no quantities or prices. Reply with JSON only: {"items": ["milk", "eggs"]}. Empty list if there is nothing of the kind."""


def _images(files) -> list[str]:
    return [base64.b64encode(f.data).decode() for f in files if f.mime in filetypes.IMAGE_MIMES]


def _see(files, system: str, user: str, json_mode: bool) -> str | None:
    """One call with the images on the user turn, or None when it cannot be
    made here (no ollama provider, no image among the files)."""
    provider = next((p for p in config.LLM_CHAIN if p["name"] == "ollama"), None)
    images = _images(files)
    if provider is None or not images:
        return None
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user, "images": images}]
    content, usage = providers.ollama_chat(provider, messages, temperature=0.2,
                                           max_tokens=300, json_mode=json_mode)
    _count(usage)
    return content.strip()


def describe(files, question: str) -> str:
    """Answer a question about the pictures, as prose."""
    if not _images(files):
        return "I can read pictures, not PDFs yet." if files else "There's no picture to look at."
    out = _see(files, _SEE, question or "What is in this picture?", json_mode=False)
    return out if out is not None else "This model can't see pictures."


def items_in(files, hint: str) -> list[str]:
    """The list-worthy items visible in the pictures; empty when unreadable."""
    raw = _see(files, _ITEMS, hint or "List the items.", json_mode=True)
    if not raw:
        return []
    try:
        items = json.loads(raw).get("items", [])
    except (ValueError, AttributeError):
        return []
    return [str(i).strip() for i in items if str(i).strip()]
