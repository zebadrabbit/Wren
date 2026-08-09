# Notes/Ideas Markdown Export — Design

## Goal

"send my notes as a markdown file" / "export my ideas" — a new intent that
sends a `.md` file attachment covering everything the user has saved via
`notes.py` (plain notes and ideas), so it can be pulled into another tool
instead of only read inline in Discord. Uses the bot's `ATTACH_FILES`
permission, granted in the OAuth invite but unused until now.

## `wren/notes_plugin.py`: new `export_notes` intent

Lives in `notes_plugin.py`, not a new plugin file — it operates entirely on
data `notes_plugin.py` already owns (`notes.search`). `INTENTS` grows one
entry:

```python
INTENTS = ["save_note", "recall_notes", "save_idea", "recall_ideas", "discard_idea", "expand_idea", "export_notes"]
```

`PROMPT_GUIDELINES` grows one bullet:

```
- export_notes: user wants their notes/ideas as a downloadable file to use elsewhere (e.g. "send my notes as a file", "export my ideas so I can paste them into X")
```

## Building the file

New helper, `_build_export(user_id: int) -> str | None`:

```python
def _build_export(user_id: int) -> str | None:
    all_notes = notes.search(user_id)
    plain = [n for n in all_notes if "idea" not in n["tags"].split(",")]
    ideas = [n for n in all_notes if "idea" in n["tags"].split(",")]
    if not plain and not ideas:
        return None

    lines = [f"# Wren Notes Export — {datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m-%d')}", ""]
    lines.append("## Notes")
    if plain:
        for n in plain:
            tag_suffix = f" (tags: {n['tags']})" if n["tags"] else ""
            lines.append(f"- [{n['created_at'][:10]}] {n['content']}{tag_suffix}")
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
```

(Matches the date-formatting pattern already used in `brain.py`'s `_now()`
— `ZoneInfo(config.TIMEZONE)` — so the export date reflects the
household's configured timezone, not server UTC. `notes_plugin.py` gains
`from datetime import datetime` and `from zoneinfo import ZoneInfo` and
`from . import config` imports.)

`n["tags"].split(",")` matches the existing pattern used everywhere else in
this file (e.g. the `recall_notes` tag_suffix logic) for turning the
comma-joined `tags` column back into a list — no new tag-parsing logic
introduced.

## Sending the file

New `elif intent == "export_notes":` branch:

```python
elif intent == "export_notes":
    export_text = _build_export(user_id)
    if export_text is None:
        await message.channel.send("Nothing to export yet.")
    else:
        today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
        buf = io.BytesIO(export_text.encode("utf-8"))
        await message.channel.send(file=discord.File(buf, filename=f"notes-export-{today}.md"))
```

`notes_plugin.py` gains `import io` and `import discord`. No temp files on
disk — `io.BytesIO` is passed directly to `discord.File`.

## Testing

`tests/test_notes_plugin.py` gains:
- `test_export_notes_empty`: no notes/ideas saved → plain-text "Nothing to
  export yet.", no `file=` kwarg passed to `send`.
- `test_export_notes_notes_only`: notes saved, no ideas → `send` called
  with a `file=` kwarg; decode the `discord.File`'s underlying bytes and
  assert the `## Notes` section contains the saved content and `## Ideas`
  says `_None._`.
- `test_export_notes_ideas_only`: mirror case for ideas-only.
- `test_export_notes_both`: both present → both sections populated,
  filename matches `notes-export-YYYY-MM-DD.md` for a fixed/mocked "today".

Tests read `discord.File`'s content via its `.fp` attribute (a `BytesIO`),
consistent with how `discord.File` is constructed in the implementation —
no mocking of `discord.File` itself, so the test exercises the real
Discord.py object the way `bot.py` will actually send it.

## Out of scope

- Filtering the export by tag or date range (always exports everything;
  matches the "pull it all into another tool" use case from the original
  idea, not a report-builder).
- Exporting shopping list or reminders (this spec is notes+ideas only, per
  the "second whitelisted contact" era's original `ATTACH_FILES` idea,
  which named notes/ideas specifically).
- CSV/JSON export formats (markdown only, per the original idea).
