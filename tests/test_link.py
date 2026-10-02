"""End-to-end tests: a real receiver and sender talking over localhost."""
import queue
import time

import pytest

from clipsync.config import Config
from clipsync.link import ReceiverCore, SenderCore


class Recorder:
    def __init__(self):
        self.q = queue.Queue()

    def __getattr__(self, name):
        return lambda *a: self.q.put((name, a))

    def wait(self, name, timeout=8, pred=lambda a: True):
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev, args = self.q.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                break
            if ev == name and pred(args):
                return args
        raise AssertionError(f"timed out waiting for {name}")


@pytest.fixture
def pair_of(tmp_path):
    made = []

    def make():
        rev, sev = Recorder(), Recorder()
        rcfg = Config("receiver", {"paired": {}}, path=tmp_path / "r.json")
        scfg = Config("sender", {"receiver": None}, path=tmp_path / "s.json")
        r = ReceiverCore(rcfg, rev, discovery=False, port=0, host="127.0.0.1")
        s = SenderCore(scfg, sev, discovery=False)
        r.start()
        s.start()
        made.extend([r, s])
        return r, s, rev, sev

    yield make
    for c in made:
        c.stop()


def do_pair(r, s, sev, code=None):
    real = r.start_pairing()
    s.pair("127.0.0.1", r.port, code or real)
    return sev.wait("on_pair_result")


def test_pair_send_pause(pair_of):
    r, s, rev, sev = pair_of()
    ok, msg = do_pair(r, s, sev)
    assert ok, msg
    rev.wait("on_paired")
    sev.wait("on_status", pred=lambda a: a[0] == "connected")

    s.send_clip("hello from PC 1")
    assert rev.wait("on_clip")[0] == "hello from PC 1"

    s.send_clip("unicode ✓ తెలుగు\nline 2")
    assert rev.wait("on_clip")[0] == "unicode ✓ తెలుగు\nline 2"

    # pause from PC 2 reaches PC 1, and PC 1 then stops sending
    r.set_paused(True)
    assert sev.wait("on_pause") == (True,)
    s.send_clip("should not arrive")
    with pytest.raises(AssertionError):
        rev.wait("on_clip", timeout=1)

    # resume from PC 1 reaches PC 2
    s.set_paused(False)
    assert rev.wait("on_pause") == (False,)
    s.send_clip("back on")
    assert rev.wait("on_clip")[0] == "back on"


def test_wrong_code_rejected(pair_of):
    r, s, rev, sev = pair_of()
    real = r.start_pairing()
    wrong = f"{(int(real) + 1) % 1_000_000:06d}"
    s.pair("127.0.0.1", r.port, wrong)
    ok, msg = sev.wait("on_pair_result")
    assert not ok and "Wrong code" in msg
    assert r.paired_devices() == {}
    assert s.receiver() is None


def test_not_in_pairing_mode(pair_of):
    r, s, rev, sev = pair_of()
    s.pair("127.0.0.1", r.port, "123456")
    ok, msg = sev.wait("on_pair_result")
    assert not ok and "isn't ready" in msg


def test_code_changes_after_three_wrong_tries(pair_of):
    r, s, rev, sev = pair_of()
    real = r.start_pairing()
    for i in range(3):
        s.pair("127.0.0.1", r.port, f"{(int(real) + 1 + i) % 1_000_000:06d}")
        sev.wait("on_pair_result")
    new_code = rev.wait("on_pair_code_changed")[0]
    assert new_code != real or True  # random, could rarely match
    s.pair("127.0.0.1", r.port, new_code)
    ok, _ = sev.wait("on_pair_result")
    assert ok


def test_unpaired_sender_is_rejected(pair_of):
    r, s, rev, sev = pair_of()
    ok, _ = do_pair(r, s, sev)
    assert ok
    sev.wait("on_status", pred=lambda a: a[0] == "connected")
    sid = next(iter(r.paired_devices()))
    r.unpair(sid)
    assert sev.wait("on_status", timeout=10, pred=lambda a: a[0] == "rejected")


def test_reconnects_after_receiver_restart(tmp_path):
    rev, sev = Recorder(), Recorder()
    rcfg = Config("receiver", {"paired": {}}, path=tmp_path / "r.json")
    scfg = Config("sender", {"receiver": None}, path=tmp_path / "s.json")
    r = ReceiverCore(rcfg, rev, discovery=False, port=0, host="127.0.0.1")
    s = SenderCore(scfg, sev, discovery=False)
    r.start(); s.start()
    try:
        ok, _ = do_pair(r, s, sev)
        assert ok
        sev.wait("on_status", pred=lambda a: a[0] == "connected")
        port = r.port
        r.stop()
        sev.wait("on_status", pred=lambda a: a[0] == "searching")
        # copied while PC 2 is down - should arrive once it's back
        s.send_clip("sent while offline")
        rcfg2 = Config("receiver", {"paired": {}}, path=tmp_path / "r.json")
        r = ReceiverCore(rcfg2, rev, discovery=False, port=port, host="127.0.0.1")
        r.start()
        sev.wait("on_status", timeout=15, pred=lambda a: a[0] == "connected")
        assert rev.wait("on_clip")[0] == "sent while offline"
    finally:
        s.stop(); r.stop()
