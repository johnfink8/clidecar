#!/usr/bin/env python3
"""MCP-stdio shim — the disposable per-Claude attach point to the persistent gateway daemon.

Claude Code spawns THIS as its `clidecar` channel server (an stdio MCP server). It owns no transport
and no state: it just proxies MCP JSON-RPC line-for-line between CC's stdio and the supervisor-owned
gateway daemon over a Unix socket. The daemon survives recycles (it holds the Discord WS, the broker,
and the inbound buffer); this shim lives and dies with each Claude. Two pumps: CC stdin → daemon, and
daemon → CC stdout.

The first line announces the connection's role so the daemon can tell a channel attach from a broker
client (ask/emit) on the same socket. If the daemon isn't up yet (the supervisor launches it before
Claude, but races happen) the shim retries briefly, then exits — CC sees the channel fail loudly
rather than the shim hanging.
"""

import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from exchange import SOCK_PATH  # the daemon owns the socket path; the shim must not re-declare it

CONNECT_TRIES = 50
CONNECT_DELAY = 0.2


def _connect() -> socket.socket | None:
    for _ in range(CONNECT_TRIES):
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(SOCK_PATH)
            return conn
        except OSError:
            time.sleep(CONNECT_DELAY)
    return None


def main() -> int:
    conn = _connect()
    if conn is None:
        sys.stderr.write(f"clidecar gateway-shim: daemon socket {SOCK_PATH} unreachable\n")
        return 1
    # Role handshake: this is the MCP channel (not a broker client), and WHICH agent it is. The
    # supervisor exports CLIDECAR_AGENT_ID when it launches each agent's Claude. Fail LOUD if it's
    # unset rather than attach anonymously: the daemon keys its registry (newest-wins) on this id, so
    # a wrong/empty id would silently EVICT a real agent's socket and hijack its channel routing.
    agent = os.environ.get("CLIDECAR_AGENT_ID", "").strip()
    if not agent:
        sys.stderr.write("clidecar gateway-shim: CLIDECAR_AGENT_ID unset — refusing to attach\n")
        conn.close()
        return 1
    conn.sendall((json.dumps({"role": "channel", "agent": agent}) + "\n").encode())

    def daemon_to_cc() -> None:
        with conn.makefile("r", encoding="utf-8") as fh:
            for line in fh:
                sys.stdout.write(line)
                sys.stdout.flush()
        # daemon closed (it died / restarted): end the shim so CC drops the channel cleanly
        try:
            sys.stdin.close()
        except OSError:
            pass

    threading.Thread(target=daemon_to_cc, daemon=True).start()
    try:
        for line in sys.stdin:
            conn.sendall(line.encode())
    except OSError:
        pass
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
