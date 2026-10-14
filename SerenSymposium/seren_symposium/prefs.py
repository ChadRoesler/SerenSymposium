"""
seren_symposium.prefs
════════════════════════════════════════════════════════════════════════

What you set in the window, kept where an installer never writes.

The yaml is the INSTALL's: Starwright writes it whole on every reinstall, from
the flags it was given, and anything it writes wins. A name typed into the
Settings tab, a project made with + project, a project's system prompt - put
in the yaml, the next reinstall would quietly put them back. So they live
here instead, in symposium-prefs.json beside the sessions folder, and are
laid over the yaml when Symposium starts:

    ui.you                  your name on your messages
    claude.name             what the claude voice signs as
    ui.confirm_destructive  ask before a delete or a rewind
    claude.permission_mode  how the claude voice is asked about tools,
    claude.ask              whether it is asked in the window,
    claude.allowed_tools    what it may use without asking (an "Always"
                            answer adds to it),
    claude.add_dirs         and the folders it may work in besides its own
    folders                 the projects, and each one's defaults

SPARSE: only what was changed here is kept. Until then the yaml's value
shows through - so a name still set by Starwright is Starwright's until you
change it in the window, and from then on it is yours.

PROJECTS are seeded: the first time one is made, renamed, changed or removed
here, the yaml's `folders:` are copied in and this file owns the list from
then on. A project in the yaml can be renamed like any other.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from .config import FolderPreset, SymposiumConfig

log = logging.getLogger(__name__)

FILE_NAME = "symposium-prefs.json"
MAX_NAME = 120
# The settings a project carries, and how a value for each is checked.
PROJECT_FIELDS = ("voice", "system_prompt", "model", "temperature", "max_tokens", "repeat_penalty")
APP_FIELDS = {"you": ("ui", "you"), "claude_name": ("claude", "name"),
              "confirm_destructive": ("ui", "confirm_destructive"),
              "claude_mode": ("claude", "permission_mode"), "claude_ask": ("claude", "ask"),
              "claude_tools": ("claude", "allowed_tools"), "claude_dirs": ("claude", "add_dirs")}
# Not bypassPermissions: a window that can switch every check off with one
# click is not one to leave open. The yaml can still say it, on purpose.
MODES = ("", "default", "acceptEdits", "plan")
MAX_RULES = 200


class Invalid(ValueError):
    pass


def prefs_path(cfg: SymposiumConfig) -> Path:
    return cfg.sessions_dir().parent / FILE_NAME


def _clean_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise Invalid("a project needs a name")
    return raw.strip()[:MAX_NAME]


def _preset(name: str, values: dict[str, Any]) -> FolderPreset:
    unknown = set(values) - set(PROJECT_FIELDS) - {"memory_tags"}
    if unknown:
        raise Invalid(f"not a project setting: {', '.join(sorted(unknown))}")
    if values.get("voice") not in (None, "", "lodestar", "claude"):
        raise Invalid("voice is lodestar or claude")
    try:
        # None means "no opinion" - the field's default.
        return FolderPreset(name=name, **{k: v for k, v in values.items() if v is not None})
    except ValidationError as ex:
        raise Invalid(ex.errors()[0].get("msg", "bad value")) from None


class Prefs:
    """The prefs file, and the config it is laid over (changed in place, so
    everything holding the config sees a change at once)."""

    def __init__(self, cfg: SymposiumConfig, path: Optional[Path] = None):
        self.cfg = cfg
        self.path = path or prefs_path(cfg)
        self.data: dict[str, Any] = self._read()
        self.apply()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as ex:
            # Never lose the file over it: a broken one is left where it is,
            # and the yaml stands alone until it is mended or replaced.
            log.warning("[symposium] %s could not be read (%s); using the yaml alone", self.path, ex)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".prefs-", suffix=".json", dir=self.path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def apply(self) -> None:
        for key, (block, attr) in APP_FIELDS.items():
            if key in self.data:
                setattr(getattr(self.cfg, block), attr, self.data[key])
        if isinstance(self.data.get("projects"), list):
            kept = []
            for p in self.data["projects"]:
                try:
                    kept.append(FolderPreset(**p))
                except (TypeError, ValidationError):
                    log.warning("[symposium] skipping a project in %s that does not parse: %r", self.path, p)
            self.cfg.folders = kept

    # ── the app's own settings ──────────────────────────────────────────

    def set_app(self, changes: Any) -> None:
        if not isinstance(changes, dict) or not changes:
            raise Invalid("send the settings to change")
        clean = {key: self._check(key, value) for key, value in changes.items()}
        self.data.update(clean)
        self._write()
        self.apply()

    @staticmethod
    def _check(key: str, value: Any) -> Any:
        if key not in APP_FIELDS:
            raise Invalid(f"{key} is not set from the window")
        if key in ("confirm_destructive", "claude_ask"):
            if not isinstance(value, bool):
                raise Invalid(f"{key} is true or false")
            return value
        if key == "claude_mode":
            if value not in MODES:
                raise Invalid(f"claude_mode is one of: {', '.join(m or '(the CLI default)' for m in MODES)}")
            return value
        if key in ("claude_tools", "claude_dirs"):
            if not isinstance(value, list) or len(value) > MAX_RULES \
                    or not all(isinstance(v, str) and v.strip() for v in value):
                raise Invalid(f"{key} is a list of names")
            out = list(dict.fromkeys(v.strip()[:300] for v in value))
            if key == "claude_dirs" and not all(os.path.isabs(os.path.expanduser(d)) for d in out):
                raise Invalid("a folder is a full path (C:\\work, /home/alice/notes)")
            return out
        if not isinstance(value, str) or not value.strip():
            raise Invalid(f"{key} needs a name")
        return value.strip()[:60]

    def allow_tool(self, rule: str) -> None:
        """An "Always" answer: the rule joins what the claude voice may use
        without asking, from the next turn on (this turn keeps it too)."""
        tools = list(self.data.get("claude_tools", self.cfg.claude.allowed_tools))
        if rule and rule not in tools:
            self.set_app({"claude_tools": [*tools, rule]})

    # ── projects ────────────────────────────────────────────────────────

    def _owned(self) -> list[dict[str, Any]]:
        """The project list, taking it over from the yaml the first time."""
        if not isinstance(self.data.get("projects"), list):
            self.data["projects"] = [f.model_dump() for f in self.cfg.folders]
        return self.data["projects"]

    def _find(self, name: str) -> Optional[dict[str, Any]]:
        return next((p for p in self._owned() if p.get("name") == name), None)

    def add_project(self, body: Any) -> FolderPreset:
        if not isinstance(body, dict):
            raise Invalid("send a JSON object")
        name = _clean_name(body.get("name"))
        if self._find(name) is not None:
            raise Invalid(f"there is already a project called {name}")
        preset = _preset(name, {k: v for k, v in body.items() if k != "name"})
        self._owned().append(preset.model_dump())
        self._write()
        self.apply()
        return preset

    def change_project(self, name: str, values: Any, rename: Optional[str] = None) -> FolderPreset:
        """New settings for project `name` (a value sent as null drops back to
        no opinion), and/or a new name. Its chats are the caller's to move."""
        if not isinstance(values, dict):
            raise Invalid("send the settings to change as an object")
        p = self._find(name)
        if p is None:
            # A name only chats use (a project that never had settings) gets
            # an entry the moment it is given some.
            p = FolderPreset(name=name).model_dump()
            self._owned().append(p)
        merged = {k: v for k, v in p.items() if k != "name"}
        merged.update(values)
        new_name = name
        if rename is not None:
            new_name = _clean_name(rename)
            if new_name != name and self._find(new_name) is not None:
                # Into one that exists: the chats merge, the settings of the
                # one being renamed go.
                self._owned().remove(p)
                self._write()
                self.apply()
                return next(f for f in self.cfg.folders if f.name == new_name)
        preset = _preset(new_name, merged)
        p.clear()
        p.update(preset.model_dump())
        self._write()
        self.apply()
        return preset

    def remove_project(self, name: str) -> bool:
        p = self._find(name)
        if p is None:
            return False
        self._owned().remove(p)
        self._write()
        self.apply()
        return True
