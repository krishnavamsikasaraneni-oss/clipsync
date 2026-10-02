"""ClipSync Receiver - runs on PC 2. Receives clips from PC 1, and has a mic and a
typing box that type into any window on this PC at a speed you choose."""
import sys
import threading

from clipsync.config import DEFAULT_SHORTCUT, Config, setup_logging

ROLE = "receiver"


def selftest():
    import clipsync.link, clipsync.winsys, clipsync.typer, clipsync.voice  # noqa: F401
    import zeroconf, websockets, spake2, numpy, sounddevice, faster_whisper, ctranslate2  # noqa: F401
    import pynput.keyboard  # noqa: F401
    from PySide6 import QtWidgets  # noqa: F401
    from clipsync.voice import Transcriber, bundled_model
    if bundled_model("base.en"):
        t = Transcriber("base.en")
        t.transcribe(numpy.zeros(16000, dtype="float32"))  # proves the speech engine loads
    print("selftest ok")
    return 0


if "--selftest" in sys.argv:
    sys.exit(selftest())

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from clipsync.link import ReceiverCore, local_ipv4s
from clipsync.typer import INSTANT, Typer, normalise
from clipsync.ui_common import AMBER, GREEN, GREY, STYLE, dot_icon, single_instance, status_dot
from clipsync.voice import MODELS, Recorder, Transcriber
from clipsync.winsys import (IS_WIN, GlobalHotkey, focus_window, foreground_window, is_other_app_window,
                             parse_shortcut, pretty_shortcut, set_autostart, window_title)

log = setup_logging(ROLE)
HISTORY_SIZE = 10
SLIDER_INSTANT = 31  # slider 1..30 = 10..300 words/min, 31 = instant


def slider_to_wpm(v):
    return INSTANT if v >= SLIDER_INSTANT else v * 10


def wpm_to_slider(wpm):
    return SLIDER_INSTANT if wpm <= INSTANT else max(1, min(30, round(wpm / 10)))


class Bridge(QObject):
    clip = Signal(str, str)
    status = Signal(list)
    pause = Signal(bool)
    paired = Signal(str)
    pair_failed = Signal()
    pair_code_changed = Signal(str)
    hotkey = Signal()
    typing_done = Signal(bool, int, str)
    voice_text = Signal(str)
    voice_error = Signal(str)
    model_ready = Signal(bool, str)

    def on_clip(self, text, name): self.clip.emit(text, name)
    def on_status(self, names): self.status.emit(names)
    def on_pause(self, p): self.pause.emit(p)
    def on_paired(self, name): self.paired.emit(name)
    def on_pair_failed(self): self.pair_failed.emit()
    def on_pair_code_changed(self, code): self.pair_code_changed.emit(code)

    def __getattr__(self, name):
        if name.startswith("on_"):
            return lambda *a: None
        raise AttributeError(name)


# ------------------------------------------------------------------ settings

class SettingsDialog(QDialog):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("ClipSync - Settings")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)

        # pairing
        box = QGroupBox("Pair a PC")
        b = QVBoxLayout(box)
        self.pair_btn = QPushButton("Pair a new PC")
        self.pair_btn.setObjectName("primary")
        self.pair_btn.clicked.connect(self.start_pairing)
        b.addWidget(self.pair_btn)
        self.code = QLabel("")
        self.code.setObjectName("code")
        self.code.setAlignment(Qt.AlignCenter)
        self.code.hide()
        b.addWidget(self.code)
        self.pair_hint = QLabel("")
        self.pair_hint.setWordWrap(True)
        self.pair_hint.setObjectName("hint")
        b.addWidget(self.pair_hint)
        b.addWidget(QLabel("Paired PCs:"))
        self.paired = QListWidget()
        self.paired.setMaximumHeight(90)
        b.addWidget(self.paired)
        rm = QPushButton("Remove selected PC")
        rm.clicked.connect(self.remove)
        b.addWidget(rm)
        lay.addWidget(box)

        # behaviour
        box2 = QGroupBox("Options")
        o = QVBoxLayout(box2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Pause shortcut:"))
        self.shortcut = QLineEdit(pretty_shortcut(win.cfg.get("shortcut")))
        row.addWidget(self.shortcut)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self.apply_shortcut)
        row.addWidget(apply_btn)
        o.addLayout(row)
        self.autostart = QCheckBox("Start with Windows")
        self.autostart.setChecked(bool(win.cfg.get("autostart")))
        self.autostart.toggled.connect(win.set_autostart)
        o.addWidget(self.autostart)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Speech model:"))
        self.model = QComboBox()
        for name, desc in MODELS.items():
            self.model.addItem(f"{name}  -  {desc}", name)
        self.model.setCurrentIndex(list(MODELS).index(win.cfg.get("model")))
        self.model.currentIndexChanged.connect(lambda i: win.set_model(self.model.itemData(i)))
        row2.addWidget(self.model)
        o.addLayout(row2)
        lay.addWidget(box2)

        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addWidget(close, alignment=Qt.AlignRight)

        win.bridge.paired.connect(self.on_paired)
        win.bridge.pair_code_changed.connect(self.show_code)
        win.bridge.pair_failed.connect(lambda: self.pair_hint.setText(
            self.pair_hint.text().split("<br><br>")[0] + "<br><br>That code was wrong - try again."))
        self.fill()

    def fill(self):
        self.paired.clear()
        for sid, info in self.win.core.paired_devices().items():
            it = QListWidgetItem(info["name"])
            it.setData(256, sid)
            self.paired.addItem(it)
        if self.paired.count() == 0:
            self.paired.addItem("(none yet)")

    def start_pairing(self):
        self.show_code(self.win.core.start_pairing())

    def show_code(self, code):
        self.code.setText(code)
        self.code.show()
        ips = ", ".join(local_ipv4s()) or "unknown"
        self.pair_hint.setText(f"On PC 1, open ClipSync Sender &gt; <b>Pair with PC 2</b>, pick "
                               f"<b>{self.win.core.name}</b> and type this code.<br>"
                               f"If PC 2 isn't listed there, type this PC's IP address: <b>{ips}</b>")

    def on_paired(self, name):
        self.code.hide()
        self.pair_hint.setText(f"&#10004; Paired with <b>{name}</b>. You're all set.")
        self.fill()

    def remove(self):
        it = self.paired.currentItem()
        sid = it.data(256) if it else None
        if not sid:
            return
        if QMessageBox.question(self, "ClipSync", f"Remove {it.text()}? It will need to pair again.") \
                == QMessageBox.Yes:
            self.win.core.unpair(sid)
            self.fill()

    def apply_shortcut(self):
        text = self.shortcut.text().strip().lower()
        try:
            parse_shortcut(text)
        except ValueError as e:
            QMessageBox.warning(self, "ClipSync", str(e))
            return
        if self.win.register_hotkey(text):
            self.win.cfg.set("shortcut", text)
            QMessageBox.information(self, "ClipSync", f"Pause shortcut is now {pretty_shortcut(text)}.")
        else:
            QMessageBox.warning(self, "ClipSync", f"{pretty_shortcut(text)} is already used by another program. "
                                                  "Try a different one.")
            self.win.register_hotkey(self.win.cfg.get("shortcut"))
            self.shortcut.setText(pretty_shortcut(self.win.cfg.get("shortcut")))

    def closeEvent(self, e):
        self.win.core.stop_pairing()
        super().closeEvent(e)


# ------------------------------------------------------------------ main window

class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cfg = Config(ROLE, {"paired": {}, "paused": False, "shortcut": DEFAULT_SHORTCUT, "wpm": 60,
                                 "mode": "paste", "lock_title": "", "autostart": True, "model": "base.en",
                                 "seen_tray_tip": False})
        if self.cfg.get("model") not in MODELS:
            self.cfg.set("model", "base.en")
        self.bridge = Bridge()
        self.core = ReceiverCore(self.cfg, self.bridge)
        self.typer = Typer()
        self.recorder = Recorder()
        self.transcriber = Transcriber(self.cfg.get("model"))
        self.recording = False
        self.target = None   # last other window you used (for the Type into window button)
        self.lock = None     # the web app window that received clips are typed into (Type mode)
        self.queue = []      # received clips waiting to be typed
        self.job = None      # text being typed right now
        self.connected = []
        self.settings = None
        self.hotkey = None

        self.setWindowTitle("ClipSync Receiver")
        self.setWindowIcon(dot_icon(GREEN))
        self.resize(560, 680)
        self.build_ui()
        self.build_tray()

        b = self.bridge
        b.clip.connect(self.on_clip)
        b.status.connect(self.on_status)
        b.pause.connect(self.on_pause)
        b.paired.connect(lambda name: self.statusBar().showMessage(f"Paired with {name}", 4000))
        b.hotkey.connect(self.on_hotkey)
        b.typing_done.connect(self.on_typing_done)
        b.voice_text.connect(self.on_voice_text)
        b.voice_error.connect(self.on_voice_error)
        b.model_ready.connect(self.on_model_ready)

        self.register_hotkey(self.cfg.get("shortcut"), first=True)
        try:
            set_autostart("ClipSync Receiver", bool(self.cfg.get("autostart")))
        except OSError:
            log.exception("Couldn't set autostart")

        self._focus_timer = QTimer(self, interval=300)
        self._focus_timer.timeout.connect(self.track_target)
        self._focus_timer.start()
        self._rec_timer = QTimer(self, interval=100)
        self._rec_timer.timeout.connect(self.update_recording)

        self.core.start()
        self.on_pause(self.core.pause.paused, quiet=True)
        self.on_status([])
        self.preload_model()

    # ---- UI
    def build_ui(self):
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 14, 16, 10)

        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.RichText)
        top.addWidget(self.status, 1)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("pause")
        self.pause_btn.clicked.connect(lambda: self.core.set_paused(not self.core.pause.paused))
        top.addWidget(self.pause_btn)
        gear = QPushButton("⚙ Settings")
        gear.clicked.connect(self.open_settings)
        top.addWidget(gear)
        lay.addLayout(top)

        mode = QHBoxLayout()
        mode.addWidget(QLabel("When a clip arrives:"))
        self.paste_mode_btn = QPushButton("Paste mode")
        self.paste_mode_btn.setToolTip("Put it on the clipboard - you press Ctrl+V")
        self.type_mode_btn = QPushButton("Type mode")
        self.type_mode_btn.setToolTip("Type it out letter by letter into your web app - never pasted")
        self.mode_group = QButtonGroup(self)
        for b in (self.paste_mode_btn, self.type_mode_btn):
            b.setCheckable(True)
            b.setObjectName("mode")
            self.mode_group.addButton(b)
            mode.addWidget(b)
        mode.addStretch()
        lay.addLayout(mode)
        self.paste_mode_btn.clicked.connect(lambda: self.set_mode("paste"))
        self.type_mode_btn.clicked.connect(lambda: self.set_mode("type"))

        self.lock_row = QWidget()
        lr = QHBoxLayout(self.lock_row)
        lr.setContentsMargins(0, 0, 0, 0)
        self.lock_lbl = QLabel()
        self.lock_lbl.setTextFormat(Qt.RichText)
        lr.addWidget(self.lock_lbl, 1)
        self.pick_btn = QPushButton("Pick web app window")
        self.pick_btn.clicked.connect(self.pick_window)
        lr.addWidget(self.pick_btn)
        lay.addWidget(self.lock_row)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#e3e6eb")
        lay.addWidget(line)

        lay.addWidget(QLabel("<b>Type or speak</b>"))
        mid = QHBoxLayout()
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Type here, or press Speak and talk...")
        self.editor.setMinimumHeight(150)
        mid.addWidget(self.editor, 1)
        micbox = QVBoxLayout()
        self.mic_btn = QPushButton("\U0001F3A4  Speak")
        self.mic_btn.setObjectName("mic")
        self.mic_btn.setMinimumHeight(70)
        self.mic_btn.clicked.connect(self.toggle_mic)
        micbox.addWidget(self.mic_btn)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setMaximumHeight(8)
        self.level.hide()
        micbox.addWidget(self.level)
        self.mic_state = QLabel("")
        self.mic_state.setObjectName("hint")
        self.mic_state.setWordWrap(True)
        self.mic_state.setMaximumWidth(140)
        micbox.addWidget(self.mic_state)
        micbox.addStretch()
        mid.addLayout(micbox)
        lay.addLayout(mid)

        btns = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.clicked.connect(self.copy_editor)
        btns.addWidget(copy)
        self.type_btn = QPushButton("Type into window")
        self.type_btn.setObjectName("primary")
        self.type_btn.clicked.connect(self.type_editor)
        btns.addWidget(self.type_btn, 1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.editor.clear)
        btns.addWidget(clear)
        lay.addLayout(btns)

        self.target_lbl = QLabel("")
        self.target_lbl.setObjectName("hint")
        lay.addWidget(self.target_lbl)

        speed = QHBoxLayout()
        speed.addWidget(QLabel("Typing speed"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1, SLIDER_INSTANT)
        self.slider.setValue(wpm_to_slider(int(self.cfg.get("wpm"))))
        self.slider.valueChanged.connect(self.on_speed)
        speed.addWidget(self.slider, 1)
        self.speed_lbl = QLabel()
        self.speed_lbl.setMinimumWidth(110)
        speed.addWidget(self.speed_lbl)
        lay.addLayout(speed)
        self.on_speed(self.slider.value())

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("color:#e3e6eb")
        lay.addWidget(line2)

        self.history_lbl = QLabel()
        lay.addWidget(self.history_lbl)
        self.history = QListWidget()
        self.history.itemClicked.connect(self.copy_history)
        self.history.itemDoubleClicked.connect(self.edit_history)
        lay.addWidget(self.history, 1)

        self.setCentralWidget(root)
        self.set_mode(self.cfg.get("mode"), save=False)

    def build_tray(self):
        self.tray = QSystemTrayIcon(dot_icon(GREY), self)
        menu = QMenu()
        show = QAction("Open ClipSync", self)
        show.triggered.connect(self.show_window)
        self.tray_pause = QAction("Pause sync", self)
        self.tray_pause.setCheckable(True)
        self.tray_pause.triggered.connect(lambda c: self.core.set_paused(c))
        quit_ = QAction("Quit", self)
        quit_.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addAction(self.tray_pause)
        menu.addSeparator()
        menu.addAction(quit_)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_window() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        e.ignore()
        self.hide()
        if not self.cfg.get("seen_tray_tip"):
            self.tray.showMessage("ClipSync", "Still running in the tray. Right-click the icon to quit.",
                                  dot_icon(GREEN), 3000)
            self.cfg.set("seen_tray_tip", True)

    # ---- status / pause
    def on_status(self, names):
        self.connected = names
        self.refresh_status()

    def refresh_status(self):
        paused = self.core.pause.paused
        if paused:
            text, color = "Paused", AMBER
        elif self.connected:
            text, color = f"Connected to {', '.join(self.connected)}", GREEN
        elif self.core.paired_devices():
            text, color = "Waiting for PC 1...", GREY
        else:
            text, color = "No PC paired yet - open Settings", GREY
        self.status.setText(f"{status_dot(color)} {text}")
        self.tray.setIcon(dot_icon(color))
        self.tray.setToolTip(f"ClipSync Receiver\n{text}")

    def on_pause(self, paused, quiet=False):
        if paused and (self.typer.busy or self.queue):
            self.stop_all_typing(quiet=True)
        self.pause_btn.setText("Resume" if paused else "Pause")
        self.pause_btn.setProperty("paused", "true" if paused else "false")
        self.pause_btn.style().unpolish(self.pause_btn)
        self.pause_btn.style().polish(self.pause_btn)
        self.tray_pause.setChecked(paused)
        self.refresh_status()
        if not quiet:
            self.statusBar().showMessage("Sync paused" if paused else "Sync resumed", 3000)
            if not self.isVisible():
                self.tray.showMessage("ClipSync", "Sync paused" if paused else "Sync resumed",
                                      dot_icon(AMBER if paused else GREEN), 1500)

    # ---- hotkey
    def register_hotkey(self, shortcut, first=False) -> bool:
        if self.hotkey:
            self.hotkey.stop()
        hk = GlobalHotkey(shortcut, self.bridge.hotkey.emit)
        if hk.ok:
            self.hotkey = hk
            self.pause_btn.setToolTip(f"Shortcut: {pretty_shortcut(shortcut)}")
            self.tray_pause.setText(f"Pause sync    ({pretty_shortcut(shortcut)})")
            return True
        self.hotkey = None
        if first:
            QTimer.singleShot(800, lambda: self.shortcut_problem(hk.error))
        return False

    def shortcut_problem(self, error):
        self.show_window()
        QMessageBox.warning(self, "ClipSync - shortcut",
                            f"{error}\n\nOpen Settings and choose a different pause shortcut.")
        self.open_settings()

    def on_hotkey(self):
        if self.typer.busy or self.queue:
            self.stop_all_typing()  # first press stops typing that's in progress
        else:
            self.core.set_paused(not self.core.pause.paused)

    # ---- receiving
    def on_clip(self, text, name):
        typing = self.cfg.get("mode") == "type"
        if not typing:
            QGuiApplication.clipboard().setText(text)
        top = self.history.item(0)
        if top is None or top.data(256) != text:
            one_line = " ".join(text.split())
            it = QListWidgetItem(one_line[:90] + ("..." if len(one_line) > 90 else ""))
            it.setData(256, text)
            it.setToolTip(text[:2000])
            self.history.insertItem(0, it)
            while self.history.count() > HISTORY_SIZE:
                self.history.takeItem(self.history.count() - 1)
        if typing:
            self.enqueue(text)
        else:
            self.statusBar().showMessage(f"Received from {name} - ready to paste", 3000)

    def copy_history(self, it):
        if self.cfg.get("mode") == "type":
            self.enqueue(it.data(256))
            return
        QGuiApplication.clipboard().setText(it.data(256))
        self.statusBar().showMessage("Copied - press Ctrl+V to paste", 2500)

    # ---- type mode: received clips are typed into one chosen web app window
    def set_mode(self, mode, save=True):
        if mode not in ("paste", "type"):
            mode = "paste"
        if save:
            self.cfg.set("mode", mode)
        if mode == "paste":
            self.stop_all_typing(quiet=True)
        (self.type_mode_btn if mode == "type" else self.paste_mode_btn).setChecked(True)
        self.lock_row.setVisible(mode == "type")
        self.refresh_lock()
        if mode == "type":
            self.history_lbl.setText("<b>Received from PC 1</b>  <span style='color:#6b7280'>(typed into your "
                                     "web app, not put on the clipboard. Click to type it again, "
                                     "double-click to edit)</span>")
        else:
            self.history_lbl.setText("<b>Received from PC 1</b>  <span style='color:#6b7280'>(already on the "
                                     "clipboard - just Ctrl+V. Click to copy again, double-click to edit)</span>")
        if save:
            self.statusBar().showMessage("Type mode: clips will be typed into your web app" if mode == "type"
                                         else "Paste mode: clips go on the clipboard", 3000)

    def lock_ok(self):
        return is_other_app_window(self.lock) or (not IS_WIN and self.lock == "any")

    def refresh_lock(self):
        waiting = f"  -  {len(self.queue)} waiting" if self.queue else ""
        if self.lock_ok():
            title = (window_title(self.lock) if IS_WIN else "") or self.cfg.get("lock_title") or "chosen window"
            self.lock_lbl.setText(f"{status_dot(GREEN)} Typing into: <b>{title[:60]}</b>{waiting}")
            self.pick_btn.setText("Change window")
        else:
            self.lock_lbl.setText(f"{status_dot(AMBER)} Pick the web app window to type into{waiting}")
            self.pick_btn.setText("Pick web app window")

    def pick_window(self):
        self.pick_btn.setEnabled(False)
        self._pick_left = 3
        self._pick_tick()

    def _pick_tick(self):
        if self._pick_left > 0:
            self.lock_lbl.setText(f"Click into the text box in your web app now... <b>{self._pick_left}</b>")
            self._pick_left -= 1
            QTimer.singleShot(1000, self._pick_tick)
            return
        self.pick_btn.setEnabled(True)
        hwnd = foreground_window()
        if is_other_app_window(hwnd):
            self.lock = hwnd
            self.cfg.set("lock_title", window_title(hwnd))
            self.statusBar().showMessage(f"Clips will be typed into: {window_title(hwnd)[:60]}", 4000)
        elif not IS_WIN:
            self.lock = "any"
        else:
            self.statusBar().showMessage("That was the ClipSync window - click into your web app instead", 4000)
        self.refresh_lock()
        self.process_queue()

    def enqueue(self, text):
        self.queue.append(text)
        if len(self.queue) > 20:
            self.queue.pop(0)
        self.process_queue()

    def process_queue(self, from_timer=False):
        self.refresh_lock()
        if self.typer.busy or not self.queue or self.core.pause.paused:
            return
        if not self.lock_ok():
            if not from_timer:
                self.show_window()
                self.statusBar().showMessage("Pick the web app window to type into first", 4000)
            return
        if IS_WIN and not focus_window(self.lock):
            # Windows refused to switch - it starts as soon as you click into the web app.
            if not from_timer:
                self.tray.showMessage("ClipSync", f"Click into {window_title(self.lock)[:40]} to start typing",
                                      dot_icon(AMBER), 3000)
            return
        self.job = self.queue.pop(0)
        guard = (lambda h=self.lock: foreground_window() == h) if IS_WIN else None
        self.start_typing(self.job, delay=0.15, guard=guard)
        self.refresh_lock()

    def stop_all_typing(self, quiet=False):
        self.queue.clear()
        self.job = None
        self.typer.cancel()
        self.refresh_lock()
        if not quiet:
            self.statusBar().showMessage("Typing stopped", 3000)

    def edit_history(self, it):
        self.editor.setPlainText(it.data(256))
        self.editor.setFocus()

    # ---- typing
    def track_target(self):
        hwnd = foreground_window()
        if self.queue and not self.typer.busy and self.lock_ok() and (not IS_WIN or hwnd == self.lock):
            self.process_queue(from_timer=True)
        if is_other_app_window(hwnd):
            if hwnd != self.target:
                self.target = hwnd
                title = window_title(hwnd) or "the last window you used"
                self.target_lbl.setText(f"Will type into: {title[:70]}")
        elif not IS_WIN:
            self.target_lbl.setText("Click into the window you want within 3 seconds of pressing Type.")

    def on_speed(self, v):
        wpm = slider_to_wpm(v)
        self.cfg.set("wpm", wpm)
        self.speed_lbl.setText("Instant" if wpm == INSTANT else f"{wpm} words/min")

    def copy_editor(self):
        text = self.editor.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)
            self.statusBar().showMessage("Copied - press Ctrl+V to paste", 2500)

    def type_editor(self):
        if self.typer.busy:
            self.typer.cancel()
            return
        text = self.editor.toPlainText()
        if not text:
            self.statusBar().showMessage("Nothing to type yet", 2500)
            return
        if self.cfg.get("mode") == "type" and self.lock_ok() and focus_window(self.lock):
            self.start_typing(text, delay=0.3)
        elif focus_window(self.target):
            self.start_typing(text, delay=0.3)
        else:
            self.statusBar().showMessage("Click into the window you want - typing starts in 3 seconds", 3000)
            self.start_typing(text, delay=3.0)

    def start_typing(self, text, delay=0.3, guard=None):
        wpm = slider_to_wpm(self.slider.value())
        if self.typer.type(text, wpm, on_done=self.bridge.typing_done.emit, start_delay=delay, guard=guard):
            self.type_btn.setText(f"Stop typing  ({pretty_shortcut(self.cfg.get('shortcut'))})")

    def on_typing_done(self, completed, typed, reason):
        self.type_btn.setText("Type into window")
        job, self.job = self.job, None
        if reason == "focus" and job is not None:
            # You clicked away mid-way - keep the rest and carry on when you click back in.
            rest = normalise(job)[typed:]
            if rest:
                self.queue.insert(0, rest)
            self.statusBar().showMessage("Paused typing - click back into your web app to continue", 5000)
        elif completed:
            self.statusBar().showMessage("Done typing", 2000)
        self.refresh_lock()
        if completed:
            QTimer.singleShot(200, self.process_queue)

    # ---- mic
    def preload_model(self):
        if self.transcriber.loaded:
            return
        self.mic_state.setText("Getting speech ready...")

        def work():
            try:
                self.transcriber.load()
                self.bridge.model_ready.emit(True, "")
            except Exception as e:
                log.exception("Speech model failed to load")
                self.bridge.model_ready.emit(False, str(e))
        threading.Thread(target=work, daemon=True).start()

    def on_model_ready(self, ok, err):
        if ok:
            self.mic_state.setText("Ready")
            QTimer.singleShot(3000, lambda: self.mic_state.setText("") if not self.recording else None)
        else:
            self.mic_state.setText("Speech model couldn't load. If you picked a new model size in Settings, "
                                   "connect to the internet once so it can download.")

    def set_model(self, name):
        self.cfg.set("model", name)
        self.transcriber.set_model(name)
        self.preload_model()

    def toggle_mic(self):
        if self.recording:
            self.stop_mic()
        else:
            self.start_mic()

    def start_mic(self):
        try:
            self.recorder.start()
        except Exception as e:
            log.exception("Mic failed")
            self.on_voice_error(f"Couldn't open the microphone: {e}")
            return
        self.recording = True
        self.mic_btn.setText("■  Stop")
        self.mic_btn.setProperty("recording", "true")
        self.mic_btn.style().unpolish(self.mic_btn)
        self.mic_btn.style().polish(self.mic_btn)
        self.level.show()
        self._rec_timer.start()

    def update_recording(self):
        secs = int(self.recorder.seconds)
        self.mic_state.setText(f"Listening... {secs // 60}:{secs % 60:02d}")
        self.level.setValue(min(100, int(self.recorder.level * 400)))

    def stop_mic(self):
        self._rec_timer.stop()
        audio = self.recorder.stop()
        self.recording = False
        self.mic_btn.setText("\U0001F3A4  Speak")
        self.mic_btn.setProperty("recording", "false")
        self.mic_btn.style().unpolish(self.mic_btn)
        self.mic_btn.style().polish(self.mic_btn)
        self.level.hide()
        self.mic_btn.setEnabled(False)
        self.mic_state.setText("Turning speech into text...")

        def work():
            try:
                self.bridge.voice_text.emit(self.transcriber.transcribe(audio))
            except Exception as e:
                log.exception("Transcription failed")
                self.bridge.voice_error.emit(f"Speech-to-text failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def on_voice_text(self, text):
        self.mic_btn.setEnabled(True)
        if not text:
            self.mic_state.setText("Didn't catch anything - try again")
            return
        self.mic_state.setText("")
        cursor = self.editor.textCursor()
        before = self.editor.toPlainText()[:cursor.position()]
        if before and not before.endswith((" ", "\n")):
            text = " " + text
        cursor.insertText(text)
        self.editor.setFocus()

    def on_voice_error(self, msg):
        self.mic_btn.setEnabled(True)
        self.mic_state.setText(msg)

    # ---- settings / quit
    def open_settings(self):
        if self.settings is None:
            self.settings = SettingsDialog(self)
        self.settings.fill()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def set_autostart(self, enabled):
        self.cfg.set("autostart", enabled)
        try:
            set_autostart("ClipSync Receiver", enabled)
        except OSError:
            log.exception("Couldn't change autostart")

    def quit(self):
        self.typer.cancel()
        if self.recording:
            self.recorder.stop()
        if self.hotkey:
            self.hotkey.stop()
        self.tray.hide()
        self.core.stop()
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ClipSync Receiver")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    lock = single_instance(ROLE)
    if lock is None:
        QMessageBox.information(None, "ClipSync", "ClipSync Receiver is already running - look for it in the "
                                                  "system tray.")
        return 0
    win = MainWindow(app)
    if "--minimized" not in sys.argv:
        win.show()
    log.info("Receiver started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
