from . import notes
from . import brain

INTENTS = ["save_note", "recall_notes", "save_idea", "recall_ideas", "discard_idea", "expand_idea"]

PROMPT_GUIDELINES = """- save_note: user is capturing something for later (grocery item, plan, reminder)
- recall_notes: user wants to retrieve or search past notes
- save_idea: user explicitly wants to remember/capture an idea to revisit or expand later (e.g. "remember this idea...", "idea:..."), distinct from save_note's reminders/grocery items/plans
- recall_ideas: user wants to see all their saved ideas
- discard_idea: user wants to delete a previously saved idea; content is a short phrase identifying which idea, not the full idea text
- expand_idea: user wants Wren to elaborate/brainstorm further on a previously saved idea; content is a short phrase identifying which idea, not the full idea text"""

async def handle(intent, message, client, user_id, content, tags, person):
    if intent == "save_note":
        notes.save(user_id, content, tags)
        await message.channel.send("Saved.")

    elif intent == "recall_notes":
        matches = notes.search(user_id, tags=tags if tags else None)
        if not matches:
            await message.channel.send("No notes found.")
        else:
            summary = brain.recall(matches, content)
            await message.channel.send(summary)

    elif intent == "save_idea":
        if not content.strip():
            await message.channel.send("What idea should I save?")
        else:
            notes.save(user_id, content, ["idea"])
            await message.channel.send("Saved that idea.")

    elif intent == "recall_ideas":
        ideas = notes.search(user_id, tags=["idea"])
        if not ideas:
            await message.channel.send("No ideas saved.")
        else:
            await message.channel.send("\n".join(f"- {i['content']}" for i in ideas))

    elif intent == "discard_idea":
        if not content.strip():
            await message.channel.send("Which idea do you want to discard?")
        else:
            matches = notes.find(user_id, content, tags=["idea"])
            if not matches:
                await message.channel.send("No idea found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await message.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                notes.delete(matches[0]["id"])
                await message.channel.send(f"Discarded: {matches[0]['content']}.")

    elif intent == "expand_idea":
        if not content.strip():
            await message.channel.send("Which idea do you want to expand on?")
        else:
            matches = notes.find(user_id, content, tags=["idea"])
            if not matches:
                await message.channel.send("No idea found matching that.")
            elif len(matches) > 1:
                listing = "\n".join(f"- {m['content']}" for m in matches)
                await message.channel.send(f"Found more than one match, be more specific.\n{listing}")
            else:
                expansion = brain.expand(matches[0]["content"])
                await message.channel.send(expansion)
