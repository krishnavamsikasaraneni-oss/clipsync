"""Networking core for both apps. No GUI code here, so it can be tested on its own.

PC 2 (receiver) runs a WebSocket server and announces itself on the Wi-Fi with
mDNS. PC 1 (sender) finds it, connects, and sends clips. Everything after the
handshake is encrypted with a key agreed during pairing.
"""
import asyncio
import json
import logging
import socket
import threading
import time

from cryptography.exceptions import InvalidTag
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .config import DEFAULT_PORT, PROTOCOL_VERSION, SERVICE_TYPE
from .crypto import (Channel, Pairing, b64d, b64e, mac, mac_ok, new_nonce,
                     new_pair_code, session_key)

log = logging.getLogger("clipsync")

MAX_MSG = 4 * 1024 * 1024
MAX_TEXT = 1_000_000  # characters
PENDING_MAX_AGE = 30  # seconds - a clip copied while offline is sent on reconnect if newer than this
HANDSHAKE_TIMEOUT = 10
NET_ERRORS = (OSError, ConnectionClosed, InvalidHandshake, InvalidURI, asyncio.TimeoutError,
              InvalidTag, ValueError, KeyError, TypeError)


class Events:
    """Callbacks fired from the network thread. GUI code replaces these."""

    def __getattr__(self, name):
        return lambda *a, **k: None


def local_ipv4s() -> list[str]:
    ips = set()
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                    ips.add(ip.ip)
    except Exception:
        pass
    if not ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                ips.add(s.getsockname()[0])
        except OSError:
            pass
    return sorted(ips)


async def _cancel_others():
    me = asyncio.current_task()
    for t in asyncio.all_tasks():
        if t is not me:
            t.cancel()


class PauseState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.paused = bool(cfg.get("paused", False))
        self.ts = float(cfg.get("paused_ts", 0.0))

    def set_local(self, paused: bool) -> dict:
        self.paused, self.ts = paused, time.time()
        self._save()
        return self.message()

    def apply(self, msg: dict) -> bool:
        """Apply a pause update from the other PC. Newest change wins."""
        ts = float(msg.get("ts", 0))
        if ts <= self.ts:
            return False
        changed = bool(msg.get("paused")) != self.paused
        self.paused, self.ts = bool(msg.get("paused")), ts
        self._save()
        return changed

    def message(self) -> dict:
        return {"t": "pause", "paused": self.paused, "ts": self.ts}

    def _save(self):
        self.cfg.data["paused"] = self.paused
        self.cfg.set("paused_ts", self.ts)


class _LoopThread:
    def __init__(self, name):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.ready = threading.Event()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self.ready.set)
        self.loop.run_forever()

    def start(self, main_coro_factory):
        self.thread.start()
        self.ready.wait()
        return asyncio.run_coroutine_threadsafe(main_coro_factory(), self.loop)

    def call(self, fn, *args):
        self.loop.call_soon_threadsafe(fn, *args)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3)


# --------------------------------------------------------------------------- receiver

class ReceiverCore:
    """Runs on PC 2. Accepts connections from paired senders."""

    def __init__(self, cfg, events=None, discovery=True, port=DEFAULT_PORT, host="0.0.0.0"):
        self.cfg = cfg
        self.ev = events or Events()
        self.discovery = discovery
        self.want_port = port
        self.host = host
        self.port = None
        self.id = cfg.get("device_id")
        self.name = cfg.get("name") or socket.gethostname()
        self.pause = PauseState(cfg)
        self._lt = _LoopThread("clipsync-receiver")
        self._conns = {}  # ws -> dict(id, name, ch, lock)
        self._pair_code = None
        self._pair_failures = 0
        self._server = None
        self._zc = None
        self._info = None

    # ---- public API (call from any thread)
    def start(self):
        fut = self._lt.start(self._main)
        fut.result(timeout=15)

    def stop(self):
        try:
            self._lt.submit(self._shutdown()).result(timeout=5)
        except Exception:
            pass
        self._lt.stop()

    def start_pairing(self) -> str:
        code = new_pair_code()
        self._pair_failures = 0
        self._pair_code = code
        return code

    def stop_pairing(self):
        self._pair_code = None

    def set_paused(self, paused: bool):
        self._lt.call(self._set_paused, paused)

    def paired_devices(self) -> dict:
        return dict(self.cfg.get("paired", {}))

    def unpair(self, sender_id: str):
        paired = dict(self.cfg.get("paired", {}))
        paired.pop(sender_id, None)
        self.cfg.set("paired", paired)
        self._lt.call(self._drop, sender_id)

    def connected_names(self) -> list[str]:
        return [c["name"] for c in self._conns.values()]

    # ---- internals (network thread)
    async def _main(self):
        try:
            self._server = await serve(self._handle, self.host, self.want_port, max_size=MAX_MSG,
                                       ping_interval=15, ping_timeout=20)
        except OSError:
            log.warning("Port %s busy, picking a free one", self.want_port)
            self._server = await serve(self._handle, self.host, 0, max_size=MAX_MSG,
                                       ping_interval=15, ping_timeout=20)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("Receiver listening on port %s", self.port)
        if self.discovery:
            asyncio.get_running_loop().create_task(self._advertise_forever())

    async def _advertise_forever(self):
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
        current = None
        while True:
            try:
                ips = local_ipv4s()
                if ips and ips != current:
                    props = {"id": self.id, "name": self.name, "v": str(PROTOCOL_VERSION)}
                    info = ServiceInfo(SERVICE_TYPE, f"ClipSync-{self.id[:8]}.{SERVICE_TYPE}",
                                       addresses=[socket.inet_aton(i) for i in ips], port=self.port,
                                       properties=props, server=f"clipsync-{self.id[:8]}.local.")
                    if self._zc is None:
                        self._zc = AsyncZeroconf()
                        await self._zc.async_register_service(info, allow_name_change=True)
                    else:
                        await self._zc.async_update_service(info)
                    self._info, current = info, ips
                    log.info("Announcing on Wi-Fi at %s:%s", ips, self.port)
            except Exception:
                log.exception("mDNS announce failed, will retry")
                self._zc, current = None, None
            await asyncio.sleep(30)

    async def _shutdown(self):
        for ws in list(self._conns):
            await ws.close()
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
        if self._zc:
            try:
                if self._info:
                    await self._zc.async_unregister_service(self._info)
                await self._zc.async_close()
            except Exception:
                pass
        await _cancel_others()

    async def _handle(self, ws):
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
            if hello.get("v") != PROTOCOL_VERSION:
                await ws.send(json.dumps({"t": "error", "reason": "version"}))
            elif hello.get("mode") == "pair":
                await self._pair(ws, hello)
            elif hello.get("mode") == "resume":
                await self._session(ws, hello)
        except NET_ERRORS as e:
            log.info("Connection ended: %s", type(e).__name__)
        except Exception:
            log.exception("Unexpected error in connection")
        finally:
            if self._conns.pop(ws, None) is not None:
                self.ev.on_status(self.connected_names())

    async def _pair(self, ws, hello):
        code = self._pair_code
        if not code:
            await ws.send(json.dumps({"t": "error", "reason": "not_pairing"}))
            return
        ok = False
        try:
            p = Pairing(code, is_sender=False)
            key = p.finish(hello["spake"])
            await ws.send(json.dumps({"t": "spake", "spake": p.outbound, "id": self.id, "name": self.name,
                                      "confirm": mac(key, b"receiver-confirm")}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
            if reply.get("t") == "confirm" and mac_ok(key, b"sender-confirm", reply.get("mac", "")):
                name = str(hello.get("name", "PC 1"))[:64]
                paired = dict(self.cfg.get("paired", {}))
                paired[hello["id"]] = {"name": name, "key": b64e(key)}
                self.cfg.set("paired", paired)
                self._pair_code = None
                await ws.send(json.dumps({"t": "paired"}))
                ok = True
                log.info("Paired with %s", name)
                self.ev.on_paired(name)
        finally:
            if not ok:
                self._pair_failures += 1
                if self._pair_failures >= 3 and self._pair_code:
                    # Too many wrong tries - change the code so it can't be guessed.
                    self._pair_code = new_pair_code()
                    self._pair_failures = 0
                    self.ev.on_pair_code_changed(self._pair_code)
                self.ev.on_pair_failed()

    async def _session(self, ws, hello):
        sid = hello["id"]
        info = self.cfg.get("paired", {}).get(sid)
        if not info:
            await ws.send(json.dumps({"t": "error", "reason": "unknown_device"}))
            return
        nr = new_nonce()
        await ws.send(json.dumps({"t": "challenge", "nonce": b64e(nr), "id": self.id, "name": self.name}))
        ch = Channel(session_key(b64d(info["key"]), b64d(hello["nonce"]), nr), is_sender=False)
        first = await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT)
        if not isinstance(first, bytes) or ch.open(first).get("t") != "auth":
            return
        conn = {"id": sid, "name": info["name"], "ch": ch, "lock": asyncio.Lock()}
        self._conns[ws] = conn
        log.info("%s connected", info["name"])
        await self._send(ws, self.pause.message())
        self.ev.on_status(self.connected_names())
        async for raw in ws:
            if not isinstance(raw, bytes):
                continue
            msg = ch.open(raw)
            if msg.get("t") == "clip":
                text = str(msg.get("text", ""))[:MAX_TEXT]
                if text and not self.pause.paused:
                    self.ev.on_clip(text, info["name"])
            elif msg.get("t") == "pause":
                if self.pause.apply(msg):
                    self.ev.on_pause(self.pause.paused)
                    await self._broadcast(self.pause.message(), skip=ws)

    async def _send(self, ws, msg):
        conn = self._conns.get(ws)
        if not conn:
            return
        async with conn["lock"]:
            await ws.send(conn["ch"].seal(msg))

    async def _broadcast(self, msg, skip=None):
        for ws in list(self._conns):
            if ws is not skip:
                try:
                    await self._send(ws, msg)
                except NET_ERRORS:
                    pass

    def _set_paused(self, paused):
        msg = self.pause.set_local(paused)
        self.ev.on_pause(paused)
        asyncio.get_running_loop().create_task(self._broadcast(msg))

    def _drop(self, sender_id):
        for ws, c in list(self._conns.items()):
            if c["id"] == sender_id:
                asyncio.get_running_loop().create_task(ws.close())


# --------------------------------------------------------------------------- sender

class SenderCore:
    """Runs on PC 1. Keeps a connection to the paired receiver and sends clips."""

    def __init__(self, cfg, events=None, discovery=True):
        self.cfg = cfg
        self.ev = events or Events()
        self.discovery = discovery
        self.id = cfg.get("device_id")
        self.name = cfg.get("name") or socket.gethostname()
        self.pause = PauseState(cfg)
        self._lt = _LoopThread("clipsync-sender")
        self._ws = None
        self._ch = None
        self._lock = None
        self._wake = None
        self._pending = None  # (text, time)
        self._found = {}  # mDNS name -> {id, name, host(s), port}
        self._zc = None
        self.state = "starting"

    # ---- public API
    def start(self):
        self._lt.start(self._main)

    def stop(self):
        try:
            self._lt.submit(self._shutdown()).result(timeout=5)
        except Exception:
            pass
        self._lt.stop()

    def send_clip(self, text: str):
        self._lt.call(self._queue_clip, text)

    def set_paused(self, paused: bool):
        self._lt.call(self._set_paused, paused)

    def pair(self, host: str, port: int, code: str):
        """Pair with PC 2. Result arrives via events.on_pair_result(ok, message)."""
        self._lt.submit(self._pair(host, int(port), code.strip()))

    def unpair(self):
        self.cfg.set("receiver", None)
        self._lt.call(self._kick)

    def receiver(self):
        return self.cfg.get("receiver")

    def found(self) -> list[dict]:
        return list(self._found.values())

    # ---- internals
    async def _main(self):
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        if self.discovery:
            loop.create_task(self._browse())
        loop.create_task(self._run_forever())

    async def _shutdown(self):
        if self._ws:
            await self._ws.close()
        if self._zc:
            try:
                await self._zc.async_close()
            except Exception:
                pass
        await _cancel_others()

    def _set_state(self, state, detail=""):
        if state != self.state:
            self.state = state
            self.ev.on_status(state, detail)

    async def _browse(self):
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

        async def resolve(zc, name):
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            if await info.async_request(zc, 3000):
                props = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
                hosts = info.parsed_addresses(IPVersion.V4Only)
                if hosts and props.get("id"):
                    self._found[name] = {"id": props["id"], "name": props.get("name", "PC 2"),
                                         "hosts": hosts, "port": info.port}
                    self.ev.on_discovered(self.found())
                    rcv = self.receiver()
                    if rcv and rcv["id"] == props["id"] and self.state != "connected":
                        self._wake.set()

        def handler(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Removed:
                if self._found.pop(name, None):
                    self.ev.on_discovered(self.found())
            else:
                asyncio.get_running_loop().create_task(resolve(zeroconf, name))

        browser = None
        while True:
            try:
                self._zc = AsyncZeroconf()
                browser = AsyncServiceBrowser(self._zc.zeroconf, SERVICE_TYPE, handlers=[handler])
                await asyncio.Event().wait()  # runs until cancelled
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mDNS browse failed, retrying")
                try:
                    if browser:
                        await browser.async_cancel()
                    await self._zc.async_close()
                except Exception:
                    pass
                await asyncio.sleep(10)

    def _targets(self, rcv):
        targets = []
        for f in self._found.values():
            if f["id"] == rcv["id"]:
                targets += [(h, f["port"]) for h in f["hosts"]]
        if rcv.get("host"):
            targets.append((rcv["host"], rcv.get("port", DEFAULT_PORT)))
        seen, out = set(), []
        for t in targets:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    async def _run_forever(self):
        backoff = 1
        while True:
            try:
                rcv = self.receiver()
                if not rcv:
                    self._set_state("unpaired")
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                if self.state != "connected":
                    self._set_state("searching", rcv.get("name", "PC 2"))
                for host, port in self._targets(rcv):
                    if await self._session(rcv, host, port):
                        backoff = 1
                        break
                if self.state == "rejected":
                    backoff = 10  # PC 2 forgot us; keep the message up and retry slowly
                else:
                    self._set_state("searching", rcv.get("name", "PC 2"))
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 5) if self.state != "rejected" else 10
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Connection loop error, carrying on")
                await asyncio.sleep(2)

    async def _session(self, rcv, host, port) -> bool:
        """Returns True if we got connected (even if it later dropped)."""
        authed = False
        try:
            async with connect(f"ws://{host}:{port}", open_timeout=3, max_size=MAX_MSG,
                               ping_interval=15, ping_timeout=20) as ws:
                ns = new_nonce()
                await ws.send(json.dumps({"t": "hello", "v": PROTOCOL_VERSION, "mode": "resume",
                                          "id": self.id, "name": self.name, "nonce": b64e(ns)}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
                if reply.get("t") == "error":
                    if reply.get("reason") == "unknown_device":
                        self._set_state("rejected", rcv.get("name", "PC 2"))
                    return False
                if reply.get("t") != "challenge" or reply.get("id") != rcv["id"]:
                    return False
                ch = Channel(session_key(b64d(rcv["key"]), ns, b64d(reply["nonce"])), is_sender=True)
                self._ws, self._ch = ws, ch
                await self._send({"t": "auth"})
                await self._send(self.pause.message())
                authed = True
                if rcv.get("host") != host or rcv.get("port") != port:
                    self.cfg.set("receiver", {**rcv, "host": host, "port": port})
                self._set_state("connected", reply.get("name", rcv.get("name", "PC 2")))
                log.info("Connected to %s at %s:%s", rcv.get("name"), host, port)
                if self._pending and time.time() - self._pending[1] < PENDING_MAX_AGE and not self.pause.paused:
                    await self._send({"t": "clip", "text": self._pending[0]})
                self._pending = None
                async for raw in ws:
                    if isinstance(raw, bytes):
                        msg = ch.open(raw)
                        if msg.get("t") == "pause" and self.pause.apply(msg):
                            self.ev.on_pause(self.pause.paused)
        except NET_ERRORS as e:
            if authed:
                log.info("Connection lost: %s", type(e).__name__)
        finally:
            self._ws = self._ch = None
        return authed

    async def _send(self, msg):
        ws, ch = self._ws, self._ch
        if not ws:
            raise ConnectionError("not connected")
        async with self._lock:
            await ws.send(ch.seal(msg))

    def _queue_clip(self, text):
        if self.pause.paused or not text:
            return
        text = text[:MAX_TEXT]
        if self._ws:
            async def go():
                try:
                    await self._send({"t": "clip", "text": text})
                except (NET_ERRORS + (ConnectionError,)):
                    self._pending = (text, time.time())
            asyncio.get_running_loop().create_task(go())
        else:
            self._pending = (text, time.time())

    def _set_paused(self, paused):
        msg = self.pause.set_local(paused)
        self.ev.on_pause(paused)
        if self._ws:
            async def go():
                try:
                    await self._send(msg)
                except (NET_ERRORS + (ConnectionError,)):
                    pass
            asyncio.get_running_loop().create_task(go())

    def _kick(self):
        if self._ws:
            asyncio.get_running_loop().create_task(self._ws.close())
        self._wake.set()

    async def _pair(self, host, port, code):
        if not (len(code) == 6 and code.isdigit()):
            self.ev.on_pair_result(False, "The code should be 6 digits.")
            return
        try:
            async with connect(f"ws://{host}:{port}", open_timeout=5, max_size=MAX_MSG) as ws:
                p = Pairing(code, is_sender=True)
                await ws.send(json.dumps({"t": "hello", "v": PROTOCOL_VERSION, "mode": "pair",
                                          "id": self.id, "name": self.name, "spake": p.outbound}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
                if reply.get("t") == "error":
                    msg = {"not_pairing": "PC 2 isn't ready to pair. On PC 2, open Settings and click 'Pair a new PC'.",
                           "version": "The two apps are different versions. Update both."}
                    self.ev.on_pair_result(False, msg.get(reply.get("reason"), "PC 2 refused the pairing."))
                    return
                key = p.finish(reply["spake"])
                if not mac_ok(key, b"receiver-confirm", reply.get("confirm", "")):
                    await ws.send(json.dumps({"t": "confirm", "mac": ""}))
                    self.ev.on_pair_result(False, "Wrong code. Check the code shown on PC 2 and try again.")
                    return
                await ws.send(json.dumps({"t": "confirm", "mac": mac(key, b"sender-confirm")}))
                done = json.loads(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
                if done.get("t") != "paired":
                    self.ev.on_pair_result(False, "Pairing didn't finish. Try again.")
                    return
            self.cfg.set("receiver", {"id": reply["id"], "name": reply.get("name", "PC 2"),
                                      "key": b64e(key), "host": host, "port": port})
            log.info("Paired with %s", reply.get("name"))
            self.state = "unpaired"
            self._kick()
            self.ev.on_pair_result(True, f"Paired with {reply.get('name', 'PC 2')}.")
        except NET_ERRORS as e:
            log.info("Pairing failed: %r", e)
            self.ev.on_pair_result(False, f"Couldn't reach PC 2 at {host}. Check both PCs are on the same Wi-Fi "
                                          "and the ClipSync Receiver is running.")
        except Exception:
            log.exception("Pairing error")
            self.ev.on_pair_result(False, "Something went wrong while pairing. Try again.")
