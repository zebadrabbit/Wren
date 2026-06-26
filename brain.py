import json
from datetime import datetime, timezone
from openai import OpenAI
import config

_client = OpenAI(base_url=config.LLM_BASE_URL, api_key="local")

_SYSTEM = """You are Wren, a private personal assistant. You are short, structured, and ready. No filler, no affirmations.

Today is {date}.

When classifying intent, respond ONLY with valid JSON matching this schema:
{{
  "intent": "save_note" | "recall_notes" | "send_to_person" | "chat",
  "content": "<extracted note or message content>",
  "tags": ["<tag1>", "<tag2>"],
  "person": "<name from whitelist or null>"
}}

Known contacts: {contacts}

Guidelines:
- save_note: user is capturing something for later (grocery item, plan, idea, reminder)
- recall_notes: user wants to retrieve or search past notes
- send_to_person: user wants to send a message or note to someone
- chat: anything else (questions, casual conversation)
- tags: 1-3 lowercase single-word tags relevant to the content
- person: only set for send_to_person intent, use the contact name as given
"""

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%A %B %d %Y %H:%M UTC")

def _contacts() -> str:
    return ", ".join(config.WHITELIST.keys())

def detect_intent(user_id: int, text: str) -> dict:
    system = _SYSTEM.format(date=_now(), contacts=_contacts())
    try:
        resp = _client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
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
    resp = _client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": f"You are Wren. Short, structured, ready. No filler."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()

def chat(text: str) -> str:
    resp = _client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": f"You are Wren, a personal assistant. Short, structured, ready. No filler. Today is {_now()}."},
            {"role": "user", "content": text},
        ],
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()
