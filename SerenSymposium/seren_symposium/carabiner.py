"""
seren_symposium.carabiner
════════════════════════════════════════════════════════════════════════

The Claude Code carabiner clipped on this box (SerenCarabiners: kbh), seen
from Symposium: whose it is, where its sessions work, which Seren services
it routes to. The Settings tab shows it beside the claude voice's own
settings, so the Claude in the window and the Claude the carabiner wakes can
be looked at side by side.

Read only, and never its secrets: a route's bearer_token stays in the file.
Symposium does not call the carabiner's services; the CLI it runs does,
through the MCP servers the carabiner registered.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import ClaudeConfig

CLIP = "claude.yaml"


def find(cfg: ClaudeConfig) -> Optional[Path]:
    """The clip's claude.yaml: the one named in the config, else KBH_CONFIG,
    else <claude.cwd>/kbh, else ~/.seren/kbh - the first that is there."""
    tries: list[Path] = []
    if cfg.carabiner:
        p = Path(os.path.expanduser(cfg.carabiner))
        tries.append(p / CLIP if p.is_dir() else p)
    if os.environ.get("KBH_CONFIG"):
        tries.append(Path(os.environ["KBH_CONFIG"]))
    if cfg.cwd:
        tries.append(Path(os.path.expanduser(cfg.cwd)) / "kbh" / CLIP)
    tries.append(Path.home() / ".seren" / "kbh" / CLIP)
    return next((p for p in tries if p.is_file()), None)


def _route(v: Any) -> str:
    """A route as something to show: its url, or the connection file it
    names. Never the token beside the url."""
    if isinstance(v, dict):
        return str(v.get("url") or "")
    return str(v or "")


def describe(cfg: ClaudeConfig) -> Optional[dict[str, Any]]:
    path = find(cfg)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as ex:
        return {"path": str(path), "error": f"could not read it ({type(ex).__name__})"}
    if not isinstance(data, dict):
        return {"path": str(path), "error": "not a carabiner config"}
    servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    return {
        "path": str(path),
        "carabiner": str(data.get("carabiner") or ""),
        "project": str(data.get("project") or ""),
        "servers": {str(k): _route(v) for k, v in servers.items()},
        "bookmark": _route(data.get("bookmark")),
        "framing": str(data.get("framing") or ""),
    }
