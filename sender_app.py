"""ClipSync Sender - runs on PC 1 in the system tray and sends copied text to PC 2."""
import sys
import time

from clipsync.config import DEFAULT_PORT, DEFAULT_SHORTCUT, Config, setup_logging

ROLE = "sender"


def selftest():
    import clipsync.link, clipsync.winsys, zeroconf, websockets, spake2  # noqa: F401
    from PySide6 import QtWidgets  # noqa: F401
    print("selftest ok")
    return 0


if "--selftest" in sys.argv:
    sys.exit(selftest())

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QIntValidator
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QSystemTrayIcon, QVBoxLayout)

from clipsync.link import SenderCore
from clipsync.ui_common import AMBER, GREEN, GREY, RED, STYLE, dot_icon, single_instance
from clipsync.winsys import GlobalHotkey, parse_shortcut, pretty_shortcut, set_autostart

log = setup_logging(ROLE)

PRIVATE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")
ZERO_MEANS_PRIVATE = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")


class Bridge(QObject):
    """Moves events from the network thread onto the Qt thread."""
    status = Signal(str, str)
    pause = Signal(bool)
    discovered = Signal(list)
    pair_result = Signal(bool, str)

    def on_status(self, state, detail):
        self.status.emit(state, detail)

    def on_pause(self, paused):
        self.pause.emit(paused)

    def on_discovered(self, items):
        self.discovered.emit(items)

    def on_pair_result(self, ok, msg):
        self.pair_result.emit(ok, msg)

    def __getattr__(self, name):
        if name.startswith("on_"):
            return lambda *a: None
        raise AttributeError(name)


def is_private(mime) -> bool:
    """True for clips that password managers mark as 'don't sync / don't keep'."""
    for fmt in mime.formats():
        if any(p in fmt for p in PRIVATE_FORMATS):
            return True
        if any(p in fmt for p in ZERO_MEANS_PRIVATE):
            data = bytes(mime.data(fmt))
            if data[:4] == b"\x00\x00\x00\x00":
                return True
    return False


class PairDialog(QDialog):
    def __init__(self, core: SenderCore, bridge: Bridge):
        super().__init__()
        self.core = core
        self.setWindowTitle("ClipSync - Pair with PC 2")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)

        steps = QLabel("<b>1.</b> On PC 2, open <b>ClipSync Receiver</b> &gt; Settings &gt; <b>Pair a new PC</b>.<br>"
                       "<b>2.</b> Pick PC 2 below (or type its IP address).<br>"
                       "<b>3.</b> Type the 6-digit code shown on PC 2 and click Pair.")
        steps.setWordWrap(True)
        lay.addWidget(steps)

        lay.addWidget(QLabel("PCs found on this Wi-Fi:"))
        self.list = QListWidget()
        self.list.setMaximumHeight(120)
        lay.addWidget(self.list)
        self.empty = QLabel("Searching... make sure ClipSync Receiver is open on PC 2.")
        self.empty.setObjectName("hint")
        lay.addWidget(self.empty)

        self.ip = QLineEdit()
        self.ip.setPlaceholderText("Or type PC 2's IP address, e.g. 192.168.1.20")
        lay.addWidget(self.ip)

        row = QHBoxLayout()
        self.code = QLineEdit()
        self.code.setPlaceholderText("6-digit code")
        self.code.setMaxLength(6)
        self.code.setValidator(QIntValidator(0, 999999))
        self.code.returnPressed.connect(self.do_pair)
        row.addWidget(self.code)
        self.btn = QPushButton("Pair")
        self.btn.setObjectName("primary")
        self.btn.clicked.connect(self.do_pair)
        row.addWidget(self.btn)
        lay.addLayout(row)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)

        bridge.discovered.connect(self.fill)
        bridge.pair_result.connect(self.on_result)
        self.fill(core.found())

    def fill(self, items):
        current = self.list.currentItem().data(256) if self.list.currentItem() else None
        self.list.clear()
        for f in items:
            it = QListWidgetItem(f"{f['name']}   ({f['hosts'][0]})")
            it.setData(256, f)
            self.list.addItem(it)
            if current and current["id"] == f["id"]:
                self.list.setCurrentItem(it)
        if self.list.count() == 1 and not self.list.currentItem():
            self.list.setCurrentRow(0)
        self.empty.setVisible(self.list.count() == 0)

    def do_pair(self):
        code = self.code.text().strip()
        ip = self.ip.text().strip()
        if ip:
            host, _, port = ip.partition(":")
            port = int(port) if port.isdigit() else DEFAULT_PORT
        elif self.list.currentItem():
            f = self.list.currentItem().data(256)
            host, port = f["hosts"][0], f["port"]
        else:
            self.msg.setText("Pick PC 2 from the list or type its IP address.")
            return
        if len(code) != 6:
            self.msg.setText("Enter the 6-digit code shown on PC 2.")
            return
        self.btn.setEnabled(False)
        self.msg.setText("Pairing...")
        self.core.pair(host, port, code)

    def on_result(self, ok, message):
        self.btn.setEnabled(True)
        self.msg.setText(("\u2714 " if ok else "") + message)
        if ok:
            QTimer.singleShot(1500, self.accept)


class SenderApp(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.cfg = Config(ROLE, {"receiver": None, "paused": False, "shortcut": DEFAULT_SHORTCUT,
                                 "autostart": True})
        self.bridge = Bridge()
        self.core = SenderCore(self.cfg, self.bridge)
        self.state, self.detail = "starting", ""
        self.last_text, self.last_time = None, 0.0
        self.pair_dialog = None

        self.tray = QSystemTrayIcon(dot_icon(GREY))
        self.menu = QMenu()
        self.status_action = QAction("Starting...")
        self.status_action.setEnabled(False)
        self.pause_action = QAction("Pause sync")
        self.pause_action.setCheckable(True)
        self.pause_action.triggered.connect(lambda checked: self.core.set_paused(checked))
        pair_action = QAction("Pair with PC 2...")
        pair_action.triggered.connect(self.open_pair)
        shortcut_action = QAction("Change pause shortcut...")
        shortcut_action.triggered.connect(self.change_shortcut)
        self.autostart_action = QAction("Start with Windows")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(bool(self.cfg.get("autostart")))
        self.autostart_action.triggered.connect(self.toggle_autostart)
        quit_action = QAction("Quit")
        quit_action.triggered.connect(self.quit)
        for a in (self.status_action, None, self.pause_action, None, pair_action, shortcut_action,
                  self.autostart_action, None, quit_action):
            self.menu.addSeparator() if a is None else self.menu.addAction(a)
        self._actions = [pair_action, shortcut_action, quit_action]
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_tray_click)
        self.tray.show()

        self.bridge.status.connect(self.on_status)
        self.bridge.pause.connect(self.on_pause)

        self._debounce = QTimer(singleShot=True, interval=120)
        self._debounce.timeout.connect(self.read_clipboard)
        QGuiApplication.clipboard().dataChanged.connect(self._debounce.start)

        self.hotkey = None
        self.register_hotkey(self.cfg.get("shortcut"), first=True)
        try:
            set_autostart("ClipSync Sender", bool(self.cfg.get("autostart")), "")
        except OSError:
            log.exception("Couldn't set autostart")

        self.core.start()
        self.pause_action.setChecked(self.core.pause.paused)
        self.refresh()
        if not self.cfg.get("receiver"):
            QTimer.singleShot(300, self.open_pair)

    # ---- clipboard
    def read_clipboard(self):
        if self.core.pause.paused:
            return
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None or not mime.hasText():
            return
        if is_private(mime):
            log.info("Skipped a private clip (password manager)")
            return
        text = mime.text()
        now = time.time()
        if not text or (text == self.last_text and now - self.last_time < 1.5):
            return
        self.last_text, self.last_time = text, now
        self.core.send_clip(text)

    # ---- hotkey
    def register_hotkey(self, shortcut, first=False) -> bool:
        if self.hotkey:
            self.hotkey.stop()
        hk = GlobalHotkey(shortcut, lambda: self.core.set_paused(not self.core.pause.paused))
        if hk.ok:
            self.hotkey = hk
            self.pause_action.setText(f"Pause sync    ({pretty_shortcut(shortcut)})")
            return True
        self.hotkey = None
        self.pause_action.setText("Pause sync")
        if first:
            QTimer.singleShot(500, lambda: self.shortcut_problem(hk.error))
        return False

    def shortcut_problem(self, error):
        QMessageBox.warning(None, "ClipSync - shortcut", f"{error}\n\nPick a different pause shortcut.")
        self.change_shortcut()

    def change_shortcut(self):
        current = pretty_shortcut(self.cfg.get("shortcut"))
        text, ok = QInputDialog.getText(None, "Pause shortcut",
                                        "New shortcut (for example Ctrl+Alt+Z or Ctrl+Shift+F9):", text=current)
        if not ok or not text.strip():
            return
        try:
            parse_shortcut(text)
        except ValueError as e:
            QMessageBox.warning(None, "ClipSync", str(e))
            return
        if self.register_hotkey(text.strip().lower()):
            self.cfg.set("shortcut", text.strip().lower())
            self.tray.showMessage("ClipSync", f"Pause shortcut is now {pretty_shortcut(text)}", dot_icon(GREEN), 2000)
        else:
            QMessageBox.warning(None, "ClipSync", f"{pretty_shortcut(text)} is already used by another program.")
            self.register_hotkey(self.cfg.get("shortcut"))

    # ---- status
    def on_status(self, state, detail):
        self.state, self.detail = state, detail
        self.refresh()

    def on_pause(self, paused):
        self.pause_action.setChecked(paused)
        self.refresh()
        self.tray.showMessage("ClipSync", "Sync paused" if paused else "Sync resumed",
                              dot_icon(AMBER if paused else GREEN), 1500)

    def refresh(self):
        paused = self.core.pause.paused
        texts = {"connected": (f"Connected to {self.detail}", GREEN),
                 "searching": (f"Looking for {self.detail}...", GREY),
                 "unpaired": ("Not paired yet - click 'Pair with PC 2'", GREY),
                 "rejected": (f"{self.detail} doesn't recognise this PC - pair again", RED),
                 "starting": ("Starting...", GREY)}
        text, color = texts.get(self.state, (self.state, GREY))
        if paused:
            text, color = f"Paused  -  {text}", AMBER
        self.status_action.setText(("●  ") + text)
        self.tray.setIcon(dot_icon(color))
        self.tray.setToolTip(f"ClipSync Sender\n{text}")

    # ---- misc
    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger and self.state in ("unpaired", "rejected"):
            self.open_pair()

    def open_pair(self):
        if self.pair_dialog and self.pair_dialog.isVisible():
            self.pair_dialog.raise_()
            self.pair_dialog.activateWindow()
            return
        self.pair_dialog = PairDialog(self.core, self.bridge)
        self.pair_dialog.show()
        self.pair_dialog.raise_()
        self.pair_dialog.activateWindow()

    def toggle_autostart(self, checked):
        self.cfg.set("autostart", checked)
        try:
            set_autostart("ClipSync Sender", checked, "")
        except OSError:
            log.exception("Couldn't change autostart")

    def quit(self):
        if self.hotkey:
            self.hotkey.stop()
        self.tray.hide()
        self.core.stop()
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ClipSync Sender")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    lock = single_instance(ROLE)
    if lock is None:
        QMessageBox.information(None, "ClipSync", "ClipSync Sender is already running - look for it in the system tray.")
        return 0
    sender = SenderApp(app)  # noqa: F841
    log.info("Sender started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
