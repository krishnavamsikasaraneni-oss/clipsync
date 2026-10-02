"""Types text into whatever window has focus, at a chosen words-per-minute speed."""
import logging
import random
import threading
import time

log = logging.getLogger("clipsync")

INSTANT = 0  # wpm value meaning "as fast as possible"


def char_delay(wpm: int) -> float:
    """Seconds per character. A 'word' is 5 characters, the usual typing-test rule."""
    if wpm <= INSTANT:
        return 0.002
    return 60.0 / (wpm * 5)


def normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


class Typer:
    def __init__(self):
        self._cancel = threading.Event()
        self._thread = None
        self.typed = 0  # characters typed in the current/last job

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def cancel(self):
        self._cancel.set()

    def type(self, text: str, wpm: int, on_done=None, start_delay=0.3, guard=None):
        """Types text in a background thread. on_done(completed, typed_chars, reason).

        guard() is checked before every character; if it returns False typing stops
        with reason 'focus' (used to stop when you click away from the target window).
        """
        if self.busy:
            return False
        self._cancel.clear()
        self.typed = 0
        self._thread = threading.Thread(target=self._run, args=(text, wpm, on_done, start_delay, guard),
                                        daemon=True)
        self._thread.start()
        return True

    def _run(self, text, wpm, on_done, start_delay, guard):
        completed, reason = False, "cancelled"
        try:
            from pynput.keyboard import Controller, Key
            kb = Controller()
            time.sleep(start_delay)
            base = char_delay(wpm)
            human = wpm > INSTANT and wpm <= 150
            text = normalise(text)
            for ch in text:
                if self._cancel.is_set():
                    break
                if guard is not None and not guard():
                    reason = "focus"
                    break
                if ch == "\n":
                    kb.press(Key.enter); kb.release(Key.enter)
                elif ch == "\t":
                    kb.press(Key.tab); kb.release(Key.tab)
                else:
                    kb.type(ch)
                self.typed += 1
                d = base
                if human:
                    d *= random.uniform(0.6, 1.4)
                    if ch in " .,;:!?\n":
                        d += base * random.uniform(0.3, 1.2)
                time.sleep(d)
            completed = self.typed == len(text)
            if completed:
                reason = "done"
        except Exception:
            log.exception("Typing failed")
            reason = "error"
        finally:
            if on_done:
                on_done(completed, self.typed, reason)
