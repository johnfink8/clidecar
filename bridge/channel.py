"""Resolve the active messaging channel for the bridge — its client entrypoint and declared
capabilities — so the Claude-aware core stays provider-agnostic. A channel is a plugin dir
plugins/<name>/ with a plugin.json manifest {kind:"messaging", client, capabilities}. The
active channel is config.env CHANNEL, else the sole installed messaging plugin.

The adapter is an in-process client the daemon imports via the manifest `client` entrypoint
("module:Class" in the plugin dir) and drives over the ChannelClient contract below — it owns the
provider connection (e.g. the Discord WS) for both inbound and outbound; the core never names a
provider in its own source.
"""

import json
import os
import sys
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Protocol, cast

CONFIG = os.path.expanduser("~/.clidecar/config.env")


class ChannelClient(Protocol):
    """What a messaging adapter's `client` entrypoint class must expose. The daemon constructs it
    with the inbound sink, `start()`s it, schedules `dispatch()` coroutines onto `loop` for outbound,
    watches `fatal` for an unrecoverable connection, and `shutdown()`s it on exit. `dispatch` mirrors
    the old verb CLI: (verb, *args) -> (returncode, stdout); `send`/`latest` put the message id on
    stdout, `fetch` the rendered lines."""

    loop: "object"  # the asyncio loop the client runs on (run_coroutine_threadsafe target)
    fatal: "object"  # a threading.Event set when the connection is unrecoverable
    fatal_reason: str

    def start(self) -> None: ...
    def shutdown(self, timeout: float = ...) -> None: ...
    def dispatch(self, verb: str, *args: str) -> "Coroutine[object, object, tuple[int, str]]": ...
    def set_channels(self, channel_ids: "set[str]") -> None:
        """Install the set of channels to accept inbound from (the fleet's enabled-agent channels +
        the control channel). Called before start() and again live on every fleet change."""
        ...


@dataclass(frozen=True)
class Manifest:
    kind: str | None = None
    client: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict[str, bool])

    @classmethod
    def from_obj(cls, obj: object) -> "Manifest":
        if not isinstance(obj, dict):
            return cls()
        d = cast("dict[str, object]", obj)  # JSON object: str keys, arbitrary values
        kind = d.get("kind")
        client = d.get("client")
        caps_value = d.get("capabilities")
        caps = cast("dict[str, object]", caps_value) if isinstance(caps_value, dict) else {}
        return cls(
            kind=kind if isinstance(kind, str) else None,
            client=client if isinstance(client, str) else None,
            capabilities={k: bool(v) for k, v in caps.items()},
        )


def _repo_root() -> str:
    """Walk up from this file to the clidecar checkout (the dir containing plugins/). Works
    whether this module lives under plugins/<x>/ or bridge/."""
    d = os.path.dirname(os.path.abspath(__file__))
    while d != "/":
        if os.path.isdir(os.path.join(d, "plugins")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError("clidecar repo root (with plugins/) not found")


def _read_config(key: str) -> str | None:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def read_config(key: str) -> str | None:
    """A single config.env value (CHANNEL, CONTROL_OWNER, …), or None. Public so gateway-side code
    (the control parser) reads the same config the channel resolver does."""
    return _read_config(key)


def _plugins_dir() -> str:
    return os.path.join(_repo_root(), "plugins")


def _manifest(name: str) -> Manifest:
    path = os.path.join(_plugins_dir(), name, "plugin.json")
    try:
        with open(path, encoding="utf-8") as fh:
            parsed = json.load(fh)
    except FileNotFoundError:
        return Manifest()
    except (OSError, json.JSONDecodeError) as e:
        # A present-but-broken manifest is a config error, not a missing plugin — say so
        # rather than silently demoting the plugin to "not a channel".
        sys.stderr.write(f"clidecar channel: unreadable manifest {path}: {e}\n")
        return Manifest()
    return Manifest.from_obj(parsed)


def _messaging_plugins() -> list[str]:
    try:
        names = sorted(os.listdir(_plugins_dir()))
    except OSError:
        return []
    # Only directories are plugin candidates — a sibling file (README.md) is not a
    # broken plugin, so don't route it through _manifest's loud-error branch.
    return [
        n
        for n in names
        if os.path.isdir(os.path.join(_plugins_dir(), n)) and _manifest(n).kind == "messaging"
    ]


def active() -> tuple[str | None, str | None]:
    """(name, reason): the active messaging channel, or (None, reason) explaining why it
    couldn't be resolved. Active = config.env CHANNEL (if set and installed), else the sole
    messaging plugin. Zero plugins, an ambiguous set with no CHANNEL, or a CHANNEL naming a
    plugin that isn't an installed messaging adapter all resolve to None WITH a reason the
    caller logs loudly — never a silent no-channel."""
    configured = _read_config("CHANNEL")
    msgs = _messaging_plugins()
    if configured:
        if configured in msgs:
            return configured, None
        return (
            None,
            f"CHANNEL={configured!r} is not an installed messaging plugin (installed: {msgs or 'none'})",
        )
    if not msgs:
        return None, "no messaging plugin installed under plugins/"
    if len(msgs) > 1:
        return (
            None,
            f"ambiguous: messaging plugins {msgs} installed; set CHANNEL in config.env to pick one",
        )
    return msgs[0], None


def client_entrypoint() -> tuple[str | None, str | None, str | None]:
    """(plugin_dir, "module:Class", reason): the active channel's in-process client entrypoint, or
    (None, None, reason) if unresolved. The daemon adds plugin_dir to sys.path, imports the module,
    and constructs the class as its inbound+outbound transport."""
    name, reason = active()
    if not name:
        return None, None, reason
    ep = _manifest(name).client
    if not ep or ":" not in ep:
        return (
            None,
            None,
            f"channel {name!r} manifest declares no valid 'client' (want 'module:Class')",
        )
    return os.path.join(_plugins_dir(), name), ep, None


def capabilities() -> dict[str, bool]:
    """The active channel's declared capabilities (e.g. edit/react/latest/listen/fetch); {} if none."""
    name, _ = active()
    return _manifest(name).capabilities if name else {}
