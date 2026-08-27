# Daily Briefing — Design (calendar, weather, briefing)

Wren speaks first. Once a day, at a time the owner picks, Wren composes what
the day looks like — calendar, reminders, weather, the shopping list — and
pushes it through `NOTIFY_VIA` without being asked. The same text is
available on demand ("what's my day look like").

Three skills, because the first two are useful on their own and the third is
only a composer:

| skill | intent(s) | reads | writes |
|---|---|---|---|
| `calendar_skill` | `recall_calendar` | ICS feed URL(s) | nothing |
| `weather_skill` | `get_weather` | Open-Meteo | nothing |
| `briefing_skill` | `briefing` + a daily loop | the two above, `reminders_store`, `shopping_store` | nothing |

## Why this shape

Today Wren opens its mouth unprompted for exactly three reasons: a reminder
fired, an email arrived, a repo event happened. Everything else is
answer-when-asked. The `input`/`chat` roles and `router.notify` are the right
seam for initiative; what is missing is content worth pushing. Calendar and
weather are the two things every assistant is asked about daily and Wren
cannot answer either. The briefing is the first thing that *composes*
several skills into one message — and it does so with zero LLM calls, so it
is cheap, deterministic, and cannot hallucinate an appointment.

## Verified constraints (checked 2026-08-26)

1. **No `dateutil`, no `icalendar` in the venv.** `requirements.txt` is six
   lines on purpose (self-hosters copy it). ICS parsing is stdlib, hand-rolled,
   with a documented recurrence ceiling.
2. **`httpx` is already a dependency** (`web_search.py`), synchronous, called
   under `asyncio.to_thread`. Both fetchers use it the same way.
3. **`registry.start_all()` starts any `PLUGINS` module with `start()`.**
   The briefing loop needs no `run.py` change.
4. **The classifier is fragile** (memory design, constraint 3). Every new
   `PROMPT_GUIDELINES` line is a risk. Three intents, one line each, and the
   skills parse "week"/"tomorrow" themselves from `ctx.text` instead of asking
   the model for a window.
5. **`chat.html` ignores unknown card kinds and shows the prose** (line 823).
   Cards for these skills can come later without a transport change.
6. **`GET /api/plugins` returns every `SETTABLE` value.** A private Google
   Calendar ICS address is a capability URL — anyone holding it reads the
   calendar. It goes in `SECRET_KEYS`, not `SETTABLE`.

## Decisions

**D1 — Calendar is read-only, from ICS feed URLs.** `CALENDAR_URLS` is a
comma-separated list in `.env` (one feed per calendar; Google, iCloud, Nextcloud
and Outlook all export one). No CalDAV, no OAuth, no event creation. Reading
is the whole daily use; writing is a different feature with a different
auth story.

**D2 — Parsing is stdlib with a named ceiling.** Unfold continuation lines,
split `VEVENT` blocks, read `SUMMARY`, `DTSTART`, `DTEND`/`DURATION`, `RRULE`,
`EXDATE`, `RECURRENCE-ID`, `STATUS`. Dates: `VALUE=DATE` (all-day), `Z`
(UTC), `TZID=` (that zone via `zoneinfo`), floating (the configured
`TIMEZONE`). Recurrence: `FREQ=DAILY|WEEKLY|MONTHLY|YEARLY` with `INTERVAL`,
`COUNT`, `UNTIL`, and `BYDAY` for `WEEKLY` only. Expansion happens inside a
requested window, never over the whole feed. `ponytail:` ceiling — `BYDAY`
with ordinals (`2MO`), `BYMONTHDAY`, `BYSETPOS`, `WKST` are ignored; an event
using them still appears on its `DTSTART` day and on the plain-frequency
instances. If that ever bites, swap `_expand` for `dateutil.rrule` — it is one
function with one caller.

**D3 — Feeds are cached per URL for `CALENDAR_CACHE_SECONDS` (default 300).**
A year of events is hundreds of KB; the briefing and an on-demand question a
minute later should not fetch twice. In-module dict, no persistence: a restart
just refetches.

**D4 — The window comes from the skill, not the model.** `recall_calendar`
reads `ctx.when` (the ISO date the classifier already produces for reminders;
missing → today) as the anchor day, then widens to 7 days if `ctx.text`
matches `\bweek\b`, or to tomorrow if it matches `\btomorrow\b` and `when` was
not set. The guideline line stays one sentence.

**D5 — Weather is Open-Meteo.** No key, no signup, JSON, generous free tier,
documented WMO codes. `WEATHER_LAT`, `WEATHER_LON` (floats, `SETTABLE`,
validated to ±90/±180) and `WEATHER_UNITS` (`fahrenheit`|`celsius`,
`SETTABLE`). Unset coordinates = skill inactive (`is_active()` false, panel
says why). Cached for 600 s. `get_weather` answers "now" unless `ctx.text`
matches `\btomorrow\b`.

**D6 — The briefing composes, never generates.** `briefing_skill.compose(user_id)
-> str` assembles four sections in fixed order — weather, calendar, reminders
due today, shopping list — each wrapped so one failing source degrades to one
line ("Calendar: couldn't reach the feed") instead of no briefing. Sections
for disabled or unconfigured skills are omitted, not apologised for. No model
call, by design: a briefing that invents a meeting is worse than none.

**D7 — Scheduled by local wall-clock, owner only.** `BRIEFING_TIME` (`HH:MM`
in `TIMEZONE`, `SETTABLE`, empty = off). The loop sleeps in ≤60 s steps and
re-reads the setting each step, so a change in the plugins panel takes effect
without a restart. It fires once per local date (in-memory guard;
`ponytail:` a restart inside the same minute could double-send — persist the
last-sent date if that is ever observed). It sends to `config.WHITELIST["owner"]`
via `router.notify`. Other household members do not get a briefing in this
slice; their calendars are not configured anywhere either.

**D8 — Reminders "due today" are the pending ones whose `fire_at` falls in
the local calendar day.** `reminders_store.pending(user_id)` already exists;
the filter is five lines in the briefing, not a new store query.

## Interfaces

```python
# wren/skills/calendar_skill.py
INTENTS = ["recall_calendar"]; PLUGIN_NAME = "Calendar"
@dataclass class Event: summary: str; start: datetime; end: datetime; all_day: bool
def is_active() -> bool                              # CALENDAR_URLS non-empty
def inactive_reason() -> str
def events_between(start: date, days: int) -> list[Event]   # sync; fetch+parse+expand, sorted
def format_day(events: list[Event], day: date) -> str       # "09:00–09:30  Standup" lines
async def handle(intent, ctx)

# wren/skills/weather_skill.py
INTENTS = ["get_weather"]; PLUGIN_NAME = "Weather"
def is_active() -> bool; def inactive_reason() -> str
def forecast() -> dict                               # sync; cached Open-Meteo payload
def summary(day: int = 0) -> str                     # 0 today/now, 1 tomorrow
async def handle(intent, ctx)

# wren/skills/briefing_skill.py
INTENTS = ["briefing"]; PLUGIN_NAME = "Briefing"
def compose(user_id: int) -> str                     # sync; no LLM
async def handle(intent, ctx)
async def start()                                    # daily loop
```

Config (all in `wren/config.py`):

| key | type | default | SETTABLE | notes |
|---|---|---|---|---|
| `CALENDAR_URLS` | list[str] | `[]` | no — `SECRET_KEYS` | capability URLs |
| `CALENDAR_CACHE_SECONDS` | int | 300 | yes (`_coerce_poll_seconds`) | |
| `WEATHER_LAT`, `WEATHER_LON` | float | unset | yes | validated range |
| `WEATHER_UNITS` | str | `fahrenheit` | yes | enum |
| `BRIEFING_TIME` | str | `""` | yes | `HH:MM` local, empty = off |

`manage.sh` keeps its own copy of the secret list — add `CALENDAR_URLS` there too.

## Output shapes

`recall_calendar`, today:
```
Today (Wed Aug 26):
  all day   Kate in town
  09:00–09:30  Standup
  13:00–14:00  Dentist
```
Empty: `Nothing on the calendar today.` A week: one block per day that has
events, days with nothing skipped, `Nothing on the calendar this week.` if all
are empty.

`get_weather`: `72°F and partly cloudy, wind 8 mph. High 81, low 63, 20% chance of rain.`
Tomorrow: `Tomorrow: high 79, low 61, mostly sunny, 10% chance of rain.`

`briefing` / the pushed message:
```
Good morning. Wed Aug 26.
Weather: 72°F and partly cloudy. High 81, low 63, 20% chance of rain.
Calendar:
  09:00–09:30  Standup
  13:00–14:00  Dentist
Reminders today:
  17:00  call the vet
Shopping list: 6 items.
```
Sections with nothing to say are dropped. If every section is empty:
`Good morning. Wed Aug 26. Nothing on the calendar, no reminders, list is empty.`

## Error handling

- Feed fetch failure (timeout, 4xx/5xx, unparseable): `recall_calendar` says
  `Couldn't reach the calendar feed.`; the briefing shows one degraded line;
  both log the exception at WARNING with the URL's host only (the path is the
  secret).
- A single malformed `VEVENT` is skipped with a debug log; the rest of the
  feed still parses.
- Open-Meteo failure: `get_weather` says `Couldn't reach the weather service.`
- `router.notify` returning False for the briefing is logged and the date is
  still marked sent (same contract as reminders: False is permanent).

## Testing

- ICS parser: fixtures as inline strings — all-day, timed with `TZID`, UTC,
  floating, `DURATION`, weekly `BYDAY` recurrence with `UNTIL`, `COUNT`,
  `EXDATE`, a `RECURRENCE-ID` override, an event straddling midnight, a
  malformed block. Window expansion asserts exact instance dates.
- Fetch/cache: patch `httpx.get`; assert one call for two reads inside the TTL.
- Weather: patch `httpx.get` with a canned Open-Meteo payload; assert the
  summary strings and unit handling.
- Briefing: patch the three sources; assert section order, omission of empty
  sections, one degraded line when a source raises, and that no `brain.*`
  function is called (`patch.object(brain, "chat", side_effect=AssertionError)`).
- Loop: `_should_fire(now, "07:30", last_sent)` is pure and tested before the
  time, inside the ten-minute window, after it, and for the once-per-day
  guard; the loop body is tested with the sleep patched and `router.notify`
  collected, matching `tests/test_reminder_plugin.py`.
- Skill tests use `CollectingChannel` + `Ctx` and call `handle()` directly.

## Deliberately not in this slice

- Calendar cards / a Calendar space in the web chat (prose first, as reminders were).
- Creating or editing events.
- Per-person calendars or briefings for non-owners.
- Weather alerts pushed on their own.
- Memory-personalised briefings ("Kate's birthday is Friday") — after memory ships.
