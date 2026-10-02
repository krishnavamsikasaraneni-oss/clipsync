"""Checks that only make sense on Windows (run by the GitHub Actions build)."""
import os
import sys
import wave

import pytest

from clipsync.winsys import GlobalHotkey, parse_shortcut
from clipsync.typer import char_delay

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_parse_shortcut():
    assert parse_shortcut("ctrl+alt+z") == (0x2 | 0x1, ord("Z"))
    assert parse_shortcut("Ctrl + Shift + F9")[1] == 0x78
    with pytest.raises(ValueError):
        parse_shortcut("z")
    with pytest.raises(ValueError):
        parse_shortcut("ctrl+banana")


def test_word_speed():
    assert char_delay(60) == pytest.approx(0.2)   # 60 wpm = 300 chars/min
    assert char_delay(300) == pytest.approx(0.04)
    assert char_delay(0) < 0.01                   # instant


@win_only
def test_hotkey_conflict_is_detected():
    first = GlobalHotkey("ctrl+alt+shift+f12", lambda: None)
    if not first.ok:
        pytest.skip("this machine can't register hotkeys (no desktop session)")
    try:
        second = GlobalHotkey("ctrl+alt+shift+f12", lambda: None)
        assert not second.ok and "already used" in second.error
    finally:
        first.stop()


@pytest.mark.skipif(not os.environ.get("CLIPSYNC_VOICE_WAV"), reason="needs a test recording")
def test_speech_to_text():
    import numpy as np
    from clipsync.voice import SAMPLE_RATE, Transcriber, bundled_model
    assert bundled_model("base.en"), "models/base.en is missing"
    with wave.open(os.environ["CLIPSYNC_VOICE_WAV"]) as w:
        sr, n = w.getframerate(), w.getnframes()
        audio = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
        if w.getnchannels() == 2:
            audio = audio[::2]
    audio = np.interp(np.arange(0, len(audio), sr / SAMPLE_RATE), np.arange(len(audio)), audio).astype(np.float32)
    text = Transcriber("base.en").transcribe(audio).lower()
    print("heard:", text)
    assert "computer" in text and "test" in text


def test_typer_stops_when_focus_leaves(monkeypatch):
    """Type mode stops if you click away from the web app, and reports how far it got."""
    import types
    import time
    from clipsync.typer import Typer
    typed = []

    class Controller:
        def type(self, ch): typed.append(ch)
        def press(self, k): typed.append("\n")
        def release(self, k): pass

    fake = types.ModuleType("pynput.keyboard")
    fake.Controller, fake.Key = Controller, types.SimpleNamespace(enter="enter", tab="tab")
    monkeypatch.setitem(sys.modules, "pynput", types.ModuleType("pynput"))
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake)
    calls, result = {"n": 0}, []

    def guard():
        calls["n"] += 1
        return calls["n"] <= 5

    t = Typer()
    t.type("hello world", 0, on_done=lambda *a: result.append(a), start_delay=0, guard=guard)
    for _ in range(50):
        if result:
            break
        time.sleep(0.05)
    assert "".join(typed) == "hello"
    assert result == [(False, 5, "focus")]

    typed.clear(); result.clear()
    t.type("line1\r\nline2", 0, on_done=lambda *a: result.append(a), start_delay=0)
    for _ in range(50):
        if result:
            break
        time.sleep(0.05)
    assert "".join(typed) == "line1\nline2" and result[0][0] is True
