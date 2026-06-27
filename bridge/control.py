"""The fleet control language — deterministic, gateway-parsed commands on the control channel.

A message on the control channel never reaches an agent: the Broker hands it here. This module gates
it to the control OWNER (config.env CONTROL_OWNER — separate from the gate.py sender allowlist, and
NOT mutable from the channel), then hands the message to an isolated Haiku translator
(bridge/translate.py) that turns natural language into at most one canonical command plus a reply.
The command — if any — flows through the SAME deterministic word grammar that has always run here:
it mutates the fleet manifest (bridge/fleet.py) and/or drops a per-agent supervisor flag, then
replies on the control channel. The supervisor reconciles processes to the manifest; the gateway
refresh() re-applies routes + the adapter's listen-set immediately. The LLM only PROPOSES — every
command still passes the deterministic gate, and if the translator is unreachable the raw text is
parsed deterministically (today's bare-word grammar), so exact commands never depend on Haiku.

Every refusal and every validation failure replies loudly; nothing fails silently.
"""

import json
import os
from collections.abc import Callable
from typing import cast

import channel
import exchange as ex
import fleet as f
import translate

CONTROL_DIR = os.path.expanduser("~/.clidecar/control")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
# Where a NEW agent's workspace is created when the owner names no existing folder. spawn first probes
# $HOME/<id> (so `spawn quorelo` imports ~/quorelo) before creating under this base.
DEFAULT_WORKDIR_BASE = "~/clidecar-agents"

# The canonical command vocabulary. GRAMMAR feeds the Haiku translator (bridge/translate.py) the exact
# shapes it may emit; HELP renders the same set for humans; the _HANDLERS table (below) executes them
# and is the source of truth. _check_vocab_lockstep() asserts the three agree at import, so a
# half-updated command fails loud rather than silently drifting.
GRAMMAR = """\
  agents
  status
  spawn <id> [channel=<cid>] [workdir=<path>] [args=<...>]
  stop <id>
  start <id>
  remove <id>
  recycle <id>
  route <id> channel=<cid>
  set <id> workdir=<path>
  set <id> args=<...>
  help"""

HELP = (
    "**fleet control** — owner-only · just type the word (case-insensitive, no `!`):\n"
    "`agents` list the fleet  ·  `status` gateway + fleet health\n"
    "`spawn <id>` add + launch an agent — auto-creates its channel + workspace; "
    "`channel=`/`workdir=` are optional overrides\n"
    "`stop <id>` / `start <id>` disable/enable  ·  `remove <id>` forget\n"
    "`recycle <id>` reset that agent's context  ·  `route <id> channel=<cid>` rebind\n"
    "`set <id> workdir=<path>|args=…` mutate (next relaunch)  ·  `help` this list"
)


def _reply(broker: ex.Broker, chat_id: str, text: str) -> None:
    broker.emit(ex.Outbound(text=text, kind="notice", source="gateway", chat_id=chat_id))


def _kv(tokens: "list[str]") -> "dict[str, str]":
    out: dict[str, str] = {}
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _agent_pidfile(agent_id: str) -> str:
    return os.path.join(CONTROL_DIR, "agents", agent_id, "claude.pid")


def _alive(agent_id: str) -> bool:
    try:
        with open(_agent_pidfile(agent_id), encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _load(broker: ex.Broker, chat_id: str) -> "f.Fleet | None":
    fleet, reason = f.load()
    if fleet is None:
        _reply(broker, chat_id, f"⚠️ fleet manifest unreadable: {reason}")
    return fleet


def _persist(
    broker: ex.Broker, chat_id: str, fleet: f.Fleet, refresh: "Callable[[], None]"
) -> bool:
    reason = f.save(fleet)
    if reason is not None:
        _reply(broker, chat_id, f"⛔ refused: {reason}")
        return False
    refresh()
    return True


def _cmd_agents(broker: ex.Broker, chat_id: str, fleet: f.Fleet) -> None:
    if not fleet.agents:
        _reply(broker, chat_id, "no agents in the fleet")
        return
    lines = ["**fleet:**"]
    for a in fleet.agents.values():
        state = "🟢 up" if _alive(a.id) else ("⚪ enabled" if a.enabled else "⚫ stopped")
        lines.append(f"`{a.id}` {state} · channel `{a.channel}` · `{a.workdir}`")
    _reply(broker, chat_id, "\n".join(lines))


def _cmd_status(broker: ex.Broker, chat_id: str, fleet: f.Fleet) -> None:
    up = sum(1 for a in fleet.enabled() if _alive(a.id))
    _reply(
        broker,
        chat_id,
        f"gateway up · {up}/{len(fleet.enabled())} enabled agents attached · "
        f"{len(fleet.agents)} total · control `{fleet.control_channel}`",
    )


def _workdir_base() -> str:
    return os.path.expanduser(channel.read_config("AGENT_WORKDIR_BASE") or DEFAULT_WORKDIR_BASE)


def _resolve_workdir(agent_id: str, explicit: "str | None") -> str:
    """Resolve the absolute workspace path for a spawn — NO side effects; the caller creates or imports
    it. An explicit path wins verbatim; otherwise the first existing of $HOME/<id> or <base>/<id>."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    base = _workdir_base()
    for cand in (os.path.join(os.path.expanduser("~"), agent_id), os.path.join(base, agent_id)):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.path.abspath(os.path.join(base, agent_id))


def _is_trusted(workdir: str) -> bool:
    """True iff ~/.claude.json already records this path as trusted. A missing/unreadable file or an
    absent entry reads as NOT trusted — fail safe (never assume trust we can't prove)."""
    try:
        with open(CLAUDE_JSON, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    projects = cast("dict[str, object]", raw).get("projects")
    if not isinstance(projects, dict):
        return False
    entry = cast("dict[str, object]", projects).get(os.path.abspath(workdir))
    return isinstance(entry, dict) and bool(
        cast("dict[str, object]", entry).get("hasTrustDialogAccepted")
    )


def _seed_trust(workdir: str) -> "str | None":
    """Mark a freshly-CREATED (empty) workdir trusted in ~/.claude.json so the agent's first launch
    doesn't wedge on the folder-trust modal in a detached screen with nobody to confirm it. ONLY ever
    called for a directory we just created empty — NEVER for imported content (see _cmd_spawn).
    Read-modify-write with an optimistic mtime guard + atomic replace, since live Claude processes
    also write this shared file. Returns an error reason or None. CAVEAT: the guard shrinks but does
    not fully eliminate the concurrent-writer race on this file."""
    abspath = os.path.abspath(workdir)
    for _ in range(5):
        try:
            before = os.path.getmtime(CLAUDE_JSON)
            with open(CLAUDE_JSON, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return f"{CLAUDE_JSON} does not exist"
        except (OSError, json.JSONDecodeError) as e:
            return f"{CLAUDE_JSON} unreadable: {e}"
        if not isinstance(raw, dict):
            return f"{CLAUDE_JSON} is not a JSON object"
        data = cast("dict[str, object]", raw)
        projects_raw = data.get("projects")
        projects = cast("dict[str, object]", projects_raw) if isinstance(projects_raw, dict) else {}
        data["projects"] = projects
        entry_raw = projects.get(abspath)
        entry = cast("dict[str, object]", entry_raw) if isinstance(entry_raw, dict) else {}
        projects[abspath] = entry
        entry["hasTrustDialogAccepted"] = True
        try:  # optimistic concurrency: bail to a re-read if the file changed under us
            if os.path.getmtime(CLAUDE_JSON) != before:
                continue
        except OSError:
            continue
        tmp = f"{CLAUDE_JSON}.clidecar.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, CLAUDE_JSON)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return f"could not write {CLAUDE_JSON}: {e}"
        return None
    return f"{CLAUDE_JSON} kept changing under us — gave up after 5 tries"


def _cmd_spawn(
    broker: ex.Broker,
    chat_id: str,
    fleet: f.Fleet,
    args: "list[str]",
    refresh: "Callable[[], None]",
) -> None:
    """Add + launch an agent. The owner types only `spawn <id>`; the gateway derives the rest:
    workspace (import $HOME/<id> or an existing <base>/<id>, else create <base>/<id> and pre-trust it)
    and Discord channel (auto-created as a sibling of the control channel). `channel=`/`workdir=`
    override either. Side effects are ordered cheapest-first and every failure aborts loudly, so a bad
    spawn never half-creates a live agent."""
    if not args:
        _reply(broker, chat_id, "usage: `spawn <id> [channel=<cid>] [workdir=<path>] [args=…]`")
        return
    agent_id, kv = args[0], _kv(args[1:])
    if not f.valid_id(agent_id):
        _reply(broker, chat_id, f"⛔ `{agent_id}` is not a valid id ([a-z0-9][a-z0-9_-]*)")
        return
    if agent_id in fleet.agents:
        _reply(broker, chat_id, f"⛔ agent `{agent_id}` already exists (use `set`/`route`)")
        return

    # Import an existing dir only if Claude already trusts it — never auto-trust existing content.
    workdir = _resolve_workdir(agent_id, kv.get("workdir"))
    if os.path.isdir(workdir):
        if not _is_trusted(workdir):
            _reply(
                broker,
                chat_id,
                f"⛔ `{workdir}` has existing content and isn't trusted in Claude — open it once "
                f"yourself to trust it, then spawn (I only auto-trust folders I create empty)",
            )
            return
    else:
        try:
            os.makedirs(workdir, exist_ok=False)
        except FileExistsError:
            # Raced: content appeared between the isdir() check above and here. Refuse — the _seed_trust
            # below is only safe for a dir THIS spawn created empty; never auto-trust a dir we didn't.
            _reply(broker, chat_id, f"⛔ `{workdir}` appeared mid-spawn — not creating; retry")
            return
        except OSError as e:
            _reply(broker, chat_id, f"⛔ could not create workdir `{workdir}`: {e}")
            return
        reason = _seed_trust(workdir)
        if reason is not None:
            _reply(broker, chat_id, f"⛔ created `{workdir}` but could not pre-trust it: {reason}")
            return

    channel_id = kv.get("channel")
    if not channel_id:
        if not channel.capabilities().get("create"):
            _reply(
                broker,
                chat_id,
                "⛔ this channel adapter can't auto-create channels — pass `channel=<cid>`",
            )
            return
        if not fleet.control_channel:
            _reply(
                broker,
                chat_id,
                "⛔ no control_channel set — can't place a new channel; pass `channel=<cid>`",
            )
            return
        channel_id = broker.create_channel(fleet.control_channel, agent_id)
        if not channel_id:
            _reply(
                broker,
                chat_id,
                "⛔ failed to create a Discord channel (Manage Channels permission?) — pass `channel=<cid>`",
            )
            return

    agent = f.Agent(
        id=agent_id,
        channel=channel_id,
        workdir=workdir,
        args=kv.get("args", f.DEFAULT_AGENT_ARGS),
        enabled=True,
    )
    if _persist(broker, chat_id, f.with_agent(fleet, agent), refresh):
        _reply(
            broker,
            chat_id,
            f"🚀 `{agent_id}` on channel `{agent.channel}` · `{workdir}` — supervisor launching",
        )


def _cmd_toggle(
    broker: ex.Broker,
    chat_id: str,
    fleet: f.Fleet,
    args: "list[str]",
    enabled: bool,
    refresh: "Callable[[], None]",
) -> None:
    verb = "start" if enabled else "stop"
    if len(args) != 1:
        _reply(broker, chat_id, f"usage: `{verb} <id>`")
        return
    cur = fleet.agents.get(args[0])
    if cur is None:
        _reply(broker, chat_id, f"⛔ no such agent `{args[0]}`")
        return
    updated = f.Agent(
        id=cur.id, channel=cur.channel, workdir=cur.workdir, args=cur.args, enabled=enabled
    )
    if _persist(broker, chat_id, f.with_agent(fleet, updated), refresh):
        _reply(broker, chat_id, f"{'▶️ starting' if enabled else '⏹️ stopping'} `{cur.id}`")


def _cmd_remove(
    broker: ex.Broker,
    chat_id: str,
    fleet: f.Fleet,
    args: "list[str]",
    refresh: "Callable[[], None]",
) -> None:
    if len(args) != 1:
        _reply(broker, chat_id, "usage: `remove <id>`")
        return
    if args[0] not in fleet.agents:
        _reply(broker, chat_id, f"⛔ no such agent `{args[0]}`")
        return
    if _persist(broker, chat_id, f.without_agent(fleet, args[0]), refresh):
        _reply(broker, chat_id, f"🗑️ removed `{args[0]}` — supervisor will stop it")


def _cmd_recycle(broker: ex.Broker, chat_id: str, fleet: f.Fleet, args: "list[str]") -> None:
    if len(args) != 1:
        _reply(broker, chat_id, "usage: `recycle <id>`")
        return
    if args[0] not in fleet.agents:
        _reply(broker, chat_id, f"⛔ no such agent `{args[0]}`")
        return
    flag_dir = os.path.join(CONTROL_DIR, "agents", args[0])
    try:
        os.makedirs(flag_dir, exist_ok=True)
        with open(os.path.join(flag_dir, "RECYCLE"), "w", encoding="utf-8") as fh:
            fh.write("")
    except OSError as e:
        _reply(broker, chat_id, f"⚠️ could not drop recycle flag: {e}")
        return
    _reply(broker, chat_id, f"♻️ recycling `{args[0]}` (fresh context)")


def _cmd_route(
    broker: ex.Broker,
    chat_id: str,
    fleet: f.Fleet,
    args: "list[str]",
    refresh: "Callable[[], None]",
) -> None:
    if len(args) < 2:
        _reply(broker, chat_id, "usage: `route <id> channel=<cid>`")
        return
    cur = fleet.agents.get(args[0])
    if cur is None:
        _reply(broker, chat_id, f"⛔ no such agent `{args[0]}`")
        return
    new_channel = _kv(args[1:]).get("channel")
    if not new_channel:
        _reply(broker, chat_id, "⛔ route needs `channel=<cid>`")
        return
    updated = f.Agent(
        id=cur.id, channel=new_channel, workdir=cur.workdir, args=cur.args, enabled=cur.enabled
    )
    if _persist(broker, chat_id, f.with_agent(fleet, updated), refresh):
        _reply(broker, chat_id, f"🔀 `{cur.id}` now on channel `{new_channel}`")


def _cmd_set(
    broker: ex.Broker,
    chat_id: str,
    fleet: f.Fleet,
    args: "list[str]",
    refresh: "Callable[[], None]",
) -> None:
    if len(args) < 2:
        _reply(broker, chat_id, "usage: `set <id> workdir=<path>|args=…`")
        return
    cur = fleet.agents.get(args[0])
    if cur is None:
        _reply(broker, chat_id, f"⛔ no such agent `{args[0]}`")
        return
    kv = _kv(args[1:])
    updated = f.Agent(
        id=cur.id,
        channel=kv.get("channel", cur.channel),
        workdir=kv.get("workdir", cur.workdir),
        args=kv.get("args", cur.args),
        enabled=cur.enabled,
    )
    if _persist(broker, chat_id, f.with_agent(fleet, updated), refresh):
        _reply(broker, chat_id, f"✏️ updated `{cur.id}` (applies on next relaunch)")


def _snapshot() -> str:
    """A compact current-fleet view fed to the Haiku translator so it can resolve agent references
    ("the main agent") and answer questions. Best-effort context only — the command it proposes is
    re-validated against a fresh load in `_dispatch`."""
    fleet, reason = f.load()
    if fleet is None:
        return f"(fleet manifest unreadable: {reason})"
    if not fleet.agents:
        return "(no agents)"
    return "\n".join(
        f"- {a.id}: channel {a.channel}, workdir {a.workdir}, {'enabled' if a.enabled else 'stopped'}"
        for a in fleet.agents.values()
    )


_Handler = Callable[[ex.Broker, str, "f.Fleet", "list[str]", "Callable[[], None]"], None]

# The executable command table — its keys are the single source of truth for the vocabulary. GRAMMAR
# (shown to Haiku) and HELP (shown to the owner) are checked against it at import. `help` is handled
# inline in _dispatch (it needs no fleet load).
_HANDLERS: "dict[str, _Handler]" = {
    "agents": lambda b, c, fl, a, r: _cmd_agents(b, c, fl),
    "status": lambda b, c, fl, a, r: _cmd_status(b, c, fl),
    "spawn": lambda b, c, fl, a, r: _cmd_spawn(b, c, fl, a, r),
    "stop": lambda b, c, fl, a, r: _cmd_toggle(b, c, fl, a, False, r),
    "start": lambda b, c, fl, a, r: _cmd_toggle(b, c, fl, a, True, r),
    "remove": lambda b, c, fl, a, r: _cmd_remove(b, c, fl, a, r),
    "recycle": lambda b, c, fl, a, r: _cmd_recycle(b, c, fl, a),
    "route": lambda b, c, fl, a, r: _cmd_route(b, c, fl, a, r),
    "set": lambda b, c, fl, a, r: _cmd_set(b, c, fl, a, r),
}

# Convenience synonyms the owner/translator may type; each resolves to a canonical verb above or `help`.
_ALIASES: "dict[str, str]" = {"list": "agents", "ls": "agents", "?": "help", "commands": "help"}


def _check_vocab_lockstep() -> None:
    """Fail loud at import if the command vocabulary drifts across its three surfaces — the _HANDLERS
    table (source of truth), the GRAMMAR shown to Haiku, and the human HELP text. A command added to
    one but not the others is otherwise a silent bug (Haiku proposes a verb that won't run, or HELP
    advertises one that returns 'unknown command')."""
    expected = set(_HANDLERS) | {"help"}
    grammar = {ln.split()[0] for ln in GRAMMAR.strip().splitlines() if ln.split()}
    if grammar != expected:
        raise RuntimeError(
            f"control vocabulary drift: GRAMMAR {sorted(grammar)} != commands {sorted(expected)}"
        )
    # Each verb appears in HELP wrapped in backticks, either bare (`agents`) or with args (`spawn <id>`);
    # require a backtick or space right after the verb so a prefix can't false-pass (`set` vs `settings`).
    missing = sorted(v for v in expected if f"`{v}`" not in HELP and f"`{v} " not in HELP)
    if missing:
        raise RuntimeError(f"control vocabulary drift: HELP omits {missing}")


_check_vocab_lockstep()


def _dispatch(broker: ex.Broker, chat_id: str, text: str, refresh: "Callable[[], None]") -> None:
    """Run one canonical command through the deterministic word grammar. The sole executor — every
    command (whether proposed by Haiku or parsed from raw fallback text) passes through here."""
    text = text.strip()
    if text.startswith("!"):
        text = text[1:].strip()  # a leading ! is tolerated but never required
    parts = text.split()
    if not parts:
        return  # empty command — ignore, don't ❌
    verb, args = parts[0].lower(), parts[1:]
    verb = _ALIASES.get(verb, verb)
    if verb == "help":
        _reply(broker, chat_id, HELP)
        return
    handler = _HANDLERS.get(verb)
    if handler is None:
        _reply(broker, chat_id, f"⛔ unknown command `{verb}` — type `help` for the list")
        return
    fleet = _load(broker, chat_id)
    if fleet is None:
        return
    handler(broker, chat_id, fleet, args, refresh)


def handle(msg: ex.Inbound, broker: ex.Broker, refresh: "Callable[[], None]") -> None:
    """Owner-gated; translates via Haiku then executes deterministically. Replies on the channel."""
    chat_id = msg.chat_id
    owner = channel.read_config("CONTROL_OWNER")
    if not owner:
        _reply(broker, chat_id, "⛔ no CONTROL_OWNER set in config.env — fleet control is disabled")
        return
    if msg.user_id != owner:
        _reply(broker, chat_id, f"⛔ {msg.user}: fleet control is owner-only.")
        return
    text = msg.content.strip()
    if not text:
        return  # empty message — ignore, don't ❌
    tr = translate.translate(text, _snapshot(), GRAMMAR)
    if tr is None:
        # translator unreachable — fall back to a literal deterministic parse so exact commands work
        _reply(broker, chat_id, "⚠️ translator unavailable — parsing your message literally")
        _dispatch(broker, chat_id, text, refresh)
        return
    if tr.reply:
        _reply(broker, chat_id, tr.reply)
    if tr.command:
        _dispatch(broker, chat_id, tr.command, refresh)
