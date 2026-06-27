#!/usr/bin/env python3
"""CLI over bridge/fleet.py — the fleet manifest's command surface for the bash supervisor and the
`clidecar agent …` verbs. The gateway/control-parser import fleet.py directly; this wrapper exists so
shell code (which can't parse JSON safely) can read + mutate the same source of truth.

Read verbs print to stdout for the supervisor; mutate verbs persist atomically via fleet.save and
exit nonzero with a reason on any invariant break (never write a manifest that won't load). Stays on
stdlib so it runs under the system python3 the supervisor uses (not the venv).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import channel  # noqa: E402
import fleet as f  # noqa: E402


def _die(msg: str) -> "None":
    sys.stderr.write(f"_fleetctl: {msg}\n")
    raise SystemExit(1)


def _load_or_die() -> f.Fleet:
    fleet, reason = f.load()
    if fleet is None:
        _die(reason or "fleet unreadable")
        raise SystemExit(1)  # unreachable; satisfies the type checker
    return fleet


def _kv(args: "list[str]") -> "dict[str, str]":
    out: dict[str, str] = {}
    for a in args:
        if "=" not in a:
            _die(f"expected key=value, got {a!r}")
        k, v = a.split("=", 1)
        out[k] = v
    return out


def _save_or_die(fleet: f.Fleet) -> None:
    reason = f.save(fleet)
    if reason is not None:
        _die(reason)


def cmd_list(args: "list[str]") -> None:
    fleet = _load_or_die()
    agents = fleet.enabled() if "--enabled" in args else list(fleet.agents.values())
    for a in agents:
        print(a.id)


def cmd_get(args: "list[str]") -> None:
    if len(args) != 2:
        _die("usage: get <id> <channel|workdir|args|enabled>")
    agent_id, field = args
    fleet = _load_or_die()
    agent = fleet.agents.get(agent_id)
    if agent is None:
        _die(f"no such agent {agent_id!r}")
        return
    values = {
        "channel": agent.channel,
        "workdir": agent.workdir,
        "args": agent.args,
        "enabled": "1" if agent.enabled else "0",
    }
    if field not in values:
        _die(f"unknown field {field!r}")
    print(values[field])


def cmd_control_channel(_args: "list[str]") -> None:
    fleet = _load_or_die()
    print(fleet.control_channel or "")


def cmd_routes(_args: "list[str]") -> None:
    fleet = _load_or_die()
    for chat_id, agent_id in fleet.routes().items():
        print(f"{chat_id}\t{agent_id}")


def cmd_validate(_args: "list[str]") -> None:
    # A missing OR broken manifest both fail: at reconcile time "unreadable" must never read as
    # "valid empty fleet". Seeding a fresh install is cmd_seed's job, not validate's.
    _, reason = f.load()
    if reason is not None:
        _die(reason)


def cmd_seed(_args: "list[str]") -> None:
    """Create the fleet store from the legacy single-agent config if it's missing — a `main` agent
    bound to DISCORD_CHANNEL_ID with the existing WORKDIR/CLAUDE_ARGS, control_channel from
    CONTROL_CHANNEL. No-op if a store already exists; fails loud if there's nothing to seed from (no
    channel), so a fresh install is told to configure rather than booting an empty fleet."""
    fleet, reason = f.load()
    if fleet is not None:
        return  # already present
    if reason and "does not exist" not in reason:
        _die(reason)  # present-but-broken: don't clobber it
    chan = channel.read_config("DISCORD_CHANNEL_ID") or ""
    workdir = channel.read_config("WORKDIR") or os.path.expanduser("~/clidecar")
    args = channel.read_config("CLAUDE_ARGS") or f.DEFAULT_AGENT_ARGS
    control = channel.read_config("CONTROL_CHANNEL") or ""
    if not chan:
        _die(
            "cannot seed the fleet store: no DISCORD_CHANNEL_ID in config.env (set it, then restart)"
        )
    seeded = f.Fleet(
        control_channel=control or None,
        agents={"main": f.Agent(id="main", channel=chan, workdir=workdir, args=args, enabled=True)},
    )
    _save_or_die(seeded)
    sys.stderr.write("_fleetctl: seeded the fleet store with agent 'main' from config.env\n")


def cmd_add(args: "list[str]") -> None:
    if not args:
        _die("usage: add <id> channel=<cid> workdir=<path> [args=…] [enabled=0|1]")
    agent_id, kv = args[0], _kv(args[1:])
    if "channel" not in kv or "workdir" not in kv:
        _die("add requires channel= and workdir=")
    fleet = _load_or_die()
    agent = f.Agent(
        id=agent_id,
        channel=kv["channel"],
        workdir=kv["workdir"],
        args=kv.get("args", f.DEFAULT_AGENT_ARGS),
        enabled=kv.get("enabled", "1") not in ("0", "false", "no"),
    )
    _save_or_die(f.with_agent(fleet, agent))


def cmd_set(args: "list[str]") -> None:
    if len(args) < 2:
        _die("usage: set <id> field=value …")
    agent_id, kv = args[0], _kv(args[1:])
    fleet = _load_or_die()
    cur = fleet.agents.get(agent_id)
    if cur is None:
        _die(f"no such agent {agent_id!r}")
        return
    updated = f.Agent(
        id=agent_id,
        channel=kv.get("channel", cur.channel),
        workdir=kv.get("workdir", cur.workdir),
        args=kv.get("args", cur.args),
        enabled=(kv["enabled"] not in ("0", "false", "no")) if "enabled" in kv else cur.enabled,
    )
    _save_or_die(f.with_agent(fleet, updated))


def cmd_remove(args: "list[str]") -> None:
    if len(args) != 1:
        _die("usage: remove <id>")
    fleet = _load_or_die()
    if args[0] not in fleet.agents:
        _die(f"no such agent {args[0]!r}")
    _save_or_die(f.without_agent(fleet, args[0]))


def _set_enabled(agent_id: str, enabled: bool) -> None:
    fleet = _load_or_die()
    cur = fleet.agents.get(agent_id)
    if cur is None:
        _die(f"no such agent {agent_id!r}")
        return
    _save_or_die(
        f.with_agent(
            fleet,
            f.Agent(
                id=agent_id,
                channel=cur.channel,
                workdir=cur.workdir,
                args=cur.args,
                enabled=enabled,
            ),
        )
    )


def cmd_enable(args: "list[str]") -> None:
    if len(args) != 1:
        _die("usage: enable <id>")
    _set_enabled(args[0], True)


def cmd_disable(args: "list[str]") -> None:
    if len(args) != 1:
        _die("usage: disable <id>")
    _set_enabled(args[0], False)


_COMMANDS = {
    "list": cmd_list,
    "get": cmd_get,
    "control-channel": cmd_control_channel,
    "routes": cmd_routes,
    "validate": cmd_validate,
    "seed": cmd_seed,
    "add": cmd_add,
    "set": cmd_set,
    "remove": cmd_remove,
    "enable": cmd_enable,
    "disable": cmd_disable,
}


def main(argv: "list[str]") -> int:
    if not argv or argv[0] not in _COMMANDS:
        sys.stderr.write(f"usage: _fleetctl.py <{'|'.join(_COMMANDS)}> [args]\n")
        return 1
    _COMMANDS[argv[0]](argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
