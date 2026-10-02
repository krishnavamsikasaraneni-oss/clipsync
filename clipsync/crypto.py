"""Pairing (SPAKE2 with a 6-digit code) and the encrypted channel (AES-256-GCM)."""
import base64
import hashlib
import hmac
import json
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_A, SPAKE2_B

ID_SENDER = b"clipsync-sender"
ID_RECEIVER = b"clipsync-receiver"


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def new_pair_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hkdf(key: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(key)


def mac(key: bytes, label: bytes) -> str:
    return b64e(hmac.new(key, label, hashlib.sha256).digest())


def mac_ok(key: bytes, label: bytes, given: str) -> bool:
    try:
        return hmac.compare_digest(b64d(mac(key, label)), b64d(given))
    except Exception:
        return False


class Pairing:
    """One side of a SPAKE2 exchange. The code never travels over the network."""

    def __init__(self, code: str, is_sender: bool):
        cls = SPAKE2_A if is_sender else SPAKE2_B
        self._s = cls(code.encode("ascii"), idA=ID_SENDER, idB=ID_RECEIVER)
        self.outbound = b64e(self._s.start())

    def finish(self, inbound: str) -> bytes:
        raw = self._s.finish(b64d(inbound))
        return hkdf(raw, b"clipsync-pair", b"long-term-key")


def session_key(long_term: bytes, sender_nonce: bytes, receiver_nonce: bytes) -> bytes:
    return hkdf(long_term, sender_nonce + receiver_nonce, b"session-key")


def new_nonce() -> bytes:
    return os.urandom(16)


class Channel:
    """Encrypts JSON messages. Counters in the nonce stop replays and reordering."""

    def __init__(self, key: bytes, is_sender: bool):
        self._aes = AESGCM(key)
        self._out_prefix = b"SEND" if is_sender else b"RECV"
        self._in_prefix = b"RECV" if is_sender else b"SEND"
        self._out = 0
        self._in = 0

    def seal(self, msg: dict) -> bytes:
        nonce = self._out_prefix + self._out.to_bytes(8, "big")
        self._out += 1
        return self._aes.encrypt(nonce, json.dumps(msg).encode("utf-8"), None)

    def open(self, data: bytes) -> dict:
        nonce = self._in_prefix + self._in.to_bytes(8, "big")
        plain = self._aes.decrypt(nonce, data, None)  # raises InvalidTag if tampered
        self._in += 1
        return json.loads(plain.decode("utf-8"))
