import asyncio
import logging
import re
import time

from . import brain
from . import config
from . import flourish
from .skills import notes_store as notes
from .skills import memory_skill
from . import registry
from . import router
from .channel import Channel, Ctx, Inbound

HELP_TEXT = """Here's what I can actually do:

**Notes**
- "remind me to call the plumber" — saves a note
- "what do I need to do" — recalls and answers from your notes
- "show my grocery notes" — filters recalled notes/ideas by tag

**Ideas** (separate from notes — for things to revisit or expand later)
- "remember this idea: build a treehouse"
- "what ideas have I saved"
- "discard the idea about the treehouse"
- "expand on the treehouse idea"
- "send my notes as a markdown file" — exports all notes and ideas as a downloadable file

**Reminders**
- "remind me to take out the trash at 6pm" / "in 20 minutes" / "tomorrow morning"
- "what are my reminders" — shows all upcoming reminders with times
- "cancel the trash reminder" — matches by phrase, asks for specifics if needed

**Calendar** (needs CALENDAR_URLS)
- "what's on my calendar" / "what does tomorrow look like" / "show my week"

**Weather** (needs WEATHER_LAT / WEATHER_LON)
- "what's the weather" / "will it rain tomorrow"

**Briefing**
- "what's my day look like" — weather, calendar, today's reminders and the shopping list in one message

**Shopping** (one shared list)
- "add potatoes to shopping"
- "got the potatoes" / "remove potatoes from shopping"
- "what's on the shopping list"
- "send shopping to hubby"

**Messaging**
- "tell hubby dinner's at 7" — messages a whitelisted contact by name

**Contacts** (owner only)
- "add 123456789012345678 as hubby" — whitelists a new contact
- "remove hubby" — un-whitelists a contact
- "who's whitelisted" — lists current contacts

**Status**
- "show status" / "what backend are you using" / "show model" — reports the active LLM backend, endpoint, uptime, and token usage

**Web** (when configured)
- "what's the weather in Chicago tomorrow" / "any news on the port strike" — searches the web and summarizes with links
- "read me the first one" / "read https://…" — fetches a page in full and summarizes

**Pins**
- "pin: wifi password is 12345" — pins an important note for quick access
- "unpin the wifi one" — removes a pin, asks for specifics if more than one matches
- "what's pinned" — lists current pins

**Plugins**
- "what plugins do you have" / "what's active" — lists Wren's active capabilities

Anything else just falls through to open-ended chat."""

_START_TIME = time.monotonic()

brain.register_plugins(registry.all_intents(), registry.all_guidelines())

# Questions core has asked and not yet had answered: user_id -> (intent, ctx,
# expires_at). One per user; a newer destructive request replaces the older.
# ponytail: process-local, so a restart forgets the question -- the safe
# direction. Persist only if a multi-process deployment ever exists.
_pending: dict[int, tuple[str, Ctx, float]] = {}
_CONFIRM_TTL = 120.0
_YES = re.compile(r"^(yes|yeah|yep|yup|do it|confirm|go ahead|sure)\b", re.I)
# "yeah,? no" / "yes,? no" first: a colloquial no. Checked before _YES below
# because ^yeah\b would otherwise claim it.
# don'?[’']?t: faster-whisper emits the curly apostrophe (U+2019), and
# transcripts sometimes drop it entirely -- "don't"/"don’t"/"dont" all count.
# cancel/stop are anchored to the WHOLE utterance (optionally "it"/"that"/
# "this" and trailing punctuation) via their own $-terminated alternative:
# unlike the other words here, they are common word-one of a real command
# ("cancel my 6pm reminder"), which must fall through to the classifier
# rather than be read as an answer to an unrelated pending question.
_NO = re.compile(
    r"^(?:yeah,?\s*no|yes,?\s*no|no|nope|nah|never mind|nevermind|don'?[’']?t)\b"
    r"|^(?:cancel|stop)(?:\s+(?:it|that|this))?[.!]?$",
    re.I,
)

# Files that arrived without a caption, waiting for one: user_id ->
# (files, expires_at). The next message from that user is the caption,
# whatever it says -- a wrong caption is one note to delete, while guessing
# whether "what's the weather" was meant as one is worse. Process-local for
# the same reason as _pending.
#
# Keyed by Wren user id, not by surface: the park is per person, so a bare
# photo sent on Telegram can be captioned from a message typed in the web
# chat a minute later. It holds the raw bytes in RAM until the next text or
# expiry, which is fine bounded by a household's size and message rate but
# would not scale past that.
_pending_files: dict[int, tuple[list[Inbound], float]] = {}
_FILES_TTL = 300.0


# A caption that asks something. A trailing "?" or a leading question word;
# deliberately a plain rule rather than the classifier, so that what happens
# to a photo is predictable from the words alone.
_QUESTION = re.compile(
    r"^(?:what|what's|which|who|where|when|why|how|is|are|was|were|do|does|did|can|could|"
    r"should|would|will|has|have)\b", re.I)


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    return t.endswith("?") or bool(_QUESTION.match(t))


def _take_parked(user_id: int) -> tuple[list[Inbound], bool] | None:
    """(files, still_fresh) for this user's parked files, removed; None if
    nothing was parked. Expired entries come back with False so the caller
    can say so instead of silently dropping a photo."""
    entry = _pending_files.pop(user_id, None)
    if entry is None:
        return None
    files, deadline = entry
    return files, deadline > time.monotonic()


def _take_pending(user_id: int):
    """The unexpired question for this user, removed. Answered or not, one
    turn is all it gets: asking again is nagging."""
    entry = _pending.pop(user_id, None)
    if entry and entry[2] > time.monotonic():
        return entry
    return None


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _status_lines() -> list[str]:
    info = brain.status()
    provider = info["provider"]
    tokens = info["tokens"]
    return [
        f"Backend: {provider['name']} ({provider['model']})",
        f"Endpoint: {provider['base_url']}",
        f"Uptime: {_format_uptime(time.monotonic() - _START_TIME)}",
        f"Tokens this session: {tokens['total']:,} ({tokens['prompt']:,} prompt / {tokens['completion']:,} completion)",
    ]


async def handle_message(user_id: int, text: str, channel: Channel, *, source: str = "text",
                         files: list[Inbound] | None = None) -> None:
    """Transport-free dispatch. Surfaces authenticate the caller, build a
    Channel, and call this. Nothing below here knows what a Discord is.

    `source` is "text" or "voice" — voice gets a yes/no confirmation before
    running anything destructive, since transcription mis-hears; text is
    unaffected and always acts immediately.

    `files` are attachments that arrived with the words. With a caption they
    become a note; without one Wren asks what the photo is and the next
    message answers."""
    # authorization gate — surfaces do authn (who are you), this does authz.
    # Kept here rather than per-surface so a new surface cannot forget it.
    if user_id not in config.id_to_name():
        return

    text = (text or "").strip()
    files = list(files or [])
    if not text and not files:
        return

    await channel.ack("seen")

    # Answering a question core asked last turn happens BEFORE the classifier:
    # "yes" is not an intent, and a 7B model handed a bare "yes" with history
    # will confidently pick something. Anything that is not a yes or a no
    # drops the question and is handled as the new turn it is.
    pending = _take_pending(user_id)
    if pending:
        intent, ctx, _ = pending
        # _NO checked first: "yeah no" matches ^yeah\b in _YES too, and it means no.
        if _NO.match(text):
            await channel.send("Okay, left it alone.")
            await channel.ack("done")
            return
        if _YES.match(text):
            ctx.channel = channel     # reply where the answer came from
            try:
                plugin = registry.INTENT_HANDLERS.get(intent)
                if plugin is None or not registry.is_enabled(plugin):
                    await channel.send("That skill is switched off.")
                else:
                    await plugin.handle(intent, ctx)
                await channel.ack("done")
            except Exception as e:
                logging.error(f"Error handling confirmation from {user_id}: {e}")
                await channel.send("Something went wrong, try again.")
                await channel.ack("error")
            return

    if files:
        # a new photo, captioned or not, supersedes a parked one: the parked
        # one must not hijack the user's next unrelated message
        _pending_files.pop(user_id, None)

    # A caption arriving for parked files, or a bare photo to park. After the
    # yes/no check on purpose: an answer to a question core asked is still an
    # answer, and the photo keeps waiting.
    if text and not files:
        parked = _take_parked(user_id)
        if parked is not None:
            parked_files, fresh = parked
            if fresh:
                files = parked_files
            else:
                await channel.send("That photo timed out, send it again.")
    if files and not text:
        _pending_files[user_id] = (files, time.monotonic() + _FILES_TTL)
        await channel.send("What is this?")
        await channel.ack("done")
        return

    try:
        history = await channel.history(limit=10)

        # to_thread: brain talks to the LLM over synchronous httpx and takes
        # seconds. Inline, it freezes the event loop shared by every surface
        # and every plugin poller — two browser tabs would serialise, and the
        # Discord gateway heartbeat would stall on each call.
        result = await asyncio.to_thread(brain.detect_intent, user_id, text, history)
        intent = result.get("intent", "chat")
        if files and intent in registry.file_intents() and intent != "save_note":
            # a skill that knows what to do with a picture ("add these to the
            # shopping list") gets it with the words
            pass
        elif files and _is_question(text):
            # "what's in this?", "is this cable usb-c" -- answered from the
            # picture, and the picture is not kept: a question is not a note
            answer = await asyncio.to_thread(brain.describe, files, text)
            await channel.send(answer)
            await channel.ack("done")
            return
        elif files:
            # Anything else with a picture is a note. Not the classifier's
            # guess: "receipt from the tyre place" must not gamble on a small
            # model reading it as weather. Its tags are still kept.
            intent = "save_note"
            result["content"] = text
            notes_plugin = registry.INTENT_HANDLERS.get("save_note")
            if notes_plugin is None or not registry.is_enabled(notes_plugin):
                await channel.send("Notes is switched off, so I can't keep that.")
                await channel.ack("done")
                return
        ctx = Ctx(
            user_id=user_id,
            channel=channel,
            content=result.get("content", text),
            text=text,
            tags=result.get("tags", []),
            person=result.get("person"),
            when=result.get("when"),
            source=source,
            files=files,
        )

        # is_enabled as well as membership: all_intents() already stops
        # offering a disabled skill, but a model can emit an intent it was
        # never offered. Treat that as unknown so it falls through to chat.
        if intent in registry.INTENT_HANDLERS and registry.is_enabled(registry.INTENT_HANDLERS[intent]):
            if source == "voice" and intent in registry.destructive_intents():
                # Transcription mis-hears and the skills fuzzy-match; between
                # them "remove milk" can become "clear the list". Ask first.
                phrase = registry.confirm_phrase(intent)
                # ctx.content is the model's extraction and brain.detect_intent
                # does no coercion -- a bare `"content": null` from the model
                # must not raise here.
                what = (ctx.content or "").strip() or "that"
                body = phrase.format(content=what) if "{content}" in phrase else phrase
                await channel.send(f"Confirm: {body}? Say yes or no.")
                # Armed only once the question is actually out: a None content
                # above, or a send() that raises (e.g. a Discord rate limit),
                # must not leave a destructive intent pending that the user
                # never saw asked -- the next "yes" would fire it blind.
                _pending[user_id] = (intent, ctx, time.monotonic() + _CONFIRM_TTL)
            else:
                await registry.INTENT_HANDLERS[intent].handle(intent, ctx)

        elif intent == "help":
            await channel.send(HELP_TEXT)

        elif intent == "status":
            await channel.send("\n".join(_status_lines()))

        elif intent == "list_plugins":
            lines = [f"{'✅' if active else '⏸️'} {name}" for name, active in registry.plugin_status()]
            await channel.send("\n".join(lines))

        elif intent == "send_to_person":
            target_name = (ctx.person or "").lower()
            if target_name not in config.whitelist():
                await channel.send("I don't know how to reach them.")
            else:
                notes.save(user_id, ctx.content, ctx.tags)
                sender = config.id_to_name().get(user_id, "someone")
                ok = await router.notify_name(target_name, f"From {sender}: {ctx.content}")
                if ok:
                    await channel.send(flourish.flourish(f"Sent to {target_name}."))
                else:
                    await channel.send(f"Couldn't reach {target_name} — they may be unreachable right now.")

        else:  # chat
            # Only chat turns are observed: "add milk" and "remind me at 6"
            # are commands, not disclosures, and running the extractor on
            # them is a model call spent to be told "nothing here".
            on = registry.is_enabled(memory_skill)
            # for_prompt is a sqlite read done inline on the event loop, by
            # design (spec D2): a household's memories are hundreds of rows,
            # not thousands, and a thread hop costs more than the read itself
            # -- unlike the model call on the next line, which is to_thread'd
            # because it takes seconds, not microseconds.
            remembered = memory_skill.for_prompt(user_id, text) if on else []
            await channel.send(await asyncio.to_thread(brain.chat, text, history, remembered))
            if on:
                memory_skill.observe(user_id, text)

        await channel.ack("done")
    except Exception as e:
        logging.error(f"Error handling message from {user_id}: {e}")
        await channel.send("Something went wrong, try again.")
        await channel.ack("error")
