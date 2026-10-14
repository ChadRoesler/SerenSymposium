"""
seren_symposium.sessions
════════════════════════════════════════════════════════════════════════

Conversations, kept. One JSON file per session in the sessions dir
(SymposiumConfig.sessions_dir):

    {"id", "folder", "title", "created", "updated",
     "settings": {voice, system_prompt, model_override, temperature, max_tokens, repeat_penalty},
     "messages": [{"role", "content", "at", "meta"?}, ...],
     "claude"?: {"session", "seen"}}

`claude` appears once Claude has answered in the session: the CLI session it
resumes, and how many of the messages that conversation has seen (the rest
were answered by another voice, and are handed over on the next Claude turn).

WHY HERE AND NOT IN THE WEBVIEW: see SessionsConfig. The short of it is that
localStorage belongs to an origin, and the origin has a port in it.

A session belongs to a folder; its settings sit on top of the folder's preset
(app.to_lodestar_payload has the order). Only a turn that came back is
written: a failed one is shown and forgotten, so it never becomes context
that every later message replays.

Writes go to a temp file and are moved into place, so a crash mid-write
leaves the old session, not half of a new one.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

SETTINGS = {
    "voice": str,
    "system_prompt": str,
    "model_override": str,
    "temperature": (int, float),
    "max_tokens": int,
    "repeat_penalty": (int, float),
}
ROLES = ("user", "assistant")
VOICES = ("lodestar", "claude")
MAX_TITLE = 120
_ID = re.compile(r"^[0-9a-f]{12,32}$")


class Invalid(ValueError):
    """What the caller sent cannot be a session; the message says why."""


def clean_settings(raw: Any) -> dict[str, Any]:
    """Only the known keys, of the right type. None or "" means "no opinion"
    and drops the key, so the folder's value shows through again."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Invalid("settings is an object")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in SETTINGS:
            raise Invalid(f"unknown setting {key!r}")
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, SETTINGS[key]):
            raise Invalid(f"{key} has the wrong type")
        if key == "voice" and value not in VOICES:
            raise Invalid(f"voice is one of {', '.join(VOICES)}")
        out[key] = value
    return out


def clean_messages(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise Invalid("messages is a list")
    out = []
    for m in raw:
        if not (isinstance(m, dict) and m.get("role") in ROLES and isinstance(m.get("content"), str)):
            raise Invalid("each message is {role: user|assistant, content: str}")
        kept = {"role": m["role"], "content": m["content"], "at": m.get("at") or _now()}
        if isinstance(m.get("meta"), dict):
            kept["meta"] = m["meta"]
        out.append(kept)
    return out


def _now() -> float:
    return round(time.time(), 3)


def summary(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": s["id"], "folder": s["folder"], "title": s["title"],
            "created": s["created"], "updated": s["updated"], "count": len(s["messages"])}


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sid: str) -> Optional[Path]:
        # The id names a file, so it is checked before it is used as one.
        return self.root / f"{sid}.json" if isinstance(sid, str) and _ID.match(sid) else None

    def _write(self, s: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(s["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def list(self) -> list[dict[str, Any]]:
        out = []
        if self.root.is_dir():
            for path in self.root.glob("*.json"):
                s = self.get(path.stem)
                if s is not None:
                    out.append(summary(s))
        return sorted(out, key=lambda s: s["updated"], reverse=True)

    def get(self, sid: str) -> Optional[dict[str, Any]]:
        path = self._path(sid)
        if path is None or not path.is_file():
            return None
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return s if isinstance(s, dict) and s.get("id") == sid else None

    def create(self, folder: str = "", title: str = "", settings: Any = None,
               messages: Any = None) -> dict[str, Any]:
        """A new session. With messages it is an import: the file another
        Symposium exported comes back as a new session here."""
        if not isinstance(folder, str) or not isinstance(title, str):
            raise Invalid("folder and title are strings")
        now = _now()
        s = {"id": uuid.uuid4().hex, "folder": folder, "title": title[:MAX_TITLE],
             "created": now, "updated": now,
             "settings": clean_settings(settings), "messages": clean_messages(messages)}
        self._write(s)
        return s

    def update(self, sid: str, changes: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Rename (title), move (folder), or change settings. Settings are
        merged: a key sent as null drops back to the folder's value."""
        s = self.get(sid)
        if s is None:
            return None
        for key in changes:
            if key not in ("title", "folder", "settings"):
                raise Invalid(f"{key} cannot be changed")
        if "title" in changes:
            if not isinstance(changes["title"], str):
                raise Invalid("title is a string")
            s["title"] = changes["title"][:MAX_TITLE]
        if "folder" in changes:
            if not isinstance(changes["folder"], str):
                raise Invalid("folder is a string")
            s["folder"] = changes["folder"]
        if "settings" in changes:
            if not isinstance(changes["settings"], dict):
                raise Invalid("settings is an object")
            merged = {**s["settings"], **changes["settings"]}
            s["settings"] = clean_settings(merged)
        s["updated"] = _now()
        self._write(s)
        return s

    def delete(self, sid: str) -> bool:
        path = self._path(sid)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    def record(self, sid: str, prompt: str, answer: str, meta: dict[str, Any],
               keep: Optional[int] = None, claude_session: Optional[str] = None
               ) -> Optional[dict[str, Any]]:
        """A turn that came back. keep cuts the thread first (a retry
        replaces the exchange it retried). A session with no title is named
        after its first prompt. claude_session: Claude answered, in that CLI
        session, and has now seen the whole thread."""
        s = self.get(sid)
        if s is None:
            return None
        if keep is not None:
            s["messages"] = s["messages"][:keep]
        now = _now()
        s["messages"] += [{"role": "user", "content": prompt, "at": now},
                          {"role": "assistant", "content": answer, "at": now, "meta": meta}]
        if not s["title"]:
            s["title"] = " ".join(prompt.split())[:48]
        if claude_session:
            s["claude"] = {"session": claude_session, "seen": len(s["messages"])}
        s["updated"] = now
        self._write(s)
        return s
