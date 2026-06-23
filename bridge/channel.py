"""Resolve the active messaging channel for the bridge — its transport script and declared
capabilities — so the Claude-aware hooks stay provider-agnostic. A channel is a plugin dir
plugins/<name>/ with a plugin.json manifest {kind:"messaging", transport, capabilities}. The
active channel is config.env CHANNEL, else the sole installed messaging plugin.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from typing import cast

CONFIG = os.path.expanduser("~/.clidecar/config.env")


@dataclass(frozen=True)
class Manifest:
    kind: str | None = None
    transport: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict[str, bool])

    @classmethod
    def from_obj(cls, obj: object) -> "Manifest":
        if not isinstance(obj, dict):
            return cls()
        d = cast("dict[str, object]", obj)  # JSON object: str keys, arbitrary values
        kind = d.get("kind")
        transport = d.get("transport")
        caps_value = d.get("capabilities")
        caps = cast("dict[str, object]", caps_value) if isinstance(caps_value, dict) else {}
        return cls(
            kind=kind if isinstance(kind, str) else None,
            transport=transport if isinstance(transport, str) else None,
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


def transport() -> tuple[str | None, str | None]:
    """(script, reason): absolute path to the active channel's transport script, or
    (None, reason) if unresolved. The bridge's send/edit/react/latest all shell out to this
    one script."""
    name, reason = active()
    if not name:
        return None, reason
    tpl = _manifest(name).transport
    if not tpl:
        return None, f"channel {name!r} manifest declares no 'transport'"
    return tpl.replace("${PLUGIN_DIR}", os.path.join(_plugins_dir(), name)), None


def capabilities() -> dict[str, bool]:
    """The active channel's declared capabilities (e.g. edit/react/latest/listen/fetch); {} if none."""
    name, _ = active()
    return _manifest(name).capabilities if name else {}
