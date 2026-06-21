"""Resolve the active messaging channel for the bridge — its transport script and declared
capabilities — so the Claude-aware hooks stay provider-agnostic. A channel is a plugin dir
plugins/<name>/ with a plugin.json manifest {kind:"messaging", transport, capabilities}. The
active channel is config.env CHANNEL, else the sole installed messaging plugin.
"""
import json
import os

CONFIG = os.path.expanduser("~/.clidecar/config.env")


def _repo_root():
    """Walk up from this file to the clidecar checkout (the dir containing plugins/). Works
    whether this module lives under plugins/<x>/ or bridge/."""
    d = os.path.dirname(os.path.abspath(__file__))
    while d != "/":
        if os.path.isdir(os.path.join(d, "plugins")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError("clidecar repo root (with plugins/) not found")


def _read_config(key):
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _plugins_dir():
    return os.path.join(_repo_root(), "plugins")


def _manifest(name):
    try:
        with open(os.path.join(_plugins_dir(), name, "plugin.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _messaging_plugins():
    try:
        names = sorted(os.listdir(_plugins_dir()))
    except OSError:
        return []
    return [n for n in names if _manifest(n).get("kind") == "messaging"]


def active():
    """Active messaging channel name: config.env CHANNEL, else the sole messaging plugin."""
    name = _read_config("CHANNEL")
    if name:
        return name
    msgs = _messaging_plugins()
    return msgs[0] if len(msgs) == 1 else None


def transport():
    """Absolute path to the active channel's transport script, or None if unresolved — the
    bridge's send/edit/react/latest all shell out to this one script."""
    name = active()
    if not name:
        return None
    t = _manifest(name).get("transport")
    return t.replace("${PLUGIN_DIR}", os.path.join(_plugins_dir(), name)) if t else None


def capabilities():
    """The active channel's declared capabilities (edit/react/latest); {} if none."""
    name = active()
    return _manifest(name).get("capabilities", {}) if name else {}
