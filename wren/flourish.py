import random

EMOTES = ["🐦", "🐦‍⬛", "🪶", "🐤", "🐣", "✨"]

def flourish(text: str) -> str:
    return f"{text} {random.choice(EMOTES)}"
