#!/usr/bin/env python3
"""Wren desktop voice client.

Hold a global hotkey, talk, release. The audio goes to your Wren over the LAN,
gets transcribed there, and the reply arrives as a native notification.

    WREN_URL=http://wren.local:8787 WREN_TOKEN=secret python wren_hotkey.py

Deliberately not a tray app: a background process with a global hotkey is the
whole requirement, and a tray icon costs another dependency to draw a picture.
"""
import base64
import io
import os
import pathlib
import subprocess
import sys
import threading
import wave

import httpx
import numpy as np
import sounddevice as sd
from pynput import keyboard

URL = os.environ.get("WREN_URL", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("WREN_TOKEN", "")
HOTKEY = os.environ.get("WREN_HOTKEY", "f9").lower()
SAMPLE_RATE = 16_000  # what whisper wants; resampling later would only lose data
TIMEOUT = 120.0       # transcription + a local LLM round-trip is not fast

_frames: list = []
_stream: sd.InputStream | None = None
_lock = threading.Lock()


NOTIFY_MAX_CHARS = 300


def _one_line(text: str) -> str:
    """Notification bodies must be a single line: a literal newline inside an
    AppleScript string is a syntax error, and long replies get truncated by
    every notification daemon anyway. The full text still goes to stdout."""
    collapsed = " ".join(text.split())
    if len(collapsed) > NOTIFY_MAX_CHARS:
        collapsed = collapsed[: NOTIFY_MAX_CHARS - 1] + "…"
    return collapsed


def _notify(title: str, body: str) -> None:
    """Best-effort native notification. Always echoes to the console, because
    every desktop notification system fails silently somewhere."""
    print(f"{title}: {body}", flush=True)
    body = _one_line(body)
    try:
        if sys.platform.startswith("linux"):
            # "--" terminator: a reply starting with "-" would otherwise be
            # parsed by GLib as an option instead of as the message body
            subprocess.run(["notify-send", "--", title, body], check=False, timeout=5)
        elif sys.platform == "darwin":
            escaped = body.replace("\\", "\\\\").replace('"', '\\"')
            title_escaped = title.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{escaped}" with title "{title_escaped}"'],
                check=False, timeout=5,
            )
        elif sys.platform == "win32":
            escaped = body.replace("'", "''")
            title_escaped = title.replace("'", "''")
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms')"
                 " > $null; [System.Windows.Forms.MessageBox]::Show("
                 f"'{escaped}', '{title_escaped}') > $null"],
                check=False, timeout=5,
            )
    except Exception as e:
        print(f"(notification failed: {e})", flush=True)


def _wav_bytes(frames: list) -> bytes:
    audio = np.concatenate(frames, axis=0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


def _start_recording() -> None:
    global _stream
    with _lock:
        if _stream is not None:
            return
        _frames.clear()

        def callback(indata, _frames_count, _time, status):
            if status:
                print(f"(audio status: {status})", flush=True)
            _frames.append(indata.copy())

        try:
            _stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback
            )
            _stream.start()
        except Exception as e:
            # a PortAudio failure must not kill the client, and a half-opened
            # stream must not be left assigned or every later recording wedges
            if _stream is not None:
                try:
                    _stream.close()
                except Exception:
                    pass
                _stream = None
            _notify("Wren", f"Microphone unavailable: {e}")
            return
        print("● recording…", flush=True)


def _stop_and_send() -> None:
    global _stream
    with _lock:
        if _stream is None:
            return
        _stream.stop()
        _stream.close()
        _stream = None
        frames = list(_frames)
        _frames.clear()

    if not frames:
        return
    audio = _wav_bytes(frames)
    # under ~0.3s is a stray keypress, not speech
    if len(audio) < SAMPLE_RATE * 2 * 0.3:
        print("(too short, ignored)", flush=True)
        return

    print("→ sending…", flush=True)
    try:
        resp = httpx.post(
            f"{URL}/voice",
            content=audio,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "audio/wav",
            },
            timeout=TIMEOUT,
        )
    except Exception as e:
        _notify("Wren", f"Couldn't reach Wren: {e}")
        return

    if resp.status_code != 200:
        detail = resp.json().get("error", resp.text) if resp.headers.get(
            "content-type", ""
        ).startswith("application/json") else resp.text
        _notify("Wren", f"Error {resp.status_code}: {detail}")
        return

    data = resp.json()
    transcript = data.get("transcript", "")
    replies = data.get("replies", [])
    files = data.get("files", [])
    if transcript:
        print(f'  heard: "{transcript}"', flush=True)
    if files:
        # a file-only response (e.g. export_notes) used to report "nothing to
        # say" and throw the payload away
        for f in files:
            path = pathlib.Path.home() / "Downloads" / f["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(f["data"]))
            print(f"  saved {path}", flush=True)
        replies = replies + [f"Saved {', '.join(f['filename'] for f in files)} to ~/Downloads"]
    if not replies:
        _notify("Wren", f'Nothing to say about "{transcript}"' if transcript
                else "Didn't catch that.")
        return
    _notify("Wren", "\n".join(replies))


def _resolve_hotkey():
    """'f9' -> keyboard.Key.f9, 'a' -> KeyCode for 'a'."""
    if hasattr(keyboard.Key, HOTKEY):
        return getattr(keyboard.Key, HOTKEY)
    if len(HOTKEY) == 1:
        return keyboard.KeyCode.from_char(HOTKEY)
    raise SystemExit(
        f"WREN_HOTKEY='{HOTKEY}' is not a key name. Use e.g. f9, f12, scroll_lock, "
        f"or a single character."
    )


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set WREN_TOKEN to a token listed in Wren's WREN_TOKENS.")
    target = _resolve_hotkey()
    print(f"Wren voice client → {URL}\nHold {HOTKEY.upper()} to talk. Ctrl-C to quit.",
          flush=True)

    def on_press(key):
        if key == target:
            _start_recording()

    def on_release(key):
        if key == target:
            # off the listener thread: pynput blocks further key events while a
            # callback runs, so a slow round-trip would freeze the keyboard hook
            threading.Thread(target=_stop_and_send, daemon=True).start()

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    main()
