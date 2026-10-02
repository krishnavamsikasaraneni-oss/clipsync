"""Settings and paired-device storage, kept as JSON in %APPDATA%\\ClipSync."""
import json
import logging
import os
import sys
import threading
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "ClipSync"
SERVICE_TYPE = "_clipsync._tcp.local."
DEFAULT_PORT = 47821
PROTOCOL_VERSION = 1
DEFAULT_SHORTCUT = "ctrl+alt+z"


def app_dir() -> Path:
    override = os.environ.get("CLIPSYNC_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        base = Path.home() / ".config" / APP_NAME.lower()
    base.mkdir(parents=True, exist_ok=True)
    return base


def setup_logging(role: str) -> logging.Logger:
    log = logging.getLogger("clipsync")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(app_dir() / f"{role}.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if sys.stderr is not None:
        log.addHandler(logging.StreamHandler())
    return log


class Config:
    """Thread-safe JSON settings file."""

    def __init__(self, role: str, defaults: dict, path: Path | None = None):
        self.path = path or (app_dir() / f"{role}.json")
        self._lock = threading.RLock()
        self.data = dict(defaults)
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logging.getLogger("clipsync").warning("Settings file unreadable, using defaults")
        if not self.data.get("device_id"):
            self.data["device_id"] = uuid.uuid4().hex
        self.save()

    def get(self, key, default=None):
        with self._lock:
            return self.data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.data[key] = value
            self.save()

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
