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
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import IO, Literal

import transcript as t

SOCK_PATH = os.path.expanduser("~/.clidecar/control/gateway.sock")
OP_RETRIES = (
    2  # the gateway retries a transient adapter failure so hooks never see a flaky transport
)
OP_BACKOFF_S = 0.5
NO_CLAUDE_REACT = (
    "❌"  # honest non-delivery: react when no Claude is attached, leaving control with the user
)

Kind = Literal[
    "message", "question", "notice"
]  # the outbound kinds; half of the (kind, dedup_key) idempotency token

Clock = Callable[[], float]
Transport = Callable[
    ..., tuple[int, str]
]  # the adapter shell-out: (verb, *args) -> (returncode, stdout)
OnRequest = Callable[["dict[str, object]"], "dict[str, object] | None"]
Notify = Callable[["Inbound"], "dict[str, object]"]


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
    kind: Kind
    source: str  # "claude" | "gateway" | "tool"
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
    """The gateway's transport mediator: a Unix-socket server + an in-memory claim registry + the
    attach point for Claude. The gateway constructs ONE Broker with its `transport`, its
    `on_request` (answer one MCP request) and `notify` (build an inbound's channel frame), calls
    serve() once (spawns the accept + reap threads), and calls route_inbound() for every message it
    receives from the transport.

    Claude attaches over the same socket as a `channel`-role client (via the disposable stdio shim):
    the Broker pushes unclaimed inbound to it as notify() frames and answers its MCP requests through
    on_request. There is at most one attached Claude — the newest attach wins."""

    def __init__(
        self, transport: Transport, on_request: OnRequest, notify: Notify, clock: Clock = time.time
    ) -> None:
        self._transport = transport  # the ONLY caller of the adapter — strict lanes
        self._on_request = on_request
        self._notify = notify
        self._clock = clock
        self._claims: list[_Claim] = []  # open waits, newest-first (a stack)
        self._emitted: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._channel: socket.socket | None = (
            None  # the attached Claude (newest shim wins); None = nobody home
        )
        self._chan_lock = threading.Lock()  # guards the _channel reference
        self._chan_write_lock = (
            threading.Lock()
        )  # serializes pushed notifications vs request responses on the one socket

    def _op(self, *args: str) -> tuple[int, str]:
        """Call the adapter, retrying a transient failure — the gateway is the ONE place transport
        retry/error-handling lives, so hooks never see (or work around) a flaky transport."""
        code, out = self._transport(*args)
        for _ in range(OP_RETRIES):
            if code == 0:
                break
            time.sleep(OP_BACKOFF_S)
            code, out = self._transport(*args)
        return code, out

    def emit(self, out: Outbound) -> str | None:
        """Emit ONE outbound message to the transport, exactly once, and RETURN its new message id —
        the status-message workflow needs the id to edit/cap it later. Dedup on (kind, dedup_key): the
        key is CLAIMED before the send and RELEASED on failure (claim/commit/release) — so a concurrent
        duplicate can't race in, yet a failed send doesn't permanently suppress a retry of the same
        logical message. Returns the new message id on success, or None if the send was deduped
        (dedup_key already emitted) or the transport failed. The single outbound funnel for every
        producer; with dedup_key=None it always sends (today's status/closing behaviour)."""
        key = (out.kind, out.dedup_key) if out.dedup_key is not None else None
        if key is not None:
            with self._lock:
                if key in self._emitted:
                    return None
                self._emitted.add(key)
        code, mid = self._op("send", out.text, *([out.reply_to] if out.reply_to else []))
        if code != 0:
            if key is not None:
                with self._lock:
                    self._emitted.discard(
                        key
                    )  # release so a retry can re-emit — never silently drop
            return None
        return mid.strip() or None

    def edit(self, message_id: str, text: str) -> bool:
        return self._op("edit", message_id, text)[0] == 0

    def react(self, message_id: str, emoji: str, add: bool = True) -> bool:
        return self._op("react" if add else "unreact", message_id, emoji)[0] == 0

    def latest(self) -> str | None:
        code, out = self._op("latest")
        return out.strip() or None if code == 0 else None

    def route_inbound(self, msg: Inbound) -> str:
        """Route ONE inbound message to exactly one sink. Returns "exchange:<label>" if an open claim
        consumed it, "claude" if pushed to the attached Claude, or "undelivered" if nobody is attached
        (the Broker reacts ❌ — honest non-delivery, control stays with the user). Total and
        deterministic — the user's reply is never double-handled."""
        with self._lock:
            for claim in list(self._claims):
                if claim.chat_id == msg.chat_id and _after(msg.id, claim.since_id):
                    self._claims.remove(claim)
                    try:
                        _send_line(claim.conn, {"reply": asdict(msg)})
                    except OSError:
                        claim.conn.close()
                        continue  # dead waiter — msg NOT consumed; fall through to Claude/❌, never a silent drop
                    claim.conn.close()
                    return f"exchange:{claim.label}"
        with self._chan_lock:
            chan = self._channel
        if chan is not None:
            try:
                with self._chan_write_lock:
                    _send_line(chan, self._notify(msg))
                return "claude"
            except OSError:
                with self._chan_lock:
                    if self._channel is chan:
                        self._channel = None  # dead attach — drop it and fall through to ❌
        self._op("react", msg.id, NO_CLAUDE_REACT)
        return "undelivered"

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
        try:
            reader = conn.makefile("r")
            first = reader.readline()
        except OSError:
            conn.close()
            return
        if not first:
            conn.close()
            return
        try:
            req = t.as_obj(json.loads(first))
        except json.JSONDecodeError:
            conn.close()
            return
        if req.get("role") == "channel":
            # Claude attaching as the channel — the same buffered reader carries its MCP stream, so
            # no byte is lost between this handshake line and the frames that follow.
            self._serve_channel(conn, reader)
            return
        op = req.get("op")
        if op == "ask":
            # Register the claim and LEAVE conn open — route_inbound (or the reaper) writes the
            # reply and closes it. The connection is the claim's liveness signal.
            claim = _Claim(
                chat_id=str(req.get("chat_id", "")),
                since_id=str(req.get("since_id", "")),
                conn=conn,
                expires_at=self._clock() + float(_num(req, "timeout", 600)),
                label=str(req.get("label", "")),
            )
            prompt = req.get("prompt")
            if isinstance(prompt, str) and prompt:
                self.emit(Outbound(text=prompt, kind="question", source="gateway"))
            with self._lock:
                self._claims.insert(0, claim)
        elif op == "emit":
            mid = self.emit(
                Outbound(
                    text=str(req.get("text", "")),
                    kind=as_kind(req.get("kind")),
                    source=str(req.get("source", "gateway")),
                    dedup_key=_opt_str(req, "dedup_key"),
                    reply_to=_opt_str(req, "reply_to"),
                )
            )
            _send_line(conn, {"id": mid})
            conn.close()
        elif op == "edit":
            ok = self.edit(str(req.get("message_id", "")), str(req.get("text", "")))
            _send_line(conn, {"ok": ok})
            conn.close()
        elif op == "react":
            ok = self.react(
                str(req.get("message_id", "")),
                str(req.get("emoji", "")),
                bool(req.get("add", True)),
            )
            _send_line(conn, {"ok": ok})
            conn.close()
        elif op == "latest":
            _send_line(conn, {"id": self.latest()})
            conn.close()
        else:
            conn.close()

    def _serve_channel(self, conn: socket.socket, reader: "IO[str]") -> None:
        """Serve an attached Claude over `conn`: it becomes the sink for unclaimed inbound (pushed by
        route_inbound as notify() frames) and the source of MCP requests (answered via on_request).
        Newest attach wins — a fresh shim supersedes a stale one. The write lock serializes our pushed
        notifications against these request responses on the one socket; on disconnect we clear the
        channel only if we are still the current one (a newer attach must not be evicted)."""
        with self._chan_lock:
            self._channel = conn
        try:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                resp = self._on_request(t.as_obj(parsed))
                if resp is not None:
                    with self._chan_write_lock:
                        _send_line(conn, resp)
        except OSError:
            pass
        finally:
            with self._chan_lock:
                if self._channel is conn:
                    self._channel = None
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


def ask(
    chat_id: str,
    prompt: str | None,
    *,
    since_id: str,
    timeout: float,
    label: str,
    sock_path: str = SOCK_PATH,
) -> Inbound | None:
    """Open an Exchange and BLOCK for the user's reply — the "ask and wait" entry point for a hook or
    skill. Connects to the gateway, hands it the prompt + claim, and blocks on the socket until the
    gateway pushes the reply (or the claim lapses). Returns the Inbound reply, or None if the gateway
    is unreachable or the wait lapses — the caller then falls back (e.g. ask in prose). `since_id` is
    the newest message id at open time, so only a genuinely NEW reply is claimed, never a backlog."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout + 5)
        conn.connect(sock_path)
        _send_line(
            conn,
            {
                "op": "ask",
                "chat_id": chat_id,
                "prompt": prompt,
                "since_id": since_id,
                "timeout": timeout,
                "label": label,
            },
        )
        resp = _recv_line(conn)
        conn.close()
    except OSError:
        return None  # gateway not reachable → caller falls back (fail-loud)
    if resp is None:
        return None
    return Inbound.from_obj(resp.get("reply"))  # absent ("lapsed") → from_obj(None) → None


def _request(
    payload: dict[str, object], sock_path: str, timeout: float = 10
) -> dict[str, object] | None:
    """One request→response round-trip to the gateway socket; None if the gateway is unreachable
    (the caller fails loud — never reaches around to the adapter)."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(sock_path)
        _send_line(conn, payload)
        resp = _recv_line(conn)
        conn.close()
        return resp
    except OSError:
        return None


def emit(out: Outbound, *, sock_path: str = SOCK_PATH) -> str | None:
    """Ask the gateway to emit one outbound message, deduped, and return its new message id — or None
    if the send was deduped or the gateway is unreachable. Every hook send goes through here: the
    adapter is the gateway's alone (strict lanes), so a None means fail-loud, never an adapter retry."""
    resp = _request({"op": "emit", **asdict(out)}, sock_path)
    if resp is None:
        return None
    mid = resp.get("id")
    return mid if isinstance(mid, str) else None


def edit(message_id: str, text: str, *, sock_path: str = SOCK_PATH) -> bool:
    resp = _request({"op": "edit", "message_id": message_id, "text": text}, sock_path)
    return bool(resp is not None and resp.get("ok"))


def react(message_id: str, emoji: str, add: bool = True, *, sock_path: str = SOCK_PATH) -> bool:
    resp = _request(
        {"op": "react", "message_id": message_id, "emoji": emoji, "add": add}, sock_path
    )
    return bool(resp is not None and resp.get("ok"))


def latest(*, sock_path: str = SOCK_PATH) -> str | None:
    resp = _request({"op": "latest"}, sock_path)
    v = resp.get("id") if resp is not None else None
    return v if isinstance(v, str) else None


def as_kind(v: object) -> Kind:
    """Narrow a wire-supplied kind to a known value — the emit op crosses the socket as untyped JSON."""
    if v == "message":
        return "message"
    if v == "question":
        return "question"
    return "notice"


def _num(req: dict[str, object] | None, key: str, default: float) -> float:
    v = req.get(key) if req else None
    return float(v) if isinstance(v, (int, float)) else default


def _opt_str(req: dict[str, object], key: str) -> str | None:
    v = req.get(key)
    return v if isinstance(v, str) else None
