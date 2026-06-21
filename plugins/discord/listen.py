#!/usr/bin/env python3
"""Stream inbound Discord messages over the Gateway WebSocket as deliverable JSON lines (push).

`msg.sh listen`: holds a Discord Gateway (v10, JSON, uncompressed) connection and writes one
gate.shape() line per new allowed message in the configured channel to stdout, flushed, as it
arrives — the push counterpart to poll.py's REST batch. The clidecar gateway reads this stream
instead of polling, so there's no interval latency and no rate-limit stall.

Dependency-free on purpose: a minimal hand-rolled WS client (RFC 6455 client framing) over an ssl
socket keeps the adapter installable with nothing but python3, and the surface stays swappable
behind the `listen` verb if it ever needs a real library.

Fail-loud: a fatal Gateway close — bad token (4004) or disallowed/invalid intents (4013/4014, i.e.
the MESSAGE_CONTENT privileged intent isn't enabled) — exits non-zero with the reason instead of
reconnecting forever, so the core escalates. Transient drops reconnect (RESUME, else re-IDENTIFY).
"""
import base64
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse

import gate

# Guilds | GuildMessages | DirectMessages | MessageContent — same set the official plugin uses.
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
GATEWAY_HOST = "gateway.discord.gg"  # stable; resumes use resume_gateway_url from READY
GATEWAY_PARAMS = "/?v=10&encoding=json"
USER_AGENT = "DiscordBot (https://github.com/johnfink8/clidecar, 0.1)"  # Discord 403s the default urllib UA
SOCKET_TIMEOUT = 90.0  # backstop; the heartbeat (~41s interval) keeps real traffic flowing
MAX_RECONNECT_FAILS = 6  # connects-without-READY before giving up so the core falls back to poll
FATAL_EXIT = 4  # exit code the core (gateway.LISTEN_FATAL_EXIT) reads as "fall back to REST poll"
# Closes that won't fix themselves by reconnecting — surface them, don't loop.
FATAL_CLOSE = {4004, 4010, 4011, 4012, 4013, 4014}


class GatewayClose(Exception):
    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"gateway close {code}: {reason}")
        self.code = code
        self.reason = reason


class FatalGateway(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(f"fatal gateway close {code}: {reason}")
        self.code = code
        self.reason = reason


class WS:
    """Minimal RFC 6455 client. recv_json() transparently answers ping and raises on close, so
    callers only ever see data frames."""

    def __init__(self, host: str, path: str):
        raw = socket.create_connection((host, 443), timeout=30)
        raw.settimeout(SOCKET_TIMEOUT)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self._buf = b""
        self._send_lock = threading.Lock()
        self._handshake(host, path)

    def _handshake(self, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"User-Agent: {USER_AGENT}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("gateway closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise ConnectionError(f"gateway handshake failed: {status!r}")

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("gateway closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_json(self) -> "dict[str, object]":
        """Control frames are handled inline — ping is answered, close raises GatewayClose — so a
        caller only ever gets a decoded data frame back."""
        data = b""
        while True:
            b0, b1 = self._read(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = int.from_bytes(self._read(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if b1 & 0x80 else b""
            payload = self._read(length)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if op == 0x8:
                code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 1006
                raise GatewayClose(code, payload[2:].decode("utf-8", "replace"))
            if op == 0x9:
                self._send(0xA, payload)
                continue
            if op == 0xA:
                continue
            data += payload
            if fin:
                parsed = json.loads(data.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        n = len(payload)
        if n < 126:
            header = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, "big")
        with self._send_lock:
            self.sock.sendall(header + mask + masked)

    def send_json(self, obj: "dict[str, object]") -> None:
        self._send(0x1, json.dumps(obj).encode())

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _heartbeat(ws: WS, state: "dict[str, object]", interval: float, stop: threading.Event) -> None:
    """Beat every interval; if the prior beat went un-ACKed the link is a zombie, so close the
    socket to break recv and force a reconnect."""
    if stop.wait(interval * 0.5):  # first beat at half-interval, per Discord's jitter guidance
        return
    while not stop.is_set():
        if not state["ack"]:
            ws.close()
            return
        state["ack"] = False
        try:
            ws.send_json({"op": 1, "d": state["seq"]})
        except OSError:
            return
        if stop.wait(interval):
            return


def run_once(token: str, channel_id: str, session: "dict[str, object] | None") -> "tuple[bool, dict[str, object] | None]":
    """Connect once and stream until the link drops. RESUME when given a session, else IDENTIFY.
    Returns (connected, session): connected is True only if THIS attempt reached READY/RESUMED —
    keyed to the live handshake, not the inherited session_id, so a flapping resume that never
    re-handshakes still counts as a failure and the caller's give-up bound bites. session is what
    to reconnect with (None = re-identify fresh); raises FatalGateway on a close that won't recover."""
    resume_url = (session or {}).get("resume_url")
    host = urllib.parse.urlparse(str(resume_url)).hostname if resume_url else GATEWAY_HOST
    allow = gate.allowed_senders()
    ws = WS(host, GATEWAY_PARAMS)
    stop = threading.Event()
    state: "dict[str, object]" = {"seq": (session or {}).get("seq"), "ack": True}
    hb: "threading.Thread | None" = None
    sess: "dict[str, object]" = dict(session) if session else {}
    connected = False
    try:
        hello = ws.recv_json()
        hd = hello.get("d") if isinstance(hello.get("d"), dict) else {}
        interval = float(hd.get("heartbeat_interval", 41250)) / 1000.0
        hb = threading.Thread(target=_heartbeat, args=(ws, state, interval, stop), daemon=True)
        hb.start()
        if session:
            ws.send_json({"op": 6, "d": {"token": token,
                                         "session_id": session.get("session_id"), "seq": session.get("seq")}})
        else:
            ws.send_json({"op": 2, "d": {"token": token, "intents": INTENTS,
                                         "properties": {"os": "linux", "browser": "clidecar", "device": "clidecar"}}})
        while True:
            msg = ws.recv_json()
            op = msg.get("op")
            if op == 0:
                state["seq"] = sess["seq"] = msg.get("s")
                event, d = msg.get("t"), msg.get("d")
                d = d if isinstance(d, dict) else {}
                if event == "READY":
                    connected = True
                    sess["session_id"] = d.get("session_id")
                    rgu = d.get("resume_gateway_url")
                    sess["resume_url"] = rgu + GATEWAY_PARAMS if isinstance(rgu, str) else None
                    sys.stderr.write("discord listen: READY\n")
                elif event == "RESUMED":
                    connected = True
                    sys.stderr.write("discord listen: RESUMED\n")
                elif event == "MESSAGE_CREATE" and d.get("channel_id") == channel_id:
                    line = gate.shape(d, allow)
                    if line:
                        sys.stdout.write(line + "\n")
                        sys.stdout.flush()
            elif op == 1:
                state["ack"] = False
                ws.send_json({"op": 1, "d": state["seq"]})
            elif op == 7:
                return connected, sess
            elif op == 9:
                return connected, (sess if msg.get("d") else None)
            elif op == 11:
                state["ack"] = True
    except GatewayClose as e:
        if e.code in FATAL_CLOSE:
            raise FatalGateway(e.code, e.reason)
        return connected, (sess if sess.get("session_id") else None)
    except (OSError, ConnectionError, ssl.SSLError, ValueError):
        return connected, (sess if sess.get("session_id") else None)
    finally:
        stop.set()
        ws.close()
        if hb:
            hb.join(timeout=2)


def main() -> int:
    """Exit non-zero (FATAL_EXIT) on anything the core should fall back to REST polling over: a
    fatal Gateway close, missing creds, OR a run of connects that never reach READY — so a
    persistently-broken-but-not-closing link can't spin here forever, invisible to the core."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        sys.stderr.write("discord listen: DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID not set\n")
        return FATAL_EXIT
    session: "dict[str, object] | None" = None
    fails = 0
    backoff = 1.0
    while True:
        try:
            connected, session = run_once(token, channel_id, session)
        except FatalGateway as e:
            sys.stderr.write(f"discord listen: FATAL {e.code}: {e.reason}\n")
            if e.code in (4013, 4014):
                sys.stderr.write("discord listen: enable the MESSAGE CONTENT privileged intent in the Discord dev portal\n")
            return FATAL_EXIT
        except Exception as e:
            sys.stderr.write(f"discord listen: connect error: {e!r}\n")
            connected, session = False, None
        if connected:  # handshaked this attempt — healthy; resume after a brief gap
            fails, backoff = 0, 1.0
            time.sleep(1.0)
            continue
        fails += 1
        if fails >= MAX_RECONNECT_FAILS:
            sys.stderr.write(f"discord listen: {fails} connects without a handshake — giving up so the core falls back\n")
            return FATAL_EXIT
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    sys.exit(main())
