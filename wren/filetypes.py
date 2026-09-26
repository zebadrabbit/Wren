"""What a file is, decided from its first bytes.

Shared by the notes store (what it will keep) and the Telegram plugin (how to
send it back), so neither imports the other. Filenames and declared mimes
are never trusted: a renamed executable sniffs as nothing and is refused.
"""

MAX_BYTES = 10 * 1024 * 1024

IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
ACCEPTED = IMAGE_MIMES | {"application/pdf"}


def sniff(data: bytes) -> str | None:
    """The accepted mime type these bytes are, or None."""
    head = data[:12]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    return None
