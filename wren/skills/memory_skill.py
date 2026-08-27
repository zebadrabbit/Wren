import asyncio
import json
import logging
import re

from .. import brain
from .. import config
from .. import flourish
from . import memory_store as memories
from ..channel import Ctx

INTENTS = ["recall_memories", "forget_memory"]
PLUGIN_NAME = "Memory"

CATEGORIES = ("preference", "person", "project", "fact")

# Module constants, not settings: every knob in config.SETTABLE is one more
# value an owner has to understand before they can change anything else.
# Promote these only if tuning proves they need it.
_MIN_TOKENS = 12
_CORE_MENTIONS = 3
_INJECT_FLOOR = 0.1

_FIRST_PERSON = re.compile(r"\b(i|i'm|im|i've|my|mine|me|we|we're|our|us)\b", re.I)
_WHEN = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|next\s+(week|month|year)|\d{1,2}\s?(am|pm)|\d{4}-\d{2}-\d{2})\b",
    re.I,
)

def _has_entity(text: str) -> bool:
    # words[1:]: the first word of a sentence is capitalised whatever it is,
    # so only a capital further in is evidence of a name.
    return any(w[:1].isupper() and w[1:2].islower() and len(w) > 2
               for w in text.split()[1:])

def should_extract(text: str) -> bool:
    """Is this turn worth spending a model call on?

    Deliberately permissive — a false fire costs one background call and the
    extractor answers "nothing"; a false skip loses the fact forever. Pure, so
    it can be tuned against a corpus without standing a model up.
    """
    text = (text or "").strip()
    if not text:
        return False
    return bool(_FIRST_PERSON.search(text) or _WHEN.search(text)
                or _has_entity(text) or len(text.split()) > _MIN_TOKENS)

def parse_facts(raw: str) -> list[dict]:
    """Strict. Anything unexpected is "nothing to remember", never a retry.

    The model is a 7B instruct running with response_format=json_object, so
    well-formed output is the norm and malformed output means it lost the
    plot on this particular sentence. Asking it again costs a call to get the
    same confusion back; the sentence will come round again if it mattered.
    """
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except Exception:
        logging.info(f"memory: unparseable extraction, treated as none: {raw[:200]!r}")
        return []
    items = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        logging.info(f"memory: extraction had no 'facts' list, treated as none: {raw[:200]!r}")
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().lower()
        fact = str(item.get("fact", "")).strip()
        # "none" lands here too and is dropped by the same check, which is
        # why the taxonomy does not need a branch of its own.
        if category in CATEGORIES and fact:
            out.append({"category": category, "fact": fact})
    return out

# Stop words carry no evidence, and leaving them in inflates every score
# toward "these two sentences are both English".
_STOP = {"a", "an", "the", "i", "im", "i'm", "is", "are", "was", "were", "to", "of",
         "and", "that", "it", "for", "in", "on", "my", "me", "we", "us", "our",
         "you", "your", "has", "have", "had", "be", "at"}

def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in _STOP}

def similarity(a: str, b: str) -> float:
    """Jaccard overlap of content words. 0.0 (nothing shared) to 1.0 (same words).

    ponytail: lexical, so "dislikes cilantro" and "hates cilantro" read as
    different facts and both get stored. That is the one thing embeddings
    would buy, and it is not worth a torch dependency in a repo people are
    meant to self-host from a six-line requirements.txt. Swap the body for
    cosine over real vectors if duplicate drift ever gets annoying -- this
    function is the only place either caller looks.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def remember(owner_id: int, candidates: list[dict]) -> None:
    """Merge candidates into the store, logging every decision.

    `known` is updated as we go, not re-read: two identical candidates in one
    batch must merge into each other rather than both being stored.
    """
    known = memories.all_for(owner_id)
    for candidate in candidates:
        best, score = None, 0.0
        for row in known:
            s = similarity(candidate["fact"], row["fact"])
            if s > score:
                best, score = row, s
        if best is not None and score >= config.MEMORY_DEDUP_THRESHOLD:
            memories.bump(best["id"])
            best["mention_count"] = best.get("mention_count", 1) + 1
            logging.info(f"memory: dedup {candidate['fact']!r} into #{best['id']} "
                         f"{best['fact']!r} (sim={score:.2f})")
        else:
            new_id = memories.save(owner_id, candidate["category"], candidate["fact"])
            known.append({"id": new_id, "fact": candidate["fact"],
                          "category": candidate["category"], "mention_count": 1})
            logging.info(f"memory: stored #{new_id} [{candidate['category']}] "
                         f"{candidate['fact']!r} (best sim={score:.2f})")

def observe(user_id: int, text: str) -> None:
    """Hot path. A regex and one INSERT — no model call, no network."""
    if not should_extract(text):
        logging.debug(f"memory: gate skipped {text[:60]!r}")
        return
    memories.enqueue(user_id, text)

def _sweep() -> None:
    """Drain the queue and merge whatever the model finds. Blocking by design:
    start() runs it in a thread so the sqlite writes and the LLM call never
    touch the event loop shared by every surface."""
    # Local imports: registry imports this module, and this module needs a
    # reference to itself to ask registry whether it is switched on. At module
    # scope either one is a cycle.
    from .. import registry
    from . import memory_skill

    # An owner who switches Memory off mid-day has queued turns already on
    # disk; draining them anyway would extract facts from a skill that is off.
    if not registry.is_enabled(memory_skill):
        return
    for row in memories.drain():
        owner_id, text = int(row["owner_id"]), row["text"]
        try:
            raw = brain.extract_facts(text)
        except Exception as e:
            logging.warning(f"memory: extraction call failed, dropping turn: {e}")
            continue
        candidates = parse_facts(raw)
        logging.info(f"memory: {len(candidates)} candidate(s) from {text[:60]!r}")
        remember(owner_id, candidates)

def for_prompt(user_id: int, text: str) -> list[str]:
    """The facts worth putting in front of the model for this message.

    Two sources, because lexical matching alone is not enough: anything said
    three times is "core profile" and goes in every turn, and the rest has to
    earn its place by sharing words with what was just asked. Small models get
    distracted by irrelevant context, so the floor matters as much as the cap.
    """
    rows = memories.all_for(user_id)
    core = [r for r in rows if r["mention_count"] >= _CORE_MENTIONS]
    rest = [r for r in rows if r["mention_count"] < _CORE_MENTIONS]
    scored = sorted(((similarity(text, r["fact"]), r) for r in rest),
                    key=lambda pair: pair[0], reverse=True)
    picked = [r for score, r in scored[:config.MEMORY_TOP_K] if score >= _INJECT_FLOOR]
    return [r["fact"] for r in core + picked]

async def start() -> None:
    # Sleeps first: the queue is empty at boot, and a sweep racing the
    # surfaces' own startup buys nothing.
    while True:
        await asyncio.sleep(config.MEMORY_SWEEP_SECONDS)
        try:
            await asyncio.to_thread(_sweep)
        except Exception as e:
            logging.warning(f"memory sweep failed: {e}")
