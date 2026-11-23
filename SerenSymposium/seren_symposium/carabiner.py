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

import json
import os
import subprocess
import sys
import threading
import time
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


# ── Its models ──────────────────────────────────────────────────────────
# The list of models a harness offers is the carabiner's (what-the-carabiner-
# owns, 22 Oct 2026: a new model shows "the day it exists"): it asks Anthropic
# when it can and falls back to the list it shipped with. Symposium asks it
# through its own verb, `kbh claude config show --json`, and keeps the answer
# a while - finding the list can take it a few seconds. Nothing clipped, or no
# answer: None, and claude.models in Symposium's yaml stands in (22 Nov 2026).

MODELS_TTL = 600.0
MODELS_TIMEOUT = 20.0
_models_cache: dict[str, tuple[float, Optional[list[dict[str, str]]]]] = {}
_models_lock = threading.Lock()


def _ask_models(path: Path) -> Optional[list[dict[str, str]]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    kbh = path.parent / f"{data.get('carabiner') or 'claude'}.kbh" if isinstance(data, dict) else None
    if kbh is None or not kbh.is_file():
        return None
    python = str(data.get("python") or "") or sys.executable
    if not os.path.isfile(os.path.expanduser(python)):
        python = sys.executable
    try:
        done = subprocess.run([os.path.expanduser(python), str(kbh), "claude", "config", "show", "--json",
                               "--config", str(path)], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=MODELS_TIMEOUT, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        shown = json.loads(done.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    field = next((f for f in shown.get("fields") or [] if isinstance(f, dict) and f.get("key") == "session.model"), None)
    if field is None:
        return None
    out = [{"value": str(c["value"]), "label": str(c.get("label") or c["value"])}
           for c in field.get("choices") or [] if isinstance(c, dict) and c.get("value")]
    return out or None


def models(cfg: ClaudeConfig, fresh: bool = False) -> Optional[list[dict[str, str]]]:
    """[{value, label}] from the carabiner clipped here, or None. Blocking (a
    process, up to MODELS_TIMEOUT): call it off the event loop."""
    path = find(cfg)
    if path is None:
        return None
    key = str(path)
    with _models_lock:
        hit = _models_cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < MODELS_TTL:
            return hit[1]
    got = _ask_models(path)
    with _models_lock:
        _models_cache[key] = (time.monotonic(), got)
    return got


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
