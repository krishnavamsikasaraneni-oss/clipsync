"""Offline English speech-to-text with faster-whisper. Free, no API key.

The default model (base.en) ships inside the app folder, so it works with no
internet from the first run. Other model sizes download once if you pick them
in Settings, and are cached in %APPDATA%\\ClipSync\\models.
"""
import logging
import sys
import threading
from pathlib import Path

from .config import app_dir

log = logging.getLogger("clipsync")
SAMPLE_RATE = 16000
MODELS = {"tiny.en": "Fastest, less accurate", "base.en": "Balanced (recommended)",
          "small.en": "Most accurate, slower"}


def bundled_model(name: str) -> str | None:
    """The release zip ships base.en next to the .exe, so no download is needed."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    path = base / "models" / name
    return str(path) if (path / "model.bin").exists() else None


class Recorder:
    def __init__(self):
        self._chunks = []
        self._stream = None
        self.level = 0.0

    def start(self):
        import numpy as np
        import sounddevice as sd
        self._chunks = []

        def cb(indata, frames, t, status):
            self._chunks.append(indata.copy())
            self.level = float(np.sqrt(np.mean(indata ** 2)))

        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=cb)
        self._stream.start()

    def stop(self):
        import numpy as np
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._chunks:
            return np.zeros(0, dtype="float32")
        return np.concatenate(self._chunks)[:, 0]

    @property
    def seconds(self) -> float:
        return sum(len(c) for c in self._chunks) / SAMPLE_RATE


class Transcriber:
    def __init__(self, model_name="base.en"):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                source = bundled_model(self.model_name) or self.model_name
                log.info("Loading speech model %s", source)
                self._model = WhisperModel(source, device="cpu", compute_type="int8",
                                           download_root=str(app_dir() / "models"))
        return self._model

    def set_model(self, name):
        with self._lock:
            if name != self.model_name:
                self.model_name, self._model = name, None

    @property
    def loaded(self):
        return self._model is not None

    def transcribe(self, audio) -> str:
        if len(audio) < SAMPLE_RATE * 0.3:
            return ""
        model = self.load()
        segments, _ = model.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                       condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()
