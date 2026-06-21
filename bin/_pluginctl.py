#!/usr/bin/env python3
"""Enable/disable/list clidecar plugins by editing the project's .claude/settings.json.

A plugin is a directory under <home>/plugins/<name>/ with a plugin.json holding a
Claude Code hooks fragment that uses the ${PLUGIN_DIR} placeholder for paths. Enabling
resolves the placeholder to the plugin's absolute dir and merges its hook entries into
settings.json; disabling strips them. A plugin's own entries are recognised by its dir
appearing in their command path, so enable is idempotent and disable is exact.

Hooks load at session launch, so a change applies on the next `clidecar recycle`.

    _pluginctl.py <home> list
    _pluginctl.py <home> enable  <name>
    _pluginctl.py <home> disable <name>
"""
import json
import os
import sys

PLACEHOLDER = "${PLUGIN_DIR}"


def settings_path(home):
    return os.path.join(home, ".claude", "settings.json")


def plugin_dir(home, name):
    return os.path.join(home, "plugins", name)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_settings(home, settings):
    path = settings_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")


def is_plugin_entry(group, pdir):
    return any(pdir in (h.get("command") or "") for h in group.get("hooks", []))


def load_plugin(home, name):
    path = os.path.join(plugin_dir(home, name), "plugin.json")
    plug = load_json(path, None)
    if plug is None:
        sys.exit(f"plugin not found: {path}")
    return plug


def enable(home, name):
    pdir = os.path.abspath(plugin_dir(home, name))
    plug = load_plugin(home, name)
    settings = load_json(settings_path(home), {})
    hooks = settings.setdefault("hooks", {})
    for event, groups in plug.get("hooks", {}).items():
        resolved = json.loads(json.dumps(groups).replace(PLACEHOLDER, pdir))
        kept = [g for g in hooks.get(event, []) if not is_plugin_entry(g, pdir)]
        hooks[event] = kept + resolved
    write_settings(home, settings)
    print(f"enabled '{name}' — recycle to apply ({settings_path(home)})")


def disable(home, name):
    pdir = os.path.abspath(plugin_dir(home, name))
    settings = load_json(settings_path(home), {})
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not is_plugin_entry(g, pdir)]
        if not hooks[event]:
            del hooks[event]
    write_settings(home, settings)
    print(f"disabled '{name}' — recycle to apply")


def list_plugins(home):
    root = os.path.join(home, "plugins")
    settings = load_json(settings_path(home), {})
    active = json.dumps(settings.get("hooks", {}))
    names = sorted(d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "plugin.json"))) \
        if os.path.isdir(root) else []
    if not names:
        print("no plugins found in plugins/")
        return
    for name in names:
        on = os.path.abspath(plugin_dir(home, name)) in active
        plug = load_json(os.path.join(plugin_dir(home, name), "plugin.json"), {})
        print(f"[{'on ' if on else 'off'}] {name} — {plug.get('description', '')}")


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: _pluginctl.py <home> list|enable|disable [name]")
    home, action = sys.argv[1], sys.argv[2]
    if action == "list":
        list_plugins(home)
    elif action in ("enable", "disable"):
        if len(sys.argv) < 4:
            sys.exit(f"usage: _pluginctl.py <home> {action} <name>")
        (enable if action == "enable" else disable)(home, sys.argv[3])
    else:
        sys.exit(f"unknown action: {action}")


if __name__ == "__main__":
    main()
