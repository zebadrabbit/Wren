import pytest

from wren import filetypes

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\0" * 16
GIF = b"GIF89a" + b"\0" * 16
PDF = b"%PDF-1.7\n" + b"\0" * 16


@pytest.mark.parametrize("data,mime", [
    (JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"),
    (GIF, "image/gif"), (PDF, "application/pdf"),
])
def test_sniff_knows_the_five_accepted_types(data, mime):
    assert filetypes.sniff(data) == mime


def test_sniff_rejects_a_renamed_executable_and_empty_bytes():
    assert filetypes.sniff(b"MZ\x90\x00" + b"\0" * 16) is None
    assert filetypes.sniff(b"") is None
    # RIFF alone is not WebP: a WAV file starts RIFF....WAVE
    assert filetypes.sniff(b"RIFF\x10\x00\x00\x00WAVEfmt ") is None


def test_image_mimes_are_exactly_the_four_image_types():
    assert filetypes.IMAGE_MIMES == {"image/jpeg", "image/png", "image/webp", "image/gif"}


def test_max_bytes_is_ten_megabytes():
    assert filetypes.MAX_BYTES == 10 * 1024 * 1024
