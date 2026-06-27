"""Haiku translation front-end for the control channel.

A control-channel message is first handed to a STRICT, isolated `claude -p` (Haiku) call that turns
natural language into at most one canonical gateway command plus a human reply. The command — if
any — is then handed to control.py's deterministic parser, which remains the sole executor and the
ground-truth responder: Haiku only PROPOSES, nothing it emits runs without passing the deterministic
gate. If the call fails OR returns output that isn't the promised JSON object, translate() returns
None and the caller falls back to parsing the raw text deterministically, so exact commands keep
working even when Haiku is unreachable or misbehaving.

The call is sandboxed — `--setting-sources ""` and `--strict-mcp-config` keep the bridge's own hooks,
MCP, and CLAUDE.md out of the translator, and the built-in tools are denied — and it runs under the
fleet's existing CLAUDE_BIN auth, so there is no API key and no extra dependency.
"""

import json
import subprocess
import sys
from dataclasses import dataclass

import channel
import transcript as t

_TIMEOUT_SECS = 30
_DEFAULT_MODEL = "haiku"
_DENY_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task"]

_SYSTEM = """\
You are the command translator for a fleet-of-Claude-agents controller. You receive ONE message the \
fleet owner typed into the control channel and turn it into at most one canonical control command \
plus a short reply.

Output ONLY a single JSON object — no markdown, no prose around it:
{{"command": "<one canonical command, or empty string if none applies>", "reply": "<short message to the owner>"}}

The canonical commands — this is the ONLY grammar `command` may use:
{grammar}

Rules:
- Emit a `command` ONLY when the owner clearly wants that action; otherwise `command` is "".
- NEVER invent an agent id, channel id, or path. If a required value is missing, set `command` to ""
  and use `reply` to ask for exactly what is missing.
- Values shown in [brackets] are OPTIONAL — OMIT them unless the owner explicitly gave one. For
  `spawn`, only <id> is required: the gateway auto-assigns the channel and workspace, so do NOT ask
  for or invent channel=/workdir= — just emit `spawn <id>`.
- Use ONLY agent ids that appear in the current fleet below, except for `spawn`, which names a new one.
- `reply` is a short (one or two sentence) confirmation of the action, or your answer when no command applies.

Current fleet:
{snapshot}"""


def _log(reason: str) -> None:
    sys.stderr.write(f"clidecar translate: {reason}\n")


@dataclass(frozen=True)
class Translation:
    command: str  # "" when Haiku proposes no action
    reply: str

    @classmethod
    def from_text(cls, text: str) -> "Translation | None":
        """Haiku's output parsed into the {command, reply} contract, or None if it isn't a JSON
        object — None routes the caller to the loud fallback, so malformed output never silently
        drops a command. A valid object with an empty/absent `command` is a legitimate decline."""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]
            if stripped.endswith("```"):
                stripped = stripped[: stripped.rfind("```")]
        try:
            obj = t.as_obj(json.loads(stripped))
        except json.JSONDecodeError:
            return None
        if (
            not obj
        ):  # non-object JSON (list/string/number) or {} -> as_obj gives {}; not the contract
            return None
        command = obj.get("command")
        reply = obj.get("reply")
        return cls(
            command=command if isinstance(command, str) else "",
            reply=reply if isinstance(reply, str) else "",
        )


def translate(text: str, fleet_snapshot: str, grammar: str) -> "Translation | None":
    """Run the isolated Haiku translator on one control-channel message. Returns None on any failure
    (missing CLAUDE_BIN, non-zero exit, timeout, unparseable output) so the caller falls back to a
    literal deterministic parse; logs the cause to stderr so a persistent outage is debuggable."""
    claude_bin = channel.read_config("CLAUDE_BIN") or "claude"
    model = channel.read_config("CONTROL_MODEL") or _DEFAULT_MODEL
    system = _SYSTEM.format(grammar=grammar, snapshot=fleet_snapshot or "(no agents)")
    cmd = [
        claude_bin,
        "-p",
        text,
        "--model",
        model,
        "--system-prompt",
        system,
        "--exclude-dynamic-system-prompt-sections",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--output-format",
        "json",
        "--disallowed-tools",
        *_DENY_TOOLS,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"claude -p did not run: {e!r}")
        return None
    if proc.returncode != 0:
        _log(f"claude -p exit {proc.returncode}: {proc.stderr.strip()[:300]}")
        return None
    try:
        envelope = t.as_obj(json.loads(proc.stdout))
    except json.JSONDecodeError:
        _log(f"unparseable envelope: {proc.stdout.strip()[:300]}")
        return None
    if envelope.get("is_error"):
        _log(f"claude reported error: {str(envelope.get('result'))[:300]}")
        return None
    result = envelope.get("result")
    if not isinstance(result, str):
        return None
    tr = Translation.from_text(result)
    if tr is None:
        _log(f"result was not the JSON contract: {result.strip()[:300]}")
    return tr
