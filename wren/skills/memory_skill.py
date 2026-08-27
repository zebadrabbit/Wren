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
