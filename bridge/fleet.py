"""The fleet manifest — clidecar's desired-state for the multi-agent topology.

ONE SQLite store, `~/.clidecar/fleet.db`, is the single source of truth: which agents exist, each
agent's bound Discord channel + workdir + launch args + enabled flag, and the control channel. Three
readers share it: the supervisor (bash, via bin/_fleetctl.py) reconciles processes to it; the gateway
daemon derives the channel→agent routing table + the adapter's listen-set from it; the control parser
mutates it. The control language and `clidecar agent …` write through save() (one transaction per
write, so concurrent readers never see a half-applied fleet).

Validation is fail-loud and total: load() returns (None, reason) on a read error or any invariant
break, and EVERY caller must treat that as "keep the last-known-good fleet and alert" — NEVER as
"zero agents". A read NEVER creates the store: load() opens it read-only, so an absent db reports
"does not exist" (fail-closed) instead of materializing an empty fleet that would reconcile the whole
fleet to death. The store is created only by save() (and the one-time json migration).
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import cast

FLEET_DB = os.path.expanduser("~/.clidecar/fleet.db")

# Launch args a new agent gets when none are given. The dev-channel flag is load-bearing: without it
# the agent never attaches to the gateway as a channel, so it can't receive inbound. One source of
# truth for every create path (control `spawn`, `clidecar agent add`, the seed fallback).
DEFAULT_AGENT_ARGS = (
    "--permission-mode auto --dangerously-load-development-channels server:clidecar"
)

# Agent ids name screen sessions (clidecar-<id>) and data dirs, so keep them filesystem- and
# shell-safe: lowercase alnum, dash, underscore, not leading with a separator.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id      TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    workdir TEXT NOT NULL,
    args    TEXT NOT NULL,
    enabled INTEGER NOT NULL
);
"""


def valid_id(agent_id: str) -> bool:
    """An id safe for screen sessions, data dirs, and a derived workspace path. Callers that create
    side effects (dirs, channels) before save() check this FIRST so a bad id costs nothing."""
    return bool(_ID_RE.match(agent_id))


@dataclass(frozen=True)
class Agent:
    id: str
    channel: str  # the Discord channel id this agent owns (routing key)
    workdir: str
    args: str
    enabled: bool


@dataclass(frozen=True)
class Fleet:
    control_channel: str | None
    agents: dict[str, Agent]

    def enabled(self) -> "list[Agent]":
        return [a for a in self.agents.values() if a.enabled]

    def routes(self) -> dict[str, str]:
        """chat_id → agent_id for ENABLED agents only — a disabled/removed agent's channel is simply
        not routed (and not listened to), so messaging it is a silent no-op, not a ❌ on a channel you
        deliberately turned off."""
        return {a.channel: a.id for a in self.enabled()}

    def listen_channels(self) -> set[str]:
        chans = {a.channel for a in self.enabled()}
        if self.control_channel:
            chans.add(self.control_channel)
        return chans


def _validate(fleet: Fleet) -> str | None:
    """Invariants — any break is a config error the caller surfaces loudly, never reconciles against."""
    seen_channels: dict[str, str] = {}
    for agent_id, agent in fleet.agents.items():
        if not _ID_RE.match(agent_id):
            return f"agent id {agent_id!r} does not match {_ID_RE.pattern}"
        if not agent.channel.isdigit():
            return f"agent {agent_id!r} channel {agent.channel!r} is not a numeric id"
        if agent.channel == fleet.control_channel:
            return f"agent {agent_id!r} channel collides with control_channel {fleet.control_channel!r}"
        if agent.channel in seen_channels:
            return (
                f"channel {agent.channel!r} bound to both {seen_channels[agent.channel]!r} "
                f"and {agent_id!r} — one channel per agent"
            )
        seen_channels[agent.channel] = agent_id
    return None


def _agent_from_row(row: "tuple[object, ...]") -> "tuple[Agent | None, str | None]":
    if len(row) != 5:
        return None, f"agent row has {len(row)} columns, expected 5"
    rid, channel, workdir, args, enabled = row
    if not isinstance(rid, str):
        return None, "agent row has a non-string id"
    if not isinstance(channel, str) or not channel:
        return None, f"agent {rid!r} missing a string 'channel'"
    if not isinstance(workdir, str) or not workdir:
        return None, f"agent {rid!r} missing a string 'workdir'"
    if not isinstance(args, str):
        return None, f"agent {rid!r} 'args' must be a string"
    return Agent(id=rid, channel=channel, workdir=workdir, args=args, enabled=bool(enabled)), None


def load(path: str | None = None) -> "tuple[Fleet | None, str | None]":
    """The validated fleet, or (None, reason). An absent store is reported as such so the caller can
    seed it; a present-but-broken store is a loud read error — never silently an empty fleet.

    Opens the db READ-ONLY: a non-existent file can never be created here (which would read as an
    empty fleet and reconcile every agent to death). `path` resolves to FLEET_DB at CALL time, not
    def time — so a test that redirects it actually redirects load/save."""
    path = path or FLEET_DB
    _ensure_migrated(path)
    if not os.path.exists(path):
        return None, f"fleet store {path} does not exist"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            mrow = cast(
                "tuple[object] | None",
                conn.execute("SELECT value FROM meta WHERE key='control_channel'").fetchone(),
            )
            rows = cast(
                "list[tuple[object, ...]]",
                conn.execute("SELECT id, channel, workdir, args, enabled FROM agents").fetchall(),
            )
        finally:
            conn.close()
    except sqlite3.Error as e:
        return None, f"fleet store {path} unreadable: {e}"
    if mrow is None:
        # save() ALWAYS writes the control_channel meta row inside its data transaction, so an absent
        # one means the tables exist but that transaction never committed — a crash or IO error during
        # the first-ever write (schema is created in autocommit, before the data txn). Fail closed:
        # reading this as a valid empty fleet would reconcile every agent to death.
        return (
            None,
            f"fleet store {path} is uninitialized (no control_channel row — interrupted first write)",
        )
    control_channel = mrow[0] if isinstance(mrow[0], str) and mrow[0] else None
    agents: dict[str, Agent] = {}
    for row in rows:
        agent, reason = _agent_from_row(row)
        if agent is None:
            return None, reason
        agents[agent.id] = agent
    fleet = Fleet(control_channel=control_channel, agents=agents)
    reason = _validate(fleet)
    if reason is not None:
        return None, reason
    return fleet, None


def save(fleet: Fleet, path: str | None = None) -> str | None:
    """Persist the whole fleet in ONE transaction (DELETE + re-INSERT every agent + the control_channel
    meta row). Validates FIRST — refuses to persist an invariant-breaking fleet (returns the reason)
    rather than write a store that would later fail to load. An existing store's update is atomic (a
    concurrent reader sees the old fleet or the new one); a fresh store's schema is created before the
    data transaction, but load() fail-closes on the absent meta row until that transaction commits — so
    a reader never mistakes a half-written store for a valid empty fleet. `path` resolves to FLEET_DB at
    call time (see load)."""
    path = path or FLEET_DB
    reason = _validate(fleet)
    if reason is not None:
        return reason
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            with conn:
                conn.execute("DELETE FROM agents")
                conn.executemany(
                    "INSERT INTO agents (id, channel, workdir, args, enabled) VALUES (?, ?, ?, ?, ?)",
                    [
                        (a.id, a.channel, a.workdir, a.args, 1 if a.enabled else 0)
                        for a in fleet.agents.values()
                    ],
                )
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('control_channel', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (fleet.control_channel or "",),
                )
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as e:
        return f"could not write {path}: {e}"
    return None


def _legacy_json_for(db_path: str) -> "tuple[Fleet | None, str | None] | None":
    """Read the pre-SQLite `fleet.json` sitting next to the db, if one exists. Returns None when
    there's nothing to migrate. The legacy path is derived from db_path's OWN directory (not a global)
    so a test pointed at a temp db never reaches around to the real ~/.clidecar/fleet.json."""
    legacy = os.path.join(os.path.dirname(db_path) or ".", "fleet.json")
    if not os.path.exists(legacy):
        return None
    try:
        with open(legacy, encoding="utf-8") as fh:
            parsed = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, f"legacy fleet.json unreadable: {e}"
    if not isinstance(parsed, dict):
        return None, "legacy fleet.json is not a JSON object"
    d = cast("dict[str, object]", parsed)
    control = d.get("control_channel")
    control_channel = control if isinstance(control, str) and control else None
    raw_agents = d.get("agents")
    if not isinstance(raw_agents, dict):
        return None, "legacy fleet.json has no 'agents' object"
    agents: dict[str, Agent] = {}
    for agent_id, raw in cast("dict[str, object]", raw_agents).items():
        if not isinstance(raw, dict):
            return None, f"legacy agent {agent_id!r} is not an object"
        a = cast("dict[str, object]", raw)
        channel, workdir, args = a.get("channel"), a.get("workdir"), a.get("args", "")
        enabled = a.get("enabled", True)
        if (
            not isinstance(channel, str)
            or not isinstance(workdir, str)
            or not isinstance(args, str)
        ):
            return None, f"legacy agent {agent_id!r} has non-string fields"
        agents[str(agent_id)] = Agent(
            id=str(agent_id), channel=channel, workdir=workdir, args=args, enabled=bool(enabled)
        )
    return Fleet(control_channel=control_channel, agents=agents), None


def _ensure_migrated(db_path: str) -> None:
    """One-time json→sqlite migration: if the db is absent but a legacy fleet.json sits beside it,
    seed the db from it and rename the json to `.migrated` so it can't re-trigger. Idempotent and
    race-safe (save's INSERTs are deterministic; the rename tolerates a peer winning). A broken legacy
    file is left untouched — load() then reports the db missing (fail-closed), never seeds garbage."""
    if os.path.exists(db_path):
        return
    legacy = _legacy_json_for(db_path)
    if legacy is None:
        return
    fleet, _reason = legacy
    if fleet is None:
        return
    if save(fleet, db_path) is None:
        try:
            os.replace(
                os.path.join(os.path.dirname(db_path) or ".", "fleet.json"),
                db_path + ".migrated.json",
            )
        except OSError:
            pass


def with_agent(fleet: Fleet, agent: Agent) -> Fleet:
    agents = dict(fleet.agents)
    agents[agent.id] = agent
    return Fleet(control_channel=fleet.control_channel, agents=agents)


def without_agent(fleet: Fleet, agent_id: str) -> Fleet:
    agents = {k: v for k, v in fleet.agents.items() if k != agent_id}
    return Fleet(control_channel=fleet.control_channel, agents=agents)
