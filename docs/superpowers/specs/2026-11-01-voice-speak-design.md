# Voice Speak — Design

`POST /voice?speak=1` returns the reply as speech instead of JSON, so a
device with a speaker and no screen can answer out loud. Text-to-speech
runs on this machine with Piper, the way speech-to-text already runs here
with faster-whisper. Without `speak`, `/voice` is unchanged.

## Why

The hardware voice device (ESP32-S3, wake word, microphone, amplifier —
spec in the WrenAI firmware repo) has no way to show text. It can play a
WAV. Today `/voice` answers `{"transcript", "replies", "files"}`; the device
would need a second service to turn `replies` into audio, a second
round-trip, and a second token. One flag on the route it already calls is
the whole requirement.

## Verified constraints (checked 2026-11-01)

1. `/voice` (`http_plugin.py`) reads the body, calls `stt.transcribe` in a
   thread, and hands the transcript to `_dispatch(user_id, transcript,
   source="voice", name="voice", dry=..., transcript=...)`, which builds the
   payload dict and returns `web.json_response(payload)`; it also returns a
   403 JSON when the user is not whitelisted. `/message` uses the same
   `_dispatch`.
2. `stt.py` is the pattern for an optional native dependency: lazy `_load()`,
   `STTUnavailable` on ImportError or model-load failure, 503 from the
   route, `reset()` for tests, settings read from `config` at import.
3. Hard rule 7: model names are `.env`-only and never `SETTABLE`.
4. `tests/test_http_surface.py` already monkeypatches `stt.transcribe` and
   `core.handle_message` and asserts on the JSON; the new tests follow
   that shape.
5. piper-tts 1.3+: `PiperVoice.load(onnx_path)` (reads the `.json` beside
   it) and `voice.synthesize_wav(text, wave_file)` writes a complete PCM WAV
   at the voice's own rate (16 or 22.05 kHz, mono, int16). CPU-bound,
   hundreds of ms per sentence.

## Decisions

**D1 — A query flag on `/voice`, not a `/tts` route.** `speak=1` (same
truthiness parse as `dry_run`) switches the response body from JSON to
`audio/wav`. One request, one token, one place in the device firmware.
Combines with `dry_run=1` exactly as the JSON form does.

**D2 — `wren/tts.py` mirrors `wren/stt.py`.** `TTSUnavailable`, lazy
`_load()` of one `PiperVoice`, `synthesize(text) -> bytes` returning the
WAV, `reset()`. The import of `piper` happens inside `_load` so an install
without voice pays nothing, and an ImportError's real message is kept (a
missing onnxruntime shows up as ImportError too).

**D3 — `WREN_TTS_VOICE` is the path to the Piper `.onnx` file, `.env`-only.**
Unset or missing file → `TTSUnavailable` → 503, with the message saying
what to set. No default voice: a path on someone else's disk is not a
default. Not `SETTABLE` (rule 7).

**D4 — `_dispatch` splits into `_handle` and a wrapper.** `_handle(...)
-> tuple[dict, int]` is the current body returning `(payload, status)`
including the 403 case; `_dispatch` becomes `web.json_response(*_handle(...))`
so `/message` and plain `/voice` are byte-for-byte unchanged. `voice()` with
`speak` calls `_handle` directly and reads `payload["replies"]`.

**D5 — What is spoken.** `" ".join(payload["replies"])`. Piper does its own
sentence splitting. Files and cards are not spoken; a file-only reply is
"nothing to say". Markdown is not stripped (deliberately not in this slice).

**D6 — Response codes with `speak=1`.**

| Case | Response |
|---|---|
| transcript empty, or `_handle` returned 200 with no replies | `204 No Content` |
| `_handle` returned non-200 (403 today) | that JSON, unchanged |
| STT unavailable / failed | 503 / 500 JSON, unchanged |
| TTS unavailable | `503 {"error": "<TTSUnavailable message>"}` |
| synthesis raised | `500 {"error": "speech synthesis failed"}`, logged |
| otherwise | `200`, `Content-Type: audio/wav`, body is the WAV |

The device treats 200 as play, 204 as blink, anything else as an error
with the JSON logged.

**D7 — Synthesis runs in `asyncio.to_thread`,** for the same reason
transcription does: it is CPU-bound for hundreds of milliseconds and the
event loop is shared with every door.

## Interfaces

```python
# wren/tts.py
class TTSUnavailable(RuntimeError): ...
def synthesize(text: str) -> bytes       # complete WAV file
def reset() -> None

# wren/config.py
WREN_TTS_VOICE = os.environ.get("WREN_TTS_VOICE", "")   # path to .onnx; .json beside it

# wren/communication/http_plugin.py
def _wants_speech(request) -> bool                       # ?speak=1|true|yes
async def _handle(user_id, text, *, source, dry, files, name, **extra) -> tuple[dict, int]
async def _dispatch(...) -> web.Response                 # web.json_response(*await _handle(...))
```

`.env.example` gains, under the Voice block:

```
# Text-to-speech for /voice?speak=1 (the hardware voice device). Runs here.
# Needs: pip install piper-tts, then a voice:
#   python -m piper.download_voices en_US-lessac-medium --data-dir ~/piper
# WREN_TTS_VOICE=/home/you/piper/en_US-lessac-medium.onnx
```

`requirements.txt` gains a commented optional line beside faster-whisper.

## Flow

```
POST /voice?speak=1  (body: 16 kHz WAV)
  → stt.transcribe (thread)            → "" → 204
  → _handle(..., source="voice")       → status != 200 → that JSON
  → replies == []                      → 204
  → tts.synthesize(" ".join(replies))  (thread) → 200 audio/wav
```

## Error handling

Covered by D6. `TTSUnavailable` is raised from `_load`, so the first
`speak=1` request after boot is the one that reports a bad path; later
ones re-attempt the load (the failed voice is not cached), matching `stt`.

## Testing

`tests/test_http_surface.py`, in its existing shape (`stt.transcribe` and
`core.handle_message` monkeypatched, `tts.synthesize` likewise):

- `speak=1` → 200, `Content-Type: audio/wav`, body equals what
  `tts.synthesize` returned, and it was called with the replies joined by a
  space.
- `speak=1` with an empty transcript → 204, `tts.synthesize` not called.
- `speak=1` with replies `[]` → 204.
- `speak=1` with `tts.synthesize` raising `TTSUnavailable` → 503 JSON.
- `speak=1` for a non-whitelisted user → 403 JSON, synthesis not called.
- without `speak` → the existing JSON tests still pass unchanged.

`tests/test_tts.py`, in the shape of `tests/test_stt.py` (a fake `piper`
module injected into `sys.modules`):

- `WREN_TTS_VOICE` unset → `TTSUnavailable`.
- `piper` missing → `TTSUnavailable` whose message contains the ImportError
  text.
- a fake voice whose `synthesize_wav` writes known bytes → `synthesize`
  returns a bytes object starting with `RIFF`, and the voice was loaded
  once across two calls.

## Deliberately not in this slice

- A standalone `/tts` route, or speech for `/message`.
- Voice selection per request, speed/pitch, SSML.
- Stripping markdown or emoji before synthesis.
- Returning the transcript or replies alongside the audio (headers or
  multipart).
- Caching synthesized audio.
- Streaming audio while synthesis continues.
