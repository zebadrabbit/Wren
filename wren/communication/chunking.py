"""Text splitting shared by every communication plugin whose platform
enforces a hard message-length ceiling.

The ceiling itself is NOT here — it is per-platform (Telegram's 4096,
Discord's 2000) and belongs to the plugin that owns that platform, per the
project's rule that a surface's limit stays with the surface. This module
only owns the cutting algorithm, so a ceiling change in one plugin can never
silently change another's.
"""


def chunks(text: str, limit: int) -> list[str]:
    """`text` split into pieces of at most `limit` characters, longest-first.

    Cuts on the last line break that fits so a note dump breaks between lines
    rather than mid-word, and falls back to a hard cut when one line is longer
    than the whole ceiling. The break itself is dropped — it is the seam, and
    keeping it would open the next message with a blank line.
    """
    parts = []
    while len(text) > limit:
        # from 1, not 0: a leading newline would cut an empty first message,
        # which is its own 400 ("text must be non-empty").
        cut = text.rfind("\n", 1, limit + 1)
        if cut < 1:
            parts.append(text[:limit])
            text = text[limit:]
        else:
            parts.append(text[:cut])
            text = text[cut + 1:]
    parts.append(text)
    return parts
