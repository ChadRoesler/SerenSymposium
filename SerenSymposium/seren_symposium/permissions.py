"""
seren_symposium.permissions
════════════════════════════════════════════════════════════════════════

The claude voice's permissions, in layers, each one more particular than the
one before it (design note, 22 Oct 2026: "cli as base > carabiner > symposium
all > project > chat", built for everyone rather than one person's setup):

    the CLI's own settings     the base. Symposium never takes anything away
                               from them; it only adds.
    the carabiner              the box's defaults for a session of this
                               harness: a `session:` block in its claude.yaml
                               (seren-carabiners/what-the-carabiner-owns).
    Symposium, every chat      Settings > Claude: claude.permission_mode and
                               claude.allowed_tools (the yaml, then the window).
    the project                its own permission_mode, allowed_tools, and
                               tools_inherit.
    the chat                   the same three, its own.

THE MODE is the most particular one set: a chat's, else its project's, else
every chat's, else the carabiner's, else none (the CLI decides).

THE ALLOW LIST is the chat's, then each layer above it, until a layer says
not to inherit: a red-team chat can ask about everything whatever its
project, Symposium or the carabiner allow. (The CLI's own allow rules still
apply; they are not ours to take back.)

COVERS is Symposium's own reading of a rule, for one job: when the CLI asks
about a tool a rule chosen here already allows (a server-wide rule the CLI
spells differently, say), the window answers yes without bothering anyone.
It is conservative: a rule it cannot read is never taken to cover anything,
and the ask goes to the person as before.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Optional
from urllib.parse import urlparse

import yaml

from .config import FolderPreset, SymposiumConfig

# What the window offers. bypassPermissions is not among them: a window that
# can switch every check off with one click is not one to leave open. The
# yaml and the carabiner can still say it, on purpose. "default" is the
# older name of "manual" and is still taken.
WINDOW_MODES = ("", "default", "manual", "acceptEdits", "auto", "plan", "dontAsk")
ALL_MODES = WINDOW_MODES + ("bypassPermissions",)
MAX_RULES = 200
LAYERS = ("chat", "project", "symposium", "carabiner")

# The file tools and the kind of rule that lets them in.
_KIND = {"Read": "Read", "Glob": "Read", "Grep": "Read", "LS": "Read",
         "Edit": "Edit", "Write": "Edit", "MultiEdit": "Edit", "NotebookEdit": "Edit"}


def clean_rules(value: Any, label: str = "allowed_tools") -> list[str]:
    """A list of rule strings, stripped and deduplicated; ValueError otherwise."""
    if not isinstance(value, list) or len(value) > MAX_RULES \
            or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{label} is a list of rules (Read, Bash(git:*), mcp__workbench__*)")
    return list(dict.fromkeys(v.strip()[:300] for v in value))


def carabiner_session(cfg: SymposiumConfig) -> dict[str, Any]:
    """The carabiner's `session:` block: {permission_mode, allowed_tools,
    path}, or {} when no carabiner is clipped or it says nothing. Read for
    every turn (it is a small file a person edits); a broken one is {}."""
    from . import carabiner
    path = carabiner.find(cfg.claude)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    s = data.get("session") if isinstance(data, dict) else None
    if not isinstance(s, dict):
        return {}
    mode = str(s.get("permission_mode") or "")
    tools = s.get("allowed_tools") or []
    if isinstance(tools, str):                       # the stdlib reader's form: comma separated
        tools = [t for t in (x.strip() for x in tools.split(",")) if t]
    try:
        tools = clean_rules(list(tools)) if tools else []
    except ValueError:
        tools = []
    return {"permission_mode": mode if mode in ALL_MODES else "", "allowed_tools": tools, "path": str(path)}


def layers(cfg: SymposiumConfig, preset: Optional[FolderPreset], settings: dict[str, Any],
           session_block: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """The layers for a chat, the most particular first."""
    k = carabiner_session(cfg) if session_block is None else session_block
    out = [{"name": "chat", "mode": settings.get("permission_mode") or "",
            "allow": list(settings.get("allowed_tools") or []), "inherit": settings.get("tools_inherit") is not False}]
    if preset is not None:
        out.append({"name": "project", "mode": preset.permission_mode or "", "allow": list(preset.allowed_tools),
                    "inherit": preset.tools_inherit is not False})
    out.append({"name": "symposium", "mode": cfg.claude.permission_mode or "",
                "allow": list(cfg.claude.allowed_tools), "inherit": True})
    out.append({"name": "carabiner", "mode": k.get("permission_mode") or "",
                "allow": list(k.get("allowed_tools") or []), "inherit": True})
    return out


def effective(stack: list[dict[str, Any]]) -> dict[str, Any]:
    """What a turn runs with: the mode and where it came from, the allow list
    and where each rule came from, and where inheriting stopped."""
    mode, mode_from = "", "cli"
    for layer in stack:
        if layer["mode"]:
            mode, mode_from = layer["mode"], layer["name"]
            break
    allow: list[str] = []
    came: dict[str, str] = {}
    stopped_at = None
    for layer in stack:
        for rule in layer["allow"]:
            if rule not in came:
                came[rule] = layer["name"]
                allow.append(rule)
        if not layer["inherit"]:
            stopped_at = layer["name"]
            break
    counts = {name: sum(1 for r in allow if came[r] == name) for name in LAYERS}
    return {"mode": mode, "mode_from": mode_from, "allow": allow, "from": came, "counts": counts,
            "stopped_at": stopped_at}


def for_chat(cfg: SymposiumConfig, folder: str, settings: dict[str, Any]) -> dict[str, Any]:
    return effective(layers(cfg, cfg.folder(folder), settings or {}))


# ── Reading a rule ────────────────────────────────────────────────────────

def _spelled(path: str) -> str:
    """A path as the CLI's file rules spell it: forward slashes, a Windows
    drive as /c (claude_voice.folder_rule's spelling), no trailing slash."""
    p = str(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p.rstrip("/")


def _target(tool_input: dict[str, Any]) -> str:
    for k in ("file_path", "notebook_path", "path"):
        if isinstance(tool_input.get(k), str) and tool_input[k]:
            return tool_input[k]
    return ""


def covers(rule: str, tool: str, tool_input: Any) -> bool:
    """Whether `rule` allows `tool` with `tool_input`, as far as Symposium can
    read it. Unknown shapes are never taken to cover anything."""
    rule, tool = str(rule or "").strip(), str(tool or "")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if not rule or not tool:
        return False
    if rule == tool:
        return True
    if rule.startswith("mcp__"):
        if rule.endswith("__*"):
            return tool.startswith(rule[:-1])                  # mcp__server__* : any of its tools
        if rule.count("__") == 1:
            return tool.startswith(rule + "__")                # mcp__server : the same
        return False
    m = re.fullmatch(r"([A-Za-z]+)\((.*)\)", rule)
    if m is None:                                                # a bare name: the whole tool, or a file kind
        return _KIND.get(tool) == rule and rule in ("Read", "Edit")
    name, spec = m.group(1), m.group(2).strip()
    if name in ("Read", "Edit"):
        if _KIND.get(tool) != name or not spec.startswith("//"):
            return False
        where = spec[1:]
        if where.endswith("/**"):
            where = where[:-3]
        target = _target(tool_input)
        if not target:
            return False
        windows = "\\" in target or (len(target) > 1 and target[1] == ":")
        path = _spelled(str(PureWindowsPath(target) if windows else PurePosixPath(target)))
        base = where.rstrip("/")
        return path == base or path.startswith(base + "/")
    if name != tool:
        return False
    if name == "Bash" and spec.endswith(":*"):
        prefix, command = spec[:-2].strip(), str(tool_input.get("command") or "").strip()
        return bool(prefix) and (command == prefix or command.startswith(prefix + " "))
    if name == "WebFetch" and spec.startswith("domain:"):
        host = (urlparse(str(tool_input.get("url") or "")).hostname or "").lower()
        want = spec[len("domain:"):].strip().lower()
        return bool(host) and (host == want or host.endswith("." + want))
    return False
