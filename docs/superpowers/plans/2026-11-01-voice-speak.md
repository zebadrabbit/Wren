# Voice Speak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /voice?speak=1` returns the reply as a Piper-synthesized WAV so the hardware voice device can answer out loud.

**Architecture:** A new `wren/tts.py` mirrors `wren/stt.py` (lazy, optional, `TTSUnavailable` → 503). In `http_plugin.py` the body of `_dispatch` becomes `_handle(...) -> (payload, status)` so the voice route can read the replies and return audio instead of JSON when the `speak` flag is set. Nothing without `speak` changes.

**Tech Stack:** Python 3.11+, aiohttp, piper-tts ≥ 1.3 (`PiperVoice.load`, `synthesize_wav`), stdlib `wave`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-01-voice-speak-design.md`

## Global Constraints

- `piper` is imported only inside `tts._load()`; an install without voice must import nothing new (spec D2).
- `WREN_TTS_VOICE` is `.env`-only and never added to `config.SETTABLE` (spec D3, CLAUDE.md rule 7).
- Synthesis runs in `asyncio.to_thread` (spec D7).
- `/message` and `/voice` without `speak` are byte-for-byte unchanged (spec D4); every existing test in `tests/test_http_surface.py` must still pass.
- Response codes with `speak=1` are exactly the spec D6 table: 200 `audio/wav`, 204, or the JSON error that would have been returned anyway; TTS failures are 503 (`TTSUnavailable`) or 500 `{"error": "speech synthesis failed"}`.
- Spoken text is `" ".join(payload["replies"])`, untouched (spec D5).
- Run tests from the repo root with `WREN_DB` pointed at a scratch file (CLAUDE.md rule 6): `WREN_DB=$(mktemp -d)/scratch.db python -m pytest ...`. On this Windows box the interpreter is the scratch venv `C:\Users\broca\AppData\Local\Temp\claude\C--Users-broca-OneDrive-Documents-PlatformIO-Projects-WrenAI\03559495-aee0-4cc8-8794-9a0ac464dd21\scratchpad\wrenvenv\Scripts\python` (has requirements.txt, piper-tts, tzdata).

## Review Focus

1. `speak=1&dry_run=1` must still return audio (and run inside the snapshot) — pinned in Task 2.
2. Replies with non-ASCII (em dash, emoji) must reach `tts.synthesize` unchanged, not crash on encoding — pinned in Task 2.
3. `speak=true` / `speak=yes` must be accepted like `dry_run` is — pinned in Task 2.
4. A configured voice path whose file has gone missing must be a 503, not a 500 traceback — pinned in Task 1.
5. STT 503 with `speak=1` must still be the JSON 503, not a 204 or an empty audio body — pinned in Task 2.

---

### Task 1: `wren/tts.py` and `WREN_TTS_VOICE`

**Files:**
- Create: `wren/tts.py`
- Create: `tests/test_tts.py`
- Modify: `wren/config.py:105` (add one line after `WREN_STT_COMPUTE`)

**Interfaces:**
- Consumes: `config.WREN_TTS_VOICE: str` (new, default `""`).
- Produces: `tts.TTSUnavailable(RuntimeError)`, `tts.synthesize(text: str) -> bytes` (a complete WAV), `tts.reset() -> None`.

- [ ] **Step 1: Add the setting**

In `wren/config.py` after line 105 (`WREN_STT_COMPUTE = ...`):

```python
# path to a Piper voice .onnx (its .json sits beside it); unset = /voice?speak=1 returns 503
WREN_TTS_VOICE = os.environ.get("WREN_TTS_VOICE", "")
```

- [ ] **Step 2: Write the failing tests**

`tests/test_tts.py`, same env preamble and `fresh` fixture shape as `tests/test_stt.py` (lines 1-15 there). A fake `piper` module stands in for the real one:

```python
def _fake_piper(record=None, raise_on_load=None):
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
```

Tests (each sets `config.WREN_TTS_VOICE` via `monkeypatch.setattr` to a `tmp_path / "v.onnx"` that it has `touch()`ed, unless stated otherwise):

- `test_unavailable_when_voice_unset`: `WREN_TTS_VOICE = ""` → `pytest.raises(tts.TTSUnavailable)`, message contains `"WREN_TTS_VOICE"`.
- `test_unavailable_when_voice_file_missing`: path not created → `TTSUnavailable`, message contains the path. (Review Focus 4.)
- `test_unavailable_when_library_missing`: `monkeypatch.setitem(sys.modules, "piper", None)` → `TTSUnavailable`, message contains `"pip install piper-tts"`.
- `test_unavailable_when_voice_fails_to_load`: `_fake_piper(raise_on_load=RuntimeError("bad onnx"))` → `TTSUnavailable`, message contains `"bad onnx"`.
- `test_synthesize_returns_wav_bytes`: `record = {}`; `out = tts.synthesize("Added milk.")` → `isinstance(out, bytes)`, `out[:4] == b"RIFF"`, `out[8:12] == b"WAVE"`, `record["text"] == "Added milk."`, and `wave.open(io.BytesIO(out)).getframerate() == 22050`.
- `test_voice_is_loaded_once_and_cached`: two `synthesize` calls → `record["loads"] == [str(path)]`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `WREN_DB=$(mktemp -d)/scratch.db python -m pytest tests/test_tts.py -q`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'wren.tts'`.

- [ ] **Step 4: Implement `wren/tts.py`**

Mirror `wren/stt.py` exactly in shape: module docstring, `class TTSUnavailable(RuntimeError)`, `_voice = None`, `_load()`, `synthesize(text: str) -> bytes`, `reset()`.

`_load()` order: if `config.WREN_TTS_VOICE` is empty → `TTSUnavailable("Text-to-speech is unavailable: set WREN_TTS_VOICE to a Piper voice .onnx path")`; if `not os.path.isfile(path)` → `TTSUnavailable(f"... voice file not found: {path}")`; `from piper import PiperVoice` inside `try` → on `ImportError as e` raise `TTSUnavailable(f"Text-to-speech is unavailable: {e}. If it is not installed, run: pip install piper-tts") from e`; `PiperVoice.load(path)` inside `try` → any `Exception as e` → `TTSUnavailable(f"Could not load TTS voice '{path}': {e}") from e`. Log one `logging.info` line on load, as `stt` does. Do not cache a failed load.

`synthesize`: `buf = io.BytesIO()`; `with wave.open(buf, "wb") as wf: _load().synthesize_wav(text, wf)`; `return buf.getvalue()`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `WREN_DB=$(mktemp -d)/scratch.db python -m pytest tests/test_tts.py tests/test_stt.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add wren/tts.py wren/config.py tests/test_tts.py
git commit -m "feat(tts): optional Piper text-to-speech module, WREN_TTS_VOICE"
```

---

### Task 2: `/voice?speak=1`

**Files:**
- Modify: `wren/communication/http_plugin.py:106-134` (`_wants_dry_run`, `_dispatch`) and `:151-175` (`voice`)
- Modify: `tests/test_http_surface.py` (helper at lines 26-37; new tests appended after line 179)

**Interfaces:**
- Consumes: `tts.synthesize`, `tts.TTSUnavailable` from Task 1.
- Produces: `_wants_speech(request) -> bool`; `async _handle(user_id, text, *, source="text", dry=False, files=None, name="web", **extra) -> tuple[dict, int]`; `_dispatch` keeps its current signature and return type.

- [ ] **Step 1: Add a raw-response helper to the test file**

Beside `_request`/`call` (lines 26-37), add `call_raw(method, path, **kw)` that does the same request but returns `(resp.status, resp.headers.get("Content-Type", ""), await resp.read())`.

- [ ] **Step 2: Write the failing tests**

Appended to `tests/test_http_surface.py`, using `_replies(...)` for `core.handle_message`, `stt.transcribe` patched to a lambda, and `tts.synthesize` patched to a recorder `lambda text: (seen.setdefault("text", text), b"RIFFfakeWAVE")[1]`:

- `test_voice_speak_returns_wav`: transcribe → `"add milk"`, replies `("Added milk.", "Anything else?")`; `call_raw("post", "/voice?speak=1", token="good-token", data=b"RIFFfake")` → status 200, content type starts with `"audio/wav"`, body `== b"RIFFfakeWAVE"`, `seen["text"] == "Added milk. Anything else?"`.
- `test_voice_speak_accepts_true_and_yes`: same with `?speak=true` and `?speak=yes` → 200 each. (Review Focus 3.)
- `test_voice_speak_without_flag_is_still_json`: no `speak` → `call(...)` returns 200 and `body["replies"] == ["Added milk.", "Anything else?"]`; `tts.synthesize` not called.
- `test_voice_speak_empty_transcript_is_204`: transcribe → `"   "` → status 204, synthesize not called.
- `test_voice_speak_no_replies_is_204`: `_replies()` with no texts → 204, synthesize not called.
- `test_voice_speak_non_whitelisted_is_403_json`: `monkeypatch.setattr(config, "id_to_name", lambda: {})`, `_replies()` → `call("post", "/voice?speak=1", ...)` status 403, `body["error"] == "user not whitelisted"`, synthesize not called.
- `test_voice_speak_stt_unavailable_is_503_json`: `stt.transcribe` raises `stt.STTUnavailable("not installed")` → 503, `body["error"] == "not installed"`. (Review Focus 5.)
- `test_voice_speak_tts_unavailable_is_503_json`: `tts.synthesize` raises `tts.TTSUnavailable("set WREN_TTS_VOICE")` → 503, `body["error"] == "set WREN_TTS_VOICE"`.
- `test_voice_speak_tts_failure_is_500_json`: `tts.synthesize` raises `RuntimeError("onnx exploded")` → 500, `body["error"] == "speech synthesis failed"`.
- `test_voice_speak_passes_unicode_through`: replies `("Added 🥛 — done.",)` → 200 and `seen["text"] == "Added 🥛 — done."`. (Review Focus 2.)
- `test_voice_speak_with_dry_run_still_speaks`: `?speak=1&dry_run=1` → 200 `audio/wav`. (Review Focus 1.)

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `WREN_DB=$(mktemp -d)/scratch.db python -m pytest tests/test_http_surface.py -q -k speak`
Expected: the `speak` tests FAIL (200 JSON where 204/audio is expected); the rest of the file is untouched.

- [ ] **Step 4: Split `_dispatch` into `_handle` plus a wrapper**

In `http_plugin.py`: move lines 117-133 into `async def _handle(...) -> tuple[dict, int]` with the same keyword signature as `_dispatch`; it returns `({"error": "user not whitelisted"}, 403)` in the branch that currently builds the 403 response, and `(payload, 200)` otherwise. Keep the docstring on `_handle`. `_dispatch` becomes two lines: `payload, status = await _handle(...)` then `return web.json_response(payload, status=status)`.

Add `_wants_speech(request: web.Request) -> bool` next to `_wants_dry_run` with the identical truthiness parse on the `speak` query key.

- [ ] **Step 5: Branch `voice()` on the flag**

At the top of `voice()` compute `speak = _wants_speech(request)`. The STT block is unchanged. Then, in this order (spec D6):

1. empty transcript → `web.Response(status=204)` if `speak`, else the existing JSON.
2. not `speak` → the existing `_dispatch(...)` call, unchanged.
3. `payload, status = await _handle(user_id, transcript, source="voice", name="voice", dry=_wants_dry_run(request), transcript=transcript)`; `status != 200` → `web.json_response(payload, status=status)`.
4. `replies = payload["replies"]`; empty → 204.
5. `from .. import tts` (local import, like `stt`); `wav = await asyncio.to_thread(tts.synthesize, " ".join(replies))` inside `try`: `tts.TTSUnavailable as e` → 503 `{"error": str(e)}`; any other `Exception as e` → `logging.error(f"speech synthesis failed: {e}")` and 500 `{"error": "speech synthesis failed"}`.
6. `return web.Response(body=wav, content_type="audio/wav")`.

- [ ] **Step 6: Run the whole HTTP test file**

Run: `WREN_DB=$(mktemp -d)/scratch.db python -m pytest tests/test_http_surface.py tests/test_tts.py -q`
Expected: all PASS, including every pre-existing test.

- [ ] **Step 7: Commit**

```bash
git add wren/communication/http_plugin.py tests/test_http_surface.py
git commit -m "feat(http): /voice?speak=1 returns the reply as Piper speech"
```

---

### Task 3: Operator docs and the full suite

**Files:**
- Modify: `.env.example:116-123` (Voice block)
- Modify: `requirements.txt:9-10`
- Modify: `README.md:206` (route table row for `/voice`) and `README.md:457-487` (Voice section)

- [ ] **Step 1: `.env.example`**

After line 122 (`# WREN_STT_COMPUTE=int8 ...`) add the block from the spec's Interfaces section verbatim (comment lines starting `# Text-to-speech for /voice?speak=1 ...` through `# WREN_TTS_VOICE=/home/you/piper/en_US-lessac-medium.onnx`). The download command is `python -m piper.download_voices en_US-lessac-medium --data-dir ~/piper` (verified against piper-tts 1.8.0, which accepts `--data-dir`).

- [ ] **Step 2: `requirements.txt`**

After line 10 add:

```
# Optional — text-to-speech for /voice?speak=1 (the hardware voice device). Wren runs fine without it.
# piper-tts>=1.3
```

- [ ] **Step 3: README**

In the route table (line 206) change the `/voice` row's Returns cell to: `` `{"transcript": "...", "replies": [...]}`; with `?speak=1`, the reply as `audio/wav` (204 when there is nothing to say) ``. In the Voice section (after line 482's paragraph on tuning STT) add one paragraph: a device with a speaker calls `/voice?speak=1` and gets the reply synthesized on the Wren host by Piper; install with `pip install piper-tts`, download a voice as in `.env.example`, set `WREN_TTS_VOICE`, restart. Without it that request returns 503 and the plain `/voice` is unaffected.

- [ ] **Step 4: Run the full suite**

Run: `WREN_DB=$(mktemp -d)/scratch.db python -m pytest -q`
Expected: all PASS (the count before this work was 1404 per the last commit message; expect that plus the new tests).

- [ ] **Step 5: Commit**

```bash
git add .env.example requirements.txt README.md
git commit -m "docs: WREN_TTS_VOICE, piper-tts, /voice?speak=1"
```

- [ ] **Step 6: Deploy note (manual, on the Wren host)**

`pip install piper-tts`, download a voice, set `WREN_TTS_VOICE` in `.env`, restart Wren, then from any LAN machine:

```bash
curl -s -o reply.wav -w '%{http_code} %{content_type}\n' "http://<wren-host>:8787/voice?speak=1&dry_run=1" \
  -H "Authorization: Bearer $WREN_TOKEN" -H 'Content-Type: audio/wav' --data-binary @some16k.wav
```

Expected: `200 audio/wav` and a playable `reply.wav`.
