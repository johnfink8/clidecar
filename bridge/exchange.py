"""The gateway exchange protocol — the two-way contract for the transport boundary.

The gateway is the SOLE owner of the transport in both directions. Two invariants, one per direction:

    INBOUND  (user → transport → ?):  every message is routed to EXACTLY ONE sink — an open claim
                                      that wants it, else Claude. A claimed message is consumed by
                                      the claim and NEVER also delivered to Claude.

    OUTBOUND (? → transport → user):  every message is emitted EXACTLY ONCE regardless of producer.
                                      Claude's hooks, Claude's reply tool, and gateway-side code all
                                      go through emit(), which drops a repeat (kind, dedup_key).

The transport boundary is mediated by a Unix-domain socket the gateway owns — NOT files. A hook (or
skill) is a socket CLIENT: it connects, registers a wait, and BLOCKS on the socket read; the gateway
pushes the reply back on that same connection. The connection's lifetime IS the claim's lifetime — a
crashed or finished client drops its socket, and the gateway reaps the claim. No polling, no stale
files, no hand-rolled atomicity. Claims live in the gateway's memory.

A claim is the inbound half of an Exchange: a deterministic request→reply cycle owned by gateway-side
code, not Claude's turn loop — post a prompt, block for the user's next reply, return it. Claude is
not in the loop and never sees the reply, so it can't double-handle it. Pending AskUserQuestion is
the first instance; "dump this file, wait for confirm" is another.
"""
import json
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

import transcript as t

SOCK_PATH = os.path.expanduser("~/.clidecar/control/gateway.sock")

Clock = Callable[[], float]
Send = Callable[[str, str | None], str | None]   # (text, reply_to) -> sent message id, or None
Deliver = Callable[["Inbound"], None]            # hand a message to Claude as a <channel> prompt


@dataclass(frozen=True)
class Inbound:
    """A message from the user. `id` is the transport's monotonic message id (a Discord snowflake);
    routing orders on it, not the wall-clock ts."""
    id: str
    chat_id: str
    user: str
    user_id: str
    content: str
    ts: str

    @classmethod
    def from_obj(cls, obj: object) -> "Inbound | None":
        d = t.as_obj(obj)
        vals = {f: d.get(f) for f in ("id", "chat_id", "user", "user_id", "content", "ts")}
        if not all(isinstance(v, str) for v in vals.values()):
            return None  # a malformed inbound is dropped, never half-trusted
        return cls(**{k: v for k, v in vals.items() if isinstance(v, str)})


@dataclass(frozen=True)
class Outbound:
    """A message to the user. `kind`+`dedup_key` is the idempotency token: the same logical message
    emitted twice (Claude's closing hook vs. a gateway echo of it) is sent once. dedup_key=None opts
    out — for genuinely distinct messages like the streamed status frames."""
    text: str
    kind: str                      # "status" | "closing" | "question" | "file" | "notice" | "reply"
    source: str                    # "claude" | "gateway" | "tool"
    dedup_key: str | None = None
    reply_to: str | None = None


@dataclass
class _Claim:
    """The inbound half of an open Exchange, held in the gateway's memory. `conn` is the live client
    socket the reply is written back on; closing it ends the claim."""
    chat_id: str
    since_id: str
    conn: socket.socket
    expires_at: float
    label: str = ""


def _after(msg_id: str, since_id: str) -> bool:
    """msg_id strictly newer than since_id. Snowflakes are monotonic ints; string-compare only if
    either isn't numeric (defensive — transport ids are numeric in practice)."""
    if msg_id.isdigit() and since_id.isdigit():
        return int(msg_id) > int(since_id)
    return msg_id > since_id


def _send_line(conn: socket.socket, obj: object) -> None:
    conn.sendall((json.dumps(obj) + "\n").encode())


def _recv_line(conn: socket.socket) -> dict[str, object] | None:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None  # peer closed before a full line
        buf.extend(chunk)
    try:
        return t.as_obj(json.loads(bytes(buf).split(b"\n", 1)[0]))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- gateway side

class Broker:
    """The gateway's transport mediator: a Unix-socket server + an in-memory claim registry. The
    gateway constructs ONE Broker with its transport `send` and its `deliver`-to-Claude, calls
    serve() once (spawns the accept + reap threads), and calls route_inbound() for every message it
    receives from the transport."""

    def __init__(self, send: Send, deliver: Deliver, clock: Clock = time.time) -> None:
        self._send = send
        self._deliver = deliver
        self._clock = clock
        self._claims: list[_Claim] = []          # open waits, newest-first (a stack)
        self._emitted: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def emit(self, out: Outbound) -> bool:
        """Emit ONE outbound message to the transport, exactly once. Dedup on (kind, dedup_key); a
        repeat returns False. The single outbound funnel for every producer."""
        if out.dedup_key is not None:
            key = (out.kind, out.dedup_key)
            with self._lock:
                if key in self._emitted:
                    return False
                self._emitted.add(key)
        self._send(out.text, out.reply_to)
        return True

    def route_inbound(self, msg: Inbound) -> str:
        """Route ONE inbound message to exactly one sink. Returns "exchange:<label>" if an open claim
        consumed it (NOT delivered to Claude), or "claude" if delivered as a prompt. Total and
        deterministic — the user's reply is never double-handled."""
        with self._lock:
            for claim in list(self._claims):
                if claim.chat_id == msg.chat_id and _after(msg.id, claim.since_id):
                    self._claims.remove(claim)
                    try:
                        _send_line(claim.conn, {"reply": asdict(msg)})
                    finally:
                        claim.conn.close()
                    return f"exchange:{claim.label}"
        self._deliver(msg)
        return "claude"

    def serve(self, sock_path: str = SOCK_PATH) -> None:
        try:
            os.unlink(sock_path)  # clear a stale socket from a prior gateway
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        threading.Thread(target=self._reap, daemon=True).start()

    def _accept(self, srv: socket.socket) -> None:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        req = _recv_line(conn)
        if req is None:
            conn.close()
            return
        op = req.get("op")
        if op == "ask":
            # Register the claim and LEAVE conn open — route_inbound (or the reaper) writes the
            # reply and closes it. The connection is the claim's liveness signal.
            claim = _Claim(
                chat_id=str(req.get("chat_id", "")), since_id=str(req.get("since_id", "")),
                conn=conn, expires_at=self._clock() + float(_num(req, "timeout", 600)),
                label=str(req.get("label", "")),
            )
            prompt = req.get("prompt")
            if isinstance(prompt, str) and prompt:
                self.emit(Outbound(text=prompt, kind="question", source="gateway"))
            with self._lock:
                self._claims.insert(0, claim)
        elif op == "emit":
            sent = self.emit(Outbound(
                text=str(req.get("text", "")), kind=str(req.get("kind", "notice")),
                source=str(req.get("source", "gateway")),
                dedup_key=_opt_str(req, "dedup_key"), reply_to=_opt_str(req, "reply_to"),
            ))
            _send_line(conn, {"sent": sent})
            conn.close()
        else:
            conn.close()

    def _reap(self) -> None:
        """Lapse expired claims and drop dead clients, so a claim never wedges inbound forever."""
        while True:
            time.sleep(1.0)
            now = self._clock()
            with self._lock:
                for claim in list(self._claims):
                    if claim.expires_at <= now:
                        self._claims.remove(claim)
                        try:
                            _send_line(claim.conn, {"lapsed": True})
                        except OSError:
                            pass
                        finally:
                            claim.conn.close()


# --------------------------------------------------------------------------- client side (hook/skill)

def ask(chat_id: str, prompt: str | None, *, since_id: str, timeout: float, label: str,
        sock_path: str = SOCK_PATH) -> Inbound | None:
    """Open an Exchange and BLOCK for the user's reply — the "ask and wait" entry point for a hook or
    skill. Connects to the gateway, hands it the prompt + claim, and blocks on the socket until the
    gateway pushes the reply (or the claim lapses). Returns the Inbound reply, or None if the gateway
    is unreachable or the wait lapses — the caller then falls back (e.g. ask in prose). `since_id` is
    the newest message id at open time, so only a genuinely NEW reply is claimed, never a backlog."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout + 5)
        conn.connect(sock_path)
        _send_line(conn, {"op": "ask", "chat_id": chat_id, "prompt": prompt,
                          "since_id": since_id, "timeout": timeout, "label": label})
        resp = _recv_line(conn)
        conn.close()
    except OSError:
        return None  # gateway not reachable → caller falls back (fail-loud)
    if resp is None:
        return None
    return Inbound.from_obj(resp.get("reply"))  # absent ("lapsed") → from_obj(None) → None


def emit(out: Outbound, *, sock_path: str = SOCK_PATH) -> bool:
    """Ask the gateway to emit one outbound message, deduped — for gateway-side producers that aren't
    the gateway process itself (a hook dumping file contents). Returns True if sent, False if deduped
    or the gateway is unreachable."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(10)
        conn.connect(sock_path)
        _send_line(conn, {"op": "emit", **{k: v for k, v in asdict(out).items()}})
        resp = _recv_line(conn)
        conn.close()
    except OSError:
        return False
    return bool(resp is not None and resp.get("sent"))


def _num(req: dict[str, object] | None, key: str, default: float) -> float:
    v = req.get(key) if req else None
    return float(v) if isinstance(v, (int, float)) else default


def _opt_str(req: dict[str, object], key: str) -> str | None:
    v = req.get(key)
    return v if isinstance(v, str) else None
