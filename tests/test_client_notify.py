"""Guards the notification-body sanitiser in the desktop voice client.

The client's real dependencies (sounddevice, numpy, pynput) are intentionally
NOT installed on the Wren server — see client/requirements.txt — so they are
stubbed here just far enough to import the module.
"""
import os, sys, types, pathlib
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDERS", "lmstudio")
os.environ.setdefault("LMSTUDIO_BASE_URL", "http://test")
os.environ.setdefault("LMSTUDIO_MODEL", "test-model")

import importlib.util
import pytest


class _PermissiveModule(types.ModuleType):
    """Any attribute resolves to a fresh class — enough for module-level type
    annotations like `sd.InputStream | None` to evaluate at import."""

    def __getattr__(self, name):
        return type(name, (), {})


def _load_client():
    for name in ("sounddevice", "numpy"):
        sys.modules.setdefault(name, _PermissiveModule(name))
    pynput = types.ModuleType("pynput")
    kb = types.ModuleType("pynput.keyboard")
    kb.Key = types.SimpleNamespace(f9=object(), f12=object())
    kb.KeyCode = types.SimpleNamespace(from_char=lambda c: ("keycode", c))
    kb.Listener = object
    pynput.keyboard = kb
    sys.modules.setdefault("pynput", pynput)
    sys.modules.setdefault("pynput.keyboard", kb)

    path = pathlib.Path(__file__).resolve().parent.parent / "client" / "wren_hotkey.py"
    spec = importlib.util.spec_from_file_location("wren_hotkey", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _load_client()


def test_one_line_collapses_newlines():
    # a literal newline inside an AppleScript string is a syntax error, so a
    # multi-line reply would silently break notifications on macOS
    assert "\n" not in client._one_line("line one\nline two\nline three")
    assert client._one_line("line one\nline two") == "line one line two"


def test_one_line_collapses_runs_of_whitespace():
    assert client._one_line("a   \t  b\n\n  c") == "a b c"


def test_one_line_truncates_long_bodies():
    out = client._one_line("x" * 5000)
    assert len(out) == client.NOTIFY_MAX_CHARS
    assert out.endswith("…")


def test_one_line_leaves_short_text_alone():
    assert client._one_line("Saved.") == "Saved."


def test_one_line_handles_empty():
    assert client._one_line("") == ""


@pytest.mark.parametrize("hotkey,expected_kind", [("f9", "key"), ("a", "keycode")])
def test_resolve_hotkey(monkeypatch, hotkey, expected_kind):
    monkeypatch.setattr(client, "HOTKEY", hotkey)
    resolved = client._resolve_hotkey()
    if expected_kind == "keycode":
        assert resolved == ("keycode", "a")
    else:
        assert resolved is not None


def test_resolve_hotkey_rejects_nonsense(monkeypatch):
    monkeypatch.setattr(client, "HOTKEY", "not_a_key")
    with pytest.raises(SystemExit, match="not a key name"):
        client._resolve_hotkey()
