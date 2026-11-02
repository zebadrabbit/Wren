import io
import logging
import os
import wave

from . import config


class TTSUnavailable(RuntimeError):
    """piper-tts is not installed, WREN_TTS_VOICE is unset, or the voice could not be loaded."""


_voice = None


def _load():
    """Lazy — the voice is tens of MB and most installs never speak."""
    global _voice
    if _voice is not None:
        return _voice
    path = config.WREN_TTS_VOICE
    if not path:
        raise TTSUnavailable(
            "Text-to-speech is unavailable: set WREN_TTS_VOICE to a Piper voice .onnx path"
        )
    if not os.path.isfile(path):
        raise TTSUnavailable(f"Text-to-speech is unavailable: voice file not found: {path}")
    try:
        from piper import PiperVoice
    except ImportError as e:
        # include the real error: a native-library load failure (onnxruntime)
        # also surfaces as ImportError, and reporting that as "not installed"
        # sends people down the wrong path
        raise TTSUnavailable(
            f"Text-to-speech is unavailable: {e}. "
            f"If it is not installed, run: pip install piper-tts"
        ) from e
    try:
        logging.info(f"Loading TTS voice '{path}' …")
        _voice = PiperVoice.load(path)
    except Exception as e:
        raise TTSUnavailable(f"Could not load TTS voice '{path}': {e}") from e
    return _voice


def synthesize(text: str) -> bytes:
    """A complete WAV file (mono, 16-bit, the voice's own sample rate)."""
    voice = _load()          # before the writer opens: a wave file closed with no
    buf = io.BytesIO()       # params set raises wave.Error and would mask TTSUnavailable
    try:
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(text, wf)
    except wave.Error:
        # piper skips sentences with no phonemes ("...", an em dash, whitespace)
        # and sets the WAV params only on the first chunk, so a reply with
        # nothing sayable leaves the writer unconfigured and its close raises.
        # That is "nothing to say", not a failure.
        return b""
    return buf.getvalue()


def reset() -> None:
    """Drop the cached voice. Tests use this; so would a voice change at runtime."""
    global _voice
    _voice = None
