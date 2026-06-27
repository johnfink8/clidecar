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
OnControl = Callable[
    ["Inbound"], None
]  # handle a message on the control channel (parse + act + reply)


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
    """A message to the user. `chat_id` is the target Discord channel (multi-agent: each agent owns
    one channel, so every outbound names where it lands). `kind`+`dedup_key` is the idempotency token,
    scoped per chat_id: the same logical message emitted twice is sent once, but two agents' identical
    text never cross-dedup. dedup_key=None opts out — for genuinely distinct messages like the
    streamed status frames."""

    text: str
    kind: Kind
    source: str  # "claude" | "gateway" | "tool"
    chat_id: str
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
    attach point for the AGENT FLEET. The gateway constructs ONE Broker with its `transport`, its
    `on_request` (answer one MCP request) and `notify` (build an inbound's channel frame), optionally
    an `on_control` (handle a control-channel command), calls serve() once (spawns the accept + reap
    threads), feeds it the channel→agent routes via set_routes(), and calls route_inbound() for every
    message it receives from the transport.

    Each agent's Claude attaches over the socket as a `channel`-role client (via the disposable stdio
    shim) announcing its agent id: the Broker keeps a registry `agent_id → socket`, pushes a message
    to the agent bound to its chat_id, and answers each agent's MCP requests through on_request. Per
    agent the newest attach wins; one channel maps to exactly one agent."""

    def __init__(
        self,
        transport: Transport,
        on_request: OnRequest,
        notify: Notify,
        on_control: OnControl | None = None,
        clock: Clock = time.time,
    ) -> None:
        self._transport = transport  # the ONLY caller of the adapter — strict lanes
        self._on_request = on_request
        self._notify = notify
        self._on_control = on_control
        self._clock = clock
        self._claims: list[_Claim] = []  # open waits, newest-first (a stack)
        self._emitted: set[tuple[str, str, str]] = set()  # (chat_id, kind, dedup_key)
        self._lock = threading.Lock()
        self._channels: dict[str, socket.socket] = {}  # agent_id → attached Claude (newest wins)
        self._routes: dict[str, str] = {}  # chat_id → agent_id (enabled agents)
        self._control_channel: str | None = None
        self._chan_lock = threading.Lock()  # guards _channels
        self._chan_write_lock = (
            threading.Lock()
        )  # serializes pushed notifications vs request responses across the agent sockets

    def set_routes(self, routes: "dict[str, str]", control_channel: str | None) -> None:
        """Install the channel→agent routing table + the control channel (from the fleet manifest).
        Replaced atomically on every fleet change — no recycle needed for a routing update."""
        self._routes = dict(routes)
        self._control_channel = control_channel

    def is_control(self, chat_id: str) -> bool:
        return self._control_channel is not None and chat_id == self._control_channel

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
        key = (out.chat_id, out.kind, out.dedup_key) if out.dedup_key is not None else None
        if key is not None:
            with self._lock:
                if key in self._emitted:
                    return None
                self._emitted.add(key)
        code, mid = self._op(
            "send", out.chat_id, out.text, *([out.reply_to] if out.reply_to else [])
        )
        if code != 0:
            if key is not None:
                with self._lock:
                    self._emitted.discard(
                        key
                    )  # release so a retry can re-emit — never silently drop
            return None
        return mid.strip() or None

    def edit(self, chat_id: str, message_id: str, text: str) -> bool:
        return self._op("edit", chat_id, message_id, text)[0] == 0

    def react(self, chat_id: str, message_id: str, emoji: str, add: bool = True) -> bool:
        return self._op("react" if add else "unreact", chat_id, message_id, emoji)[0] == 0

    def latest(self, chat_id: str) -> str | None:
        code, out = self._op("latest", chat_id)
        return out.strip() or None if code == 0 else None

    def create_channel(self, parent_chat_id: str, name: str) -> str | None:
        """Create a new channel as a sibling of `parent_chat_id` and return its id, or None on failure.
        Gateway-side only (control `spawn` derives an agent's channel) — the adapter is the gateway's
        alone (strict lanes), so there is no client-side wrapper for this."""
        code, out = self._op("create_channel", parent_chat_id, name)
        return (out.strip() or None) if code == 0 else None

    def route_inbound(self, msg: Inbound) -> str:
        """Route ONE inbound message to exactly one sink. Returns "exchange:<label>" if an open claim
        consumed it, "control" if it was a control-channel command, "claude:<agent>" if pushed to the
        agent bound to its channel, or "undelivered" if that channel maps to no attached agent (the
        Broker reacts ❌ in that channel — honest non-delivery). Total and deterministic — the user's
        reply is never double-handled."""
        with self._lock:
            for claim in list(self._claims):
                if claim.chat_id == msg.chat_id and _after(msg.id, claim.since_id):
                    self._claims.remove(claim)
                    try:
                        _send_line(claim.conn, {"reply": asdict(msg)})
                    except OSError:
                        claim.conn.close()
                        continue  # dead waiter — msg NOT consumed; fall through, never a silent drop
                    claim.conn.close()
                    return f"exchange:{claim.label}"
        if self.is_control(msg.chat_id):
            if self._on_control is not None:
                self._on_control(msg)  # parse + act + reply; never routed to an agent
            return "control"
        agent = self._routes.get(msg.chat_id)
        if agent is not None:
            with self._chan_lock:
                chan = self._channels.get(agent)
            if chan is not None:
                try:
                    with self._chan_write_lock:
                        _send_line(chan, self._notify(msg))
                    return f"claude:{agent}"
                except OSError:
                    with self._chan_lock:
                        if self._channels.get(agent) is chan:
                            del self._channels[agent]  # dead attach — drop it, fall through to ❌
        self._op("react", msg.chat_id, msg.id, NO_CLAUDE_REACT)
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
            # An agent attaching as the channel — the same buffered reader carries its MCP stream, so
            # no byte is lost between this handshake line and the frames that follow. The agent id
            # keys the registry; reject an anonymous attach loudly rather than register a wrong key.
            agent = req.get("agent")
            if not isinstance(agent, str) or not agent:
                conn.close()
                return
            self._serve_channel(conn, reader, agent)
            return
        op = req.get("op")
        if op == "ask":
            # Register the claim and LEAVE conn open — route_inbound (or the reaper) writes the
            # reply and closes it. The connection is the claim's liveness signal.
            chat_id = str(req.get("chat_id", ""))
            claim = _Claim(
                chat_id=chat_id,
                since_id=str(req.get("since_id", "")),
                conn=conn,
                expires_at=self._clock() + float(_num(req, "timeout", 600)),
                label=str(req.get("label", "")),
            )
            prompt = req.get("prompt")
            if isinstance(prompt, str) and prompt:
                self.emit(Outbound(text=prompt, kind="question", source="gateway", chat_id=chat_id))
            with self._lock:
                self._claims.insert(0, claim)
        elif op == "emit":
            mid = self.emit(
                Outbound(
                    text=str(req.get("text", "")),
                    kind=as_kind(req.get("kind")),
                    source=str(req.get("source", "gateway")),
                    chat_id=str(req.get("chat_id", "")),
                    dedup_key=_opt_str(req, "dedup_key"),
                    reply_to=_opt_str(req, "reply_to"),
                )
            )
            _send_line(conn, {"id": mid})
            conn.close()
        elif op == "edit":
            ok = self.edit(
                str(req.get("chat_id", "")),
                str(req.get("message_id", "")),
                str(req.get("text", "")),
            )
            _send_line(conn, {"ok": ok})
            conn.close()
        elif op == "react":
            ok = self.react(
                str(req.get("chat_id", "")),
                str(req.get("message_id", "")),
                str(req.get("emoji", "")),
                bool(req.get("add", True)),
            )
            _send_line(conn, {"ok": ok})
            conn.close()
        elif op == "latest":
            _send_line(conn, {"id": self.latest(str(req.get("chat_id", "")))})
            conn.close()
        elif op == "buttons":
            code, _ = self._op(
                "buttons",
                str(req.get("chat_id", "")),
                str(req.get("message_id", "")),
                str(_num(req, "timeout", 570)),
            )
            _send_line(conn, {"ok": code == 0})
            conn.close()
        elif op == "home":
            code, out = self._op("home")
            _send_line(conn, {"id": (out.strip() or None) if code == 0 else None})
            conn.close()
        else:
            conn.close()

    def _serve_channel(self, conn: socket.socket, reader: "IO[str]", agent: str) -> None:
        """Serve one attached agent's Claude over `conn`: it becomes the sink for inbound on its
        channel (pushed by route_inbound as notify() frames) and the source of MCP requests (answered
        via on_request). Per agent the newest attach wins — a fresh shim supersedes a stale one for
        the same id (the superseded socket is closed). The write lock serializes our pushed
        notifications against request responses across the agent sockets; on disconnect we drop this
        agent's entry only if we are still its current one (a newer attach must not be evicted)."""
        with self._chan_lock:
            old = self._channels.get(agent)
            if old is not None and old is not conn:
                try:
                    old.close()
                except OSError:
                    pass
            self._channels[agent] = conn
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
                if self._channels.get(agent) is conn:
                    del self._channels[agent]
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


def edit(chat_id: str, message_id: str, text: str, *, sock_path: str = SOCK_PATH) -> bool:
    resp = _request(
        {"op": "edit", "chat_id": chat_id, "message_id": message_id, "text": text}, sock_path
    )
    return bool(resp is not None and resp.get("ok"))


def react(
    chat_id: str, message_id: str, emoji: str, add: bool = True, *, sock_path: str = SOCK_PATH
) -> bool:
    resp = _request(
        {"op": "react", "chat_id": chat_id, "message_id": message_id, "emoji": emoji, "add": add},
        sock_path,
    )
    return bool(resp is not None and resp.get("ok"))


def latest(chat_id: str, *, sock_path: str = SOCK_PATH) -> str | None:
    resp = _request({"op": "latest", "chat_id": chat_id}, sock_path)
    v = resp.get("id") if resp is not None else None
    return v if isinstance(v, str) else None


def buttons(chat_id: str, message_id: str, timeout: float, *, sock_path: str = SOCK_PATH) -> bool:
    """Ask the gateway to attach approval buttons to an already-sent message, live for `timeout`
    seconds (the caller's reply wait — the single source for the button lifetime). False if the
    gateway is unreachable or the channel can't render them. Additive — the typed-reply path still
    claims a reply if buttons fail, so a False degrades gracefully rather than blocking approval."""
    resp = _request(
        {"op": "buttons", "chat_id": chat_id, "message_id": message_id, "timeout": timeout},
        sock_path,
    )
    return bool(resp is not None and resp.get("ok"))


def home(*, sock_path: str = SOCK_PATH) -> str | None:
    """The active channel's home chat id (resolved adapter-side from its config), or None if the
    gateway is unreachable / no home is configured. Lets a hook reach the user on a turn that carries
    no inbound chat_id (plan mode entered autonomously)."""
    resp = _request({"op": "home"}, sock_path)
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
