from datetime import datetime
from zoneinfo import ZoneInfo
from ..channel import Ctx
from . import notes_store as notes
from .. import brain
from .. import config
from .. import flourish

INTENTS = ["save_note", "recall_notes", "save_idea", "recall_ideas", "discard_idea", "expand_idea", "export_notes"]
DESTRUCTIVE = ["discard_idea"]
CONFIRM = {"discard_idea": 'discard the idea "{content}"'}
PLUGIN_NAME = "Notes & Ideas"

PROMPT_GUIDELINES = """- save_note: user is capturing something for later (grocery item, plan, reminder)
- recall_notes: user wants to retrieve or search past SAVED notes (not the same as asking about this conversation/chat itself, e.g. "do you remember what I said" is chat, not recall_notes); only set tags if the user explicitly names a category to filter by (e.g. "show my grocery notes") — otherwise leave tags empty to see everything, since you don't know what tags were used when notes were saved
- save_idea: user explicitly wants to remember/capture an idea to revisit or expand later (e.g. "remember this idea...", "idea:..."), distinct from save_note's reminders/grocery items/plans
- recall_ideas: user wants to see all their saved ideas
- discard_idea: user wants to delete a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- expand_idea: user wants Wren to elaborate/brainstorm further on a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- export_notes: user wants their notes/ideas as a downloadable file to use elsewhere (e.g. "send my notes as a file", "export my ideas so I can paste them into X")"""


def _describe(atts: list) -> str:
    """"1 image" / "3 images" / "2 files" for a list of attachment rows or
    Inbound records -- anything with a .mime or ["mime"]."""
    from .. import filetypes
    mimes = [a["mime"] if isinstance(a, dict) else a.mime for a in atts]
    n = len(mimes)
    if all(m in filetypes.IMAGE_MIMES for m in mimes):
        return f"{n} image" + ("" if n == 1 else "s")
    return f"{n} file" + ("" if n == 1 else "s")


def _marker(note_id: int) -> str:
    atts = notes.attachments(note_id)
    return f" ({_describe(atts)})" if atts else ""


async def _send_attachments(ctx: Ctx, note_ids: list[int]) -> None:
    """Send the files of these notes, in note order, stopping at the cap."""
    sent = 0
    for note_id in note_ids:
        for meta in notes.attachments(note_id):
            if sent >= notes.MAX_FILES_PER_REPLY:
                return
            row = notes.attachment(meta["id"])
            if row is None:
                continue
            await ctx.channel.send_file(row["data"], row["filename"])
            sent += 1


_REFUSALS = {
    "too big": "That's too big, 10 MB max.",
    "unsupported type": "I can keep images and PDFs, not that.",
}


def _build_export(user_id: int) -> str | None:
    all_notes = notes.search(user_id)
    plain = [n for n in all_notes if "idea" not in n["tags"].split(",")]
    ideas = [n for n in all_notes if "idea" in n["tags"].split(",")]
    if not plain and not ideas:
        return None

    today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
    lines = [f"# Wren Notes Export — {today}", "", "## Notes"]
    if plain:
        for n in plain:
            tag_suffix = f" (tags: {n['tags']})" if n["tags"] else ""
            lines.append(f"- [{n['created_at'][:10]}] {n['content']}{_marker(n['id'])}{tag_suffix}")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("## Ideas")
    if ideas:
        for i in ideas:
            lines.append(f"- {i['content']}")
    else:
        lines.append("_None._")
    return "\n".join(lines) + "\n"

async def handle(intent: str, ctx: Ctx) -> None:
    if intent == "save_note":
        if not ctx.files:
            notes.save(ctx.user_id, ctx.content, ctx.tags)
            await ctx.channel.send(flourish.flourish("Saved."))
            return
        note_id = notes.save(ctx.user_id, ctx.content, ctx.tags)
        kept, skipped = [], []
        for f in ctx.files:
            try:
                notes.attach(note_id, f.filename, f.mime, f.data)
                kept.append(f)
            except ValueError as e:
                skipped.append(f"Skipped {f.filename}: {_REFUSALS.get(str(e), str(e))}")
        if not kept and not ctx.content.strip():
            # every file refused and nothing to say: a note with no body and
            # no file is not worth keeping
            notes.delete(note_id)
            await ctx.channel.send(" ".join(skipped))
            return
        parts = ([f"Saved, {_describe(kept)}."] if kept else []) + skipped
        await ctx.channel.send(" ".join(parts))

    elif intent == "recall_notes":
        matches = notes.search(ctx.user_id, tags=ctx.tags if ctx.tags else None)
        if not matches and ctx.tags:
            # the model's guessed tag may not match what was actually used
            # when the note was saved — fall back to an unfiltered search
            # rather than falsely reporting no notes at all
            matches = notes.search(ctx.user_id, tags=None)
        if ctx.content.strip():
            # a specific question — let the LLM answer using the notes. NOT a
            # card: a card re-dispatches this intent to refresh itself, which
            # would put an LLM call behind every render and every page reload.
            if not matches:
                await ctx.channel.send("No notes found.")
            else:
                summary = brain.recall(matches, ctx.content)
                await ctx.channel.send(summary)
                await _send_attachments(ctx, [n["id"] for n in matches])
        else:
            rows = [{"content": n["content"],
                     "tags": [t for t in n["tags"].split(",") if t],
                     "created_at": n["created_at"],
                     "files": [{"id": a["id"], "filename": a["filename"], "mime": a["mime"]}
                               for a in notes.attachments(n["id"])]}
                    for n in matches]
            if rows:
                text = "\n".join(
                    f"[{n['created_at'][:10]}] {n['content']}{_marker(n['id'])}"
                    + (f" (tags: {n['tags']})" if n["tags"] else "")
                    for n in matches)
            else:
                text = "No notes found."
            # One message, not one per note: a wall of separate messages is what
            # the card replaces, and Discord/Telegram get the same relief.
            await ctx.channel.send_card(
                "notes", {"notes": rows}, text,
                intent="recall_notes",
                # the tags ride along so a filtered card stays filtered when it
                # re-renders later; content stays empty for the reason above
                params={"content": "", "tags": list(ctx.tags or [])},
            )
            await _send_attachments(ctx, [n["id"] for n in matches])

    elif intent == "save_idea":
        if not ctx.content.strip():
            await ctx.channel.send("What idea should I save?")
        else:
            notes.save(ctx.user_id, ctx.content, ["idea"])
            await ctx.channel.send(flourish.flourish("Saved that idea."))

    elif intent == "recall_ideas":
        ideas = notes.search(ctx.user_id, tags=["idea"])
        rows = [{"content": i["content"], "created_at": i["created_at"]} for i in ideas]
        text = "\n".join(f"- {i['content']}" for i in ideas) if ideas else "No ideas saved."
        await ctx.channel.send_card(
            "ideas", {"ideas": rows}, text,
            intent="recall_ideas", params={"content": ""},
        )

    elif intent == "discard_idea":
        if not ctx.content.strip():
            await ctx.channel.send("Which idea do you want to discard?")
        else:
            matches = notes.find(ctx.user_id, ctx.content, tags=["idea"])
            if not matches:
                await ctx.channel.send("No idea found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await ctx.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                notes.delete(matches[0]["id"])
                await ctx.channel.send(flourish.flourish(f"Discarded: {matches[0]['content']}."))

    elif intent == "expand_idea":
        if not ctx.content.strip():
            await ctx.channel.send("Which idea do you want to expand on?")
        else:
            matches = notes.find(ctx.user_id, ctx.content, tags=["idea"])
            if not matches:
                await ctx.channel.send("No idea found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await ctx.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                expansion = brain.expand(matches[0]["content"])
                await ctx.channel.send(expansion)

    elif intent == "export_notes":
        export_text = _build_export(ctx.user_id)
        if export_text is None:
            await ctx.channel.send("Nothing to export yet.")
        else:
            today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
            await ctx.channel.send_file(export_text.encode("utf-8"), f"notes-export-{today}.md")
