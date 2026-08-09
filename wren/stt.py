import io
import logging

from . import config


class STTUnavailable(RuntimeError):
    """faster-whisper is not installed, or the model could not be loaded."""


_model = None


def _load():
    """Lazy — the model is hundreds of MB and most installs never use voice."""
    global _model
    if _model is not None:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        # include the real error: a native-library load failure (missing
        # libctranslate2, wrong CUDA) also surfaces as ImportError, and
        # reporting that as "not installed" sends people down the wrong path
        raise STTUnavailable(
            f"Speech-to-text is unavailable: {e}. "
            f"If it is not installed, run: pip install faster-whisper"
        ) from e
    try:
        logging.info(f"Loading STT model '{config.WREN_STT_MODEL}' …")
        _model = WhisperModel(
            config.WREN_STT_MODEL,
            device=config.WREN_STT_DEVICE,
            compute_type=config.WREN_STT_COMPUTE,
        )
    except Exception as e:
        raise STTUnavailable(f"Could not load STT model '{config.WREN_STT_MODEL}': {e}") from e
    return _model


def transcribe(wav_bytes: bytes) -> str:
    segments, _info = _load().transcribe(io.BytesIO(wav_bytes))
    return " ".join(segment.text.strip() for segment in segments).strip()


def reset() -> None:
    """Drop the cached model. Tests use this; so would a model change at runtime."""
    global _model
    _model = None
