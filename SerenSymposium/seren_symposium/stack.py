"""
seren_symposium.stack
════════════════════════════════════════════════════════════════════════

What a chat's model is given, layer by layer (design note, 22 Oct 2026: the
whole system prompt stack, visible per chat, so default tools and anything
else nobody uses can be seen and cleared; system prompts live at the project
and the chat, and add up).

THE PROMPTS ADD UP. A project's prompt, then the chat's after it. A chat that
unticks "after its project's prompt" (its system_inherit set false) has only
its own: the way it was before 22 Oct, when a chat's prompt replaced its
project's.

FOR THE CLAUDE VOICE the stack is, in order:

    the system prompt    the CLI's own (built in, not readable from here) or
                         the preface in its place; then the project's, the
                         chat's and the framing (app.claude_system).
    loaded by the CLI    the memory files it reads itself at start (CLAUDE.md
                         up from its working folder, the user's, the auto
                         memory index) and the session-start hooks, whose
                         output nobody here sees.
    tools and extras     what the CLI's init event said it loaded on this
                         chat's last turn (claude_voice.digest_init): every
                         tool, grouped by MCP server with each server's
                         status, the skills, agents, commands and plugins.
                         Before a chat's first answer, another chat's.
    this turn            the permissions (seren_symposium.permissions) and
                         whether the next turn resumes or hands the chat over.

FOR A LOCAL MODEL it is what Lodestar would send, asked of Lodestar itself
(POST /api/v1/chat/inspect): its tool descriptions in front of the project's
and the chat's prompt, then the history, then the sampling settings. Nothing
here guesses at what Lodestar adds.

Read only, and only from this box and Lodestar. Sizes are characters; a token
is about four (est.).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .config import FolderPreset, SymposiumConfig

MAX_FILE_CHARS = 60_000
# The auto memory index: the CLI reads its first lines into every session.
MEMORY_INDEX_LINES = 200


def own_prompt(preset: Optional[FolderPreset], settings: dict[str, Any]) -> str:
    """The project's prompt, then the chat's; only the chat's when it says
    not to inherit."""
    return "\n\n".join(text for _, text in own_parts(preset, settings))


def own_parts(preset: Optional[FolderPreset], settings: dict[str, Any]) -> list[tuple[str, str]]:
    chat = str(settings.get("system_prompt") or "").strip()
    project = (preset.system_prompt if preset else "").strip()
    if settings.get("system_inherit") is False:
        project = ""
    return [(k, t) for k, t in (("project", project), ("chat", chat)) if t]


def est_tokens(chars: int) -> int:
    return (chars + 3) // 4


def _layer(key: str, group: str, title: str, *, text: Optional[str] = None, where: str = "",
           state: str = "on", note: str = "", edit: str = "", **more: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "group": group, "title": title, "where": where, "state": state}
    if text is not None:
        out["text"] = text
        out["chars"] = len(text)
    if note:
        out["note"] = note
    if edit:
        out["edit"] = edit
    out.update(more)
    return out


def _prompt_layers(preset: Optional[FolderPreset], settings: dict[str, Any], folder: str) -> list[dict[str, Any]]:
    """The project's and the chat's prompts, each shown even when empty, so
    the place to put one is visible."""
    inherit = settings.get("system_inherit") is not False
    project = (preset.system_prompt if preset else "").strip()
    chat = str(settings.get("system_prompt") or "").strip()
    out = []
    if folder:
        out.append(_layer("project", "system", f"Project: {folder}", text=project if inherit else "",
                          where="its project's settings", edit="project",
                          state=("off" if not inherit and project else "on" if project else "empty"),
                          note=("Left out: this chat says not to inherit it." if not inherit and project else "")))
    out.append(_layer("chat", "system", "This chat", text=chat, where="this chat's settings (⚙)", edit="chat",
                      state="on" if chat else "empty",
                      note=("Added after its project's prompt." if chat and project and inherit else "")))
    return out


# ── The claude voice ──────────────────────────────────────────────────────

def memory_files(cwd: str, auto_dir: str = "") -> list[dict[str, Any]]:
    """The memory files the claude CLI reads itself at start, as found on
    this box: the user's CLAUDE.md, then CLAUDE.md, .claude/CLAUDE.md and
    CLAUDE.local.md in its working folder and each folder above it, then the
    auto memory index. What the CLI loads can differ (imports inside them are
    not followed here; a managed policy file is not looked for)."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: Path, kind: str, lines: Optional[int] = None) -> None:
        try:
            key = str(path.resolve()).lower()
            if key in seen or not path.is_file():
                return
            seen.add(key)
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if lines is not None:
            text = "\n".join(text.splitlines()[:lines])
        found.append({"path": str(path), "kind": kind, "chars": len(text), "text": text[:MAX_FILE_CHARS]})

    add(Path.home() / ".claude" / "CLAUDE.md", "yours")
    if cwd:
        here = Path(os.path.expanduser(cwd))
        for d in [here, *here.parents]:
            for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
                add(d / name, "project" if d == here else "above")
    if auto_dir:
        add(Path(os.path.expanduser(auto_dir)) / "MEMORY.md", "auto memory", MEMORY_INDEX_LINES)
    return found


def session_hooks(cwd: str) -> list[str]:
    """Where a session-start hook is set (the settings files that name one):
    each runs as the CLI starts and adds what it prints, which nothing here
    sees. Only the hooks key is read; the commands are not shown."""
    files = [Path.home() / ".claude" / "settings.json"]
    if cwd:
        base = Path(os.path.expanduser(cwd))
        files += [base / ".claude" / "settings.json", base / ".claude" / "settings.local.json"]
    out = []
    for f in files:
        try:
            hooks = (json.loads(f.read_text(encoding="utf-8")) or {}).get("hooks") or {}
        except (OSError, ValueError, AttributeError):
            continue
        n = sum(len(m.get("hooks") or []) for m in (hooks.get("SessionStart") or []) if isinstance(m, dict))
        if n:
            out.append(f"{f} ({n})")
    return out


def _server_key(name: str) -> str:
    """An MCP server's name as the CLI spells it in its tools' names."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def group_tools(init: dict[str, Any]) -> dict[str, Any]:
    """The init's tools: the CLI's own, then each MCP server's, with the
    server's status; a tool whose server the init did not list is grouped
    under its prefix anyway."""
    tools = [t for t in init.get("tools") or [] if isinstance(t, str)]
    servers = [dict(s) for s in init.get("mcp_servers") or [] if isinstance(s, dict) and s.get("name")]
    by_key = {_server_key(s["name"]): s for s in servers}
    for s in servers:
        s["tools"] = []
    builtin = []
    for t in tools:
        if not t.startswith("mcp__"):
            builtin.append(t)
            continue
        key, _, rest = t[len("mcp__"):].partition("__")
        s = by_key.get(key)
        if s is None:
            s = by_key[key] = {"name": key, "status": "", "source": "", "tools": []}
            servers.append(s)
        s["tools"].append(rest or t)
    servers.sort(key=lambda s: -len(s["tools"]))
    return {"builtin": builtin, "servers": servers, "count": len(tools)}


def claude_stack(cfg: SymposiumConfig, preset: Optional[FolderPreset], folder: str,
                 settings: dict[str, Any], *, preface: str, framing: str,
                 init: dict[str, Any], init_from: str, perms: dict[str, Any],
                 turn: dict[str, Any]) -> list[dict[str, Any]]:
    """The claude voice's layers (see the module docstring). `init` is the
    digest of the CLI's last init event and `init_from` whose it is ("this
    chat", "another chat", or "" when none is known yet); `turn` says
    whether the next turn resumes and what it would hand over."""
    version = init.get("version") or ""
    layers = [_layer("cli", "system", "Claude Code's own prompt",
                     where=f"built into the CLI{f' {version}' if version else ''}",
                     state="replaced" if preface else "on",
                     note=("Replaced by the preface below." if preface
                           else "Built in, so it can't be read from here. Several thousand words; "
                                "a preface (claude.preface) replaces it."))]
    if preface or cfg.claude.preface.url or cfg.claude.preface.file:
        layers.append(_layer("preface", "system", "The preface",
                             where=cfg.claude.preface.file or f"{cfg.claude.preface.url.rstrip('/')}/preface",
                             text=preface, state="on" if preface else "empty",
                             note="" if preface else "Set, but nothing came back: the CLI keeps its own prompt."))
    layers += _prompt_layers(preset, settings, folder)
    layers.append(_layer("framing", "system", "The framing", where="claude.framing in the yaml",
                         text=framing, state="on" if framing else "empty",
                         note="What each turn is told about the window."))

    cwd = init.get("cwd") or cfg.claude.cwd or os.getcwd()
    files = memory_files(cwd, init.get("memory_auto") or "")
    layers.append(_layer("memory", "loaded", "Memory files", where=f"read by the CLI from {cwd}",
                         chars=sum(f["chars"] for f in files), files=files,
                         state="on" if files else "empty",
                         note="The CLI reads these itself at start, whatever the system prompt says."))
    hooks = session_hooks(cwd)
    layers.append(_layer("hooks", "loaded", "Session-start hooks", where="Claude Code's settings files",
                         items=hooks, count=len(hooks), state="on" if hooks else "empty",
                         note=("Each runs as the CLI starts and adds what it prints (a carabiner's bookmark, "
                               "say); that text is not shown here." if hooks else "")))

    if init:
        grouped = group_tools(init)
        trim = ("Only some servers load: claude.mcp_servers." if cfg.claude.mcp_servers
                else "Every server the CLI knows loads, the account's connectors too; "
                     "claude.mcp_servers in the yaml names the only ones to load.")
        layers.append(_layer("tools", "tools", "Tools", where=f"as the CLI loaded them for {init_from}",
                             count=grouped["count"], builtin=grouped["builtin"], servers=grouped["servers"],
                             note=trim + (" With ToolSearch among them, an MCP tool's description is read "
                                          "when it is first needed; its name is there from the start."
                                          if "ToolSearch" in grouped["builtin"] else "")))
        for key, title in (("skills", "Skills"), ("agents", "Agents"), ("slash_commands", "Commands"),
                           ("plugins", "Plugins")):
            got = init.get(key) or []
            layers.append(_layer(key, "tools", title, where=f"as the CLI loaded them for {init_from}",
                                 items=got, count=len(got), state="on" if got else "empty"))
    else:
        layers.append(_layer("tools", "tools", "Tools", state="unknown",
                             note="Known after the first answer from Claude here: the CLI says what it "
                                  "loaded when it starts."))

    mode = perms.get("mode") or ""
    layers.append(_layer("permissions", "turn", "Permissions", where="CLI > carabiner > every chat > project > chat",
                         mode=mode, mode_from=perms.get("mode_from"), counts=perms.get("counts") or {},
                         allow=perms.get("allow") or [], stopped_at=perms.get("stopped_at"),
                         note="The CLI's own settings always count too."))
    layers.append(_layer("history", "turn", "The conversation", **turn))
    return layers


# ── A local model, through Lodestar ───────────────────────────────────────

def local_stack(cfg: SymposiumConfig, preset: Optional[FolderPreset], folder: str, settings: dict[str, Any],
                *, inspect: Optional[dict[str, Any]], inspect_error: str,
                history: dict[str, Any], sampling: dict[str, Any]) -> list[dict[str, Any]]:
    """A local model's layers: Lodestar's tool descriptions (from its own
    /chat/inspect), the project's and the chat's prompts, the history and
    the sampling settings."""
    if inspect is not None:
        n = int(inspect.get("tools_block_chars") or 0)
        first = (inspect.get("messages") or [{}])[0]
        block = first.get("content", "")[:n] if n and first.get("role") == "system" else ""
        tools = [_layer("tools", "system", "Lodestar's tools", where=f"Lodestar, in its {inspect.get('dialect')} dialect",
                        text=block, count=int(inspect.get("tools_count") or 0),
                        items=list(inspect.get("tool_names") or []), state="on" if block else "empty",
                        note=("Put in front of the system prompt on every turn. Which tools Lodestar offers is "
                              "its own config (tooling); a tool that is never used still costs this."
                              if block else "Lodestar offers no tools."))]
    else:
        tools = [_layer("tools", "system", "Lodestar's tools", state="unknown",
                        note=f"Lodestar did not say ({inspect_error}).")]
    layers = tools + _prompt_layers(preset, settings, folder)
    layers.append(_layer("history", "turn", "The conversation", **history))
    layers.append(_layer("sampling", "turn", "Sampling", where="this chat, else its project, else Lodestar's",
                         values=sampling))
    return layers


def summarize(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Characters by group, and the tokens they come to (est.)."""
    by: dict[str, int] = {}
    for layer in layers:
        if layer.get("state") in ("off", "replaced", "unknown"):
            continue
        by[layer["group"]] = by.get(layer["group"], 0) + int(layer.get("chars") or 0)
    total = sum(by.values())
    return {"chars": by, "total_chars": total, "est_tokens": est_tokens(total)}
