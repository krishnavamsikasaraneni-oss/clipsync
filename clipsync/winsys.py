"""Windows-only helpers: global hotkey, start with Windows, window focus.

Everything degrades to a harmless no-op on other systems so the apps can be
tested on Linux too.
"""
import logging
import os
import sys
import threading
import time

log = logging.getLogger("clipsync")
IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    for _fn in ("IsWindow", "IsIconic", "SetForegroundWindow"):
        getattr(user32, _fn).argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_KEYS = {"space": 0x20, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
               "pageup": 0x21, "pagedown": 0x22, "f1": 0x70}


def parse_shortcut(text: str):
    """'ctrl+alt+z' -> (modifiers, virtual key). Raises ValueError if invalid."""
    parts = [p.strip().lower() for p in text.replace(" ", "").split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("Use at least one modifier, like Ctrl+Alt+Z")
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p not in _MODS:
            raise ValueError(f"Unknown modifier '{p}'")
        mods |= _MODS[p]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise ValueError(f"Unknown key '{key}'")
    return mods, vk


def pretty_shortcut(text: str) -> str:
    return "+".join(p.strip().capitalize() for p in text.split("+"))


class GlobalHotkey:
    """System-wide shortcut. .ok is False if another program already owns it."""

    def __init__(self, shortcut: str, callback):
        self.shortcut = shortcut
        self.callback = callback
        self.ok = False
        self.error = ""
        self._tid = None
        self._ready = threading.Event()
        try:
            self._mods, self._vk = parse_shortcut(shortcut)
        except ValueError as e:
            self.error = str(e)
            return
        if not IS_WIN:
            self.ok = True
            return
        threading.Thread(target=self._run, name="clipsync-hotkey", daemon=True).start()
        self._ready.wait(3)

    def _run(self):
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self._mods | MOD_NOREPEAT, self._vk):
            self.error = f"{pretty_shortcut(self.shortcut)} is already used by another program."
            log.warning(self.error)
            self._ready.set()
            return
        self.ok = True
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    self.callback()
                except Exception:
                    log.exception("Hotkey callback failed")
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if IS_WIN and self._tid and self.ok:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self.ok = False


# ---------------------------------------------------------------- start with Windows

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command(extra_args: str) -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {extra_args}'.strip()
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}" {extra_args}'.strip()


def set_autostart(name: str, enabled: bool, extra_args: str = "--minimized"):
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, _launch_command(extra_args))
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- window focus

def foreground_window():
    return user32.GetForegroundWindow() if IS_WIN else None


def window_pid(hwnd) -> int:
    if not IS_WIN or not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_other_app_window(hwnd) -> bool:
    return bool(IS_WIN and hwnd and user32.IsWindow(hwnd) and window_pid(hwnd) != os.getpid())


def window_title(hwnd) -> str:
    if not IS_WIN or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def focus_window(hwnd) -> bool:
    """Bring a window to the front. Returns True only if it really is in front now."""
    if not is_other_app_window(hwnd):
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows only lets the app that's in front change focus. Borrow the front
    # window's input queue for a moment so we're allowed to switch.
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    if user32.GetForegroundWindow() != hwnd:
        # Fallback: a quick Alt tap also lifts the focus lock.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd
