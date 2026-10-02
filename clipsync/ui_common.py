"""Shared Qt bits: icons drawn in code, styling, single-instance guard."""
from PySide6.QtCore import QLockFile, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

from .config import app_dir

GREEN = "#22a35a"
GREY = "#8a8f98"
AMBER = "#e0a100"
RED = "#d64545"
BLUE = "#2f6fde"

STYLE = """
QWidget { font-family: 'Segoe UI', sans-serif; font-size: 11pt; }
QPushButton { padding: 8px 14px; border-radius: 6px; border: 1px solid #c9ced6; background: #f4f6f9; }
QPushButton:hover { background: #e8ecf2; }
QPushButton:pressed { background: #dde3ec; }
QPushButton:disabled { color: #9aa1ab; }
QPushButton#primary { background: #2f6fde; color: white; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #285fc0; }
QPushButton#mic { font-size: 13pt; font-weight: 600; min-width: 120px; background: #2f6fde; color: white; border: none; }
QPushButton#mic[recording="true"] { background: #d64545; }
QPushButton#mode { min-width: 110px; }
QPushButton#mode:checked { background: #2f6fde; color: white; border: none; font-weight: 600; }
QPushButton#pause { font-weight: 600; min-width: 130px; }
QPushButton#pause[paused="true"] { background: #e0a100; color: white; border: none; }
QPlainTextEdit, QListWidget, QLineEdit { border: 1px solid #c9ced6; border-radius: 6px; padding: 6px; background: white; }
QLabel#hint { color: #6b7280; font-size: 9.5pt; }
QLabel#code { font-size: 32pt; font-weight: 700; letter-spacing: 6px; color: #2f6fde; }
QListWidget::item { padding: 6px; border-bottom: 1px solid #eef0f3; }
QListWidget::item:hover { background: #f1f5fd; color: #111; }
QListWidget::item:selected { background: #dbe7fb; color: #111; }
"""


def dot_icon(color: str, letter: str = "C", size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.setPen(QColor("white"))
    f = QFont("Segoe UI", int(size * 0.42))
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, letter)
    p.end()
    return QIcon(pm)


def status_dot(color: str) -> str:
    return f'<span style="color:{color}; font-size:14pt;">&#9679;</span>'


def single_instance(role: str) -> QLockFile | None:
    """Returns a held lock, or None if the app is already running."""
    lock = QLockFile(str(app_dir() / f"{role}.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        return None
    return lock
