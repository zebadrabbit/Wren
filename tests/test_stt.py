import os, sys, types, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import config
from wren import stt


@pytest.fixture(autouse=True)
def fresh():
    stt.reset()
    yield
    stt.reset()


class _Segment:
    def __init__(self, text):
        self.text = text


def _fake_module(segments, *, record=None, raise_on_init=None):
    """A stand-in faster_whisper so tests never download a real model."""
    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, name, device=None, compute_type=None):
            if raise_on_init:
                raise raise_on_init
            if record is not None:
                record.update(name=name, device=device, compute_type=compute_type)

        def transcribe(self, _audio):
            return iter(segments), object()

    module.WhisperModel = WhisperModel
    return module


def test_unavailable_when_library_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(stt.STTUnavailable) as e:
        stt.transcribe(b"RIFF")
    assert "pip install faster-whisper" in str(e.value)


def test_unavailable_when_model_fails_to_load(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_module([], raise_on_init=RuntimeError("no such model")))
    with pytest.raises(stt.STTUnavailable) as e:
        stt.transcribe(b"RIFF")
    assert "no such model" in str(e.value)


def test_transcribe_joins_segments(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_module([_Segment(" remind me "), _Segment(" at six ")]))
    assert stt.transcribe(b"RIFF") == "remind me at six"


def test_transcribe_empty_audio_gives_empty_string(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_module([]))
    assert stt.transcribe(b"RIFF") == ""


def test_model_is_configured_from_env(monkeypatch):
    record = {}
    monkeypatch.setattr(config, "WREN_STT_MODEL", "small.en")
    monkeypatch.setattr(config, "WREN_STT_DEVICE", "cpu")
    monkeypatch.setattr(config, "WREN_STT_COMPUTE", "float32")
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_module([_Segment("hi")], record=record))
    stt.transcribe(b"RIFF")
    assert record == {"name": "small.en", "device": "cpu", "compute_type": "float32"}


def test_model_is_loaded_once_and_cached(monkeypatch):
    loads = []

    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *a, **kw):
            loads.append(1)

        def transcribe(self, _audio):
            return iter([_Segment("hi")]), object()

    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    stt.transcribe(b"RIFF")
    stt.transcribe(b"RIFF")
    assert len(loads) == 1
