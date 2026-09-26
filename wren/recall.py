"""Everything Wren has on a question, from every store that could hold it.

"What did I say about the dentist" used to look only at notes; the answer is
as likely to be a memory the extractor kept, a reminder, or a calendar
event. gather() returns one ranked, labelled list for brain.recall.

ponytail: relevance is word overlap (memory_skill.similarity), so "dentist"
finds "dentist" and not "tooth doctor". That is the ceiling; embeddings are
the upgrade and this module is the only place they would go.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config
from .skills import calendar_skill
from .skills import memory_store as memories
from .skills import reminders_store as reminders
from .skills.memory_skill import similarity
from .skills.reminder_skill import _format_local, _format_repeat

# Records handed to the model per question. Small models get distracted by
# irrelevant context, and a household's notes outgrow any prompt.
CAP = 20
_CALENDAR_DAYS = 30


def gather(user_id: int, question: str, notes: list[dict]) -> list[dict]:
    """Records {source, when, content[, note_id]} that share a word with the
    question, most similar first, at most CAP. `notes` is the caller's already
    tag-filtered candidate set, so a real tag match keeps narrowing. With no
    overlap anywhere, the CAP most recent notes -- the old behaviour, bounded.
    """
    records = [{"source": "note", "when": n["created_at"][:10], "note_id": n["id"],
                "content": n["content"], "tags": n["tags"]}
               for n in notes]
    records += [{"source": "memory", "when": m["last_seen_at"][:10], "content": m["fact"]}
                for m in memories.all_for(user_id)]
    records += [{"source": "reminder", "content": r["content"],
                 "when": _format_local(r["fire_at"]) + (f", {_format_repeat(r['repeat'])}" if r.get("repeat") else "")}
                for r in reminders.pending(user_id)]
    if calendar_skill.is_active():
        try:
            today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
            records += [{"source": "calendar", "content": e.summary,
                         "when": e.start.strftime("%Y-%m-%d" if e.all_day else "%Y-%m-%d %H:%M")}
                        for e in calendar_skill.events_between(today, _CALENDAR_DAYS)]
        except Exception as e:
            # a dead feed must not turn "what did I say about X" into an error
            logging.warning(f"recall: calendar skipped: {e}")
    scored = [(similarity(question, r["content"]), r) for r in records]
    hits = [r for s, r in sorted(scored, key=lambda p: p[0], reverse=True) if s > 0]
    if hits:
        return hits[:CAP]
    return [r for r in records if r["source"] == "note"][:CAP]
