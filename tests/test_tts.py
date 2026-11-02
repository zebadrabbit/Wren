import io, os, sys, types, wave, pytest
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("WREN_OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

from wren import config
from wren import tts


@pytest.fixture(autouse=True)
def fresh():
    tts.reset()
    yield
    tts.reset()


@pytest.fixture
def voice_path(tmp_path, monkeypatch):
    """An existing (empty) .onnx so the path check passes and the fake loads."""
    path = tmp_path / "v.onnx"
    path.touch()
    monkeypatch.setattr(config, "WREN_TTS_VOICE", str(path))
    return str(path)


def _fake_piper(record=None, raise_on_load=None):
    """A stand-in piper so tests never load a real voice."""
    module = types.ModuleType("piper")

    class PiperVoice:
        @staticmethod
        def load(path):
            if raise_on_load:
                raise raise_on_load
            if record is not None:
                record.setdefault("loads", []).append(path)
            return PiperVoice()

        def synthesize_wav(self, text, wav_file):
            if record is not None:
                record["text"] = text
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x01\x00" * 100)

    module.PiperVoice = PiperVoice
    return module


def test_unavailable_when_voice_unset(monkeypatch):
    monkeypatch.setattr(config, "WREN_TTS_VOICE", "")
    monkeypatch.setitem(sys.modules, "piper", _fake_piper())
    with pytest.raises(tts.TTSUnavailable) as e:
        tts.synthesize("hi")
    assert "WREN_TTS_VOICE" in str(e.value)


def test_unavailable_when_voice_file_missing(tmp_path, monkeypatch):
    missing = str(tmp_path / "gone.onnx")
    monkeypatch.setattr(config, "WREN_TTS_VOICE", missing)
    monkeypatch.setitem(sys.modules, "piper", _fake_piper())
    with pytest.raises(tts.TTSUnavailable) as e:
        tts.synthesize("hi")
    assert missing in str(e.value)


def test_unavailable_when_library_missing(voice_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "piper", None)
    with pytest.raises(tts.TTSUnavailable) as e:
        tts.synthesize("hi")
    assert "pip install piper-tts" in str(e.value)


def test_unavailable_when_voice_fails_to_load(voice_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "piper",
                        _fake_piper(raise_on_load=RuntimeError("bad onnx")))
    with pytest.raises(tts.TTSUnavailable) as e:
        tts.synthesize("hi")
    assert "bad onnx" in str(e.value)


def test_synthesize_returns_wav_bytes(voice_path, monkeypatch):
    record = {}
    monkeypatch.setitem(sys.modules, "piper", _fake_piper(record=record))
    out = tts.synthesize("Added milk.")
    assert isinstance(out, bytes)
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WAVE"
    assert record["text"] == "Added milk."
    with wave.open(io.BytesIO(out)) as wf:
        assert wf.getframerate() == 22050


def test_voice_is_loaded_once_and_cached(voice_path, monkeypatch):
    record = {}
    monkeypatch.setitem(sys.modules, "piper", _fake_piper(record=record))
    tts.synthesize("one")
    tts.synthesize("two")
    assert record["loads"] == [voice_path]
