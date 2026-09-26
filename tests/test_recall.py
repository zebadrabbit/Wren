import os, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")
os.environ.setdefault("TIMEZONE", "UTC")

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from wren import config, recall
from wren.skills import notes_store as notes, memory_store as memories, reminders_store as reminders
from wren.skills import calendar_skill
from wren.skills.calendar_feed import Event


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_DB", str(tmp_path / "wren.db"))
    notes.init_db(); memories.init_db(); reminders.init_db()
    monkeypatch.setattr(config, "CALENDAR_URLS", [])


def _event(summary, days_ahead=3):
    start = datetime.now(ZoneInfo("UTC")).replace(microsecond=0) + timedelta(days=days_ahead)
    return Event(summary=summary, start=start, end=start + timedelta(hours=1), all_day=False)


def test_gather_pulls_matching_records_from_every_store(monkeypatch):
    notes.save(1, "dentist is dr patel on college ave", ["health"])
    notes.save(1, "the wifi password is on the fridge", [])
    memories.save(1, "fact", "daughter Maya is allergic to peanuts")
    memories.save(1, "person", "dentist appointments make me anxious")
    reminders.save(1, "dentist cleaning", (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(timespec="seconds"), repeat="180d")
    reminders.save(1, "water the plants", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds"))
    monkeypatch.setattr(config, "CALENDAR_URLS", ["https://cal.example/x.ics"])
    monkeypatch.setattr(calendar_skill, "events_between", lambda start, days: [_event("Dentist — Dr Patel"), _event("Kindergarten open day")])
    records = recall.gather(1, "when is my next dentist appointment", notes.search(1))
    sources = {(r["source"], r["content"]) for r in records}
    assert ("note", "dentist is dr patel on college ave") in sources
    assert ("memory", "dentist appointments make me anxious") in sources
    assert ("reminder", "dentist cleaning") in sources
    assert ("calendar", "Dentist — Dr Patel") in sources
    assert not any("wifi" in r["content"] or "peanuts" in r["content"] or "plants" in r["content"]
                   or "Kindergarten" in r["content"] for r in records)


def test_records_carry_a_label_and_a_when(monkeypatch):
    nid = notes.save(1, "dentist is dr patel", ["health"])
    fire = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(timespec="seconds")
    reminders.save(1, "dentist cleaning", fire, repeat="7d")
    records = {r["source"]: r for r in recall.gather(1, "dentist", notes.search(1))}
    assert records["note"]["note_id"] == nid and len(records["note"]["when"]) == 10
    assert records["reminder"]["when"].endswith(", every week")


def test_most_similar_first_capped_at_twenty():
    for i in range(30):
        notes.save(1, f"dentist note {i}", [])
    notes.save(1, "dentist dentist dentist", [])
    records = recall.gather(1, "dentist", notes.search(1))
    assert len(records) == recall.CAP == 20
    assert records[0]["content"] == "dentist dentist dentist"


def test_nothing_shares_a_word_falls_back_to_recent_notes_only():
    for i in range(25):
        notes.save(1, f"note number {i}", [])
    memories.save(1, "fact", "likes pineapple")
    records = recall.gather(1, "zzz qqq", notes.search(1))
    assert len(records) == 20 and all(r["source"] == "note" for r in records)


def test_calendar_is_skipped_when_unconfigured_and_a_dead_feed_does_not_fail_the_rest(monkeypatch):
    notes.save(1, "dentist is dr patel", [])
    called = []
    monkeypatch.setattr(calendar_skill, "events_between", lambda s, d: called.append(1) or (_ for _ in ()).throw(RuntimeError("feed down")))
    assert [r["source"] for r in recall.gather(1, "dentist", notes.search(1))] == ["note"]
    assert called == []
    monkeypatch.setattr(config, "CALENDAR_URLS", ["https://cal.example/x.ics"])
    assert [r["source"] for r in recall.gather(1, "dentist", notes.search(1))] == ["note"]
    assert called == [1]
