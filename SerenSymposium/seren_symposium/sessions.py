"""
seren_symposium.sessions
════════════════════════════════════════════════════════════════════════

Conversations, kept. One JSON file per session in the sessions dir
(SymposiumConfig.sessions_dir):

    {"id", "folder", "title", "created", "updated",
     "settings": {voice, system_prompt, model_override, temperature, max_tokens, repeat_penalty},
     "messages": [{"role", "content", "at", "meta"?, "files"?}, ...],
     "claude"?: {"session", "seen"}}

A message's `files` are what was attached to it - [{"name", "orig", "type",
"size"}] - and they live beside the session, in <id>.files/. A file is never
deleted by an edit: rewinding past a message and sending it again names the
same files. Deleting the chat takes the folder with it.

`claude` appears once Claude has answered in the session: the CLI session it
resumes, and how many of the messages that conversation has seen (the rest
were answered by another voice, and are handed over on the next Claude turn).
The CLI cannot unsay anything, so a change to messages it has seen - a
rewind, an edit, a deleted message - drops `claude`: the next turn starts a
new CLI conversation with the chat as it now stands handed over. A fork, not
an undo.

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
import shutil
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
# A stored file: eight hex, then the name it came with, made safe.
_FILE = re.compile(r"^[0-9a-f]{8}-[A-Za-z0-9._-]{1,80}$")
MAX_ORIG = 200


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
        # A message that did not come from the person: who fed it (the game
        # table, say) stays on it, so it is never drawn as theirs.
        if isinstance(m.get("source"), str) and m["source"]:
            kept["source"] = m["source"][:40]
            if isinstance(m.get("by"), str) and m["by"]:
                kept["by"] = m["by"][:80]
        files = [f for f in m.get("files") or [] if isinstance(f, dict) and isinstance(f.get("name"), str)]
        if files:
            kept["files"] = files
        out.append(kept)
    return out


def safe_name(orig: str) -> str:
    """A stored file's name: unique, and nothing a path could be built from."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(orig or "")).strip("._") or "file"
    if len(stem) > 80:
        root, dot, ext = stem.rpartition(".")
        stem = (root[:80 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else stem[:80]
    return f"{uuid.uuid4().hex[:8]}-{stem}"


def file_entry(name: str, orig: str, mime: str, size: int) -> dict[str, Any]:
    return {"name": name, "orig": (orig or name)[:MAX_ORIG], "type": mime or "application/octet-stream",
            "size": size}


def _now() -> float:
    return round(time.time(), 3)


def summary(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": s["id"], "folder": s["folder"], "title": s["title"],
            "created": s["created"], "updated": s["updated"], "count": len(s["messages"]),
            # what has spoken into this chat besides the person (["table"]), for the badge
            "sources": list(s.get("sources") or [])}


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sid: str) -> Optional[Path]:
        # The id names a file, so it is checked before it is used as one.
        return self.root / f"{sid}.json" if isinstance(sid, str) and _ID.match(sid) else None

    def files_dir(self, sid: str) -> Optional[Path]:
        """Where a session's attachments live (made on first use)."""
        return self.root / f"{sid}.files" if isinstance(sid, str) and _ID.match(sid) else None

    def file_path(self, sid: str, name: str) -> Optional[Path]:
        """A stored file, by name - None for any name that is not one."""
        d = self.files_dir(sid)
        if d is None or not isinstance(name, str) or not _FILE.match(name):
            return None
        p = d / name
        return p if p.is_file() else None

    def add_file(self, sid: str, orig: str, data: bytes, mime: str) -> Optional[dict[str, Any]]:
        """Keep a file beside session `sid`; the entry a message can carry.
        None when there is no such session."""
        d = self.files_dir(sid)
        if d is None or self.get(sid) is None:
            return None
        d.mkdir(parents=True, exist_ok=True)
        name = safe_name(orig)
        (d / name).write_bytes(data)
        return file_entry(name, orig, mime, len(data))

    def listing(self, sid: str) -> set[str]:
        d = self.files_dir(sid)
        return {p.name for p in d.iterdir()} if d is not None and d.is_dir() else set()

    def adopt(self, sid: str, before: set[str]) -> list[tuple[str, str]]:
        """Files that appeared in the session's folder since `before` (the
        answer saved them there), renamed into stored names: [(old, new)]."""
        d = self.files_dir(sid)
        if d is None or not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir()):
            if p.name in before or not p.is_file():
                continue
            new = p.name if _FILE.match(p.name) else safe_name(p.name)
            if new != p.name:
                p.rename(d / new)
            out.append((p.name, new))
        return out

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

    def rename_folder(self, old: str, new: str) -> int:
        """Move every chat in folder `old` to `new`; how many moved. Their
        `updated` stays: a rename is not something said in them."""
        moved = 0
        for summ in self.list():
            if summ["folder"] != old:
                continue
            s = self.get(summ["id"])
            if s is not None:
                s["folder"] = new
                self._write(s)
                moved += 1
        return moved

    def delete(self, sid: str) -> bool:
        path = self._path(sid)
        if path is None or not path.is_file():
            return False
        path.unlink()
        shutil.rmtree(self.files_dir(sid), ignore_errors=True)
        return True

    @staticmethod
    def _forget_claude_past(s: dict[str, Any], index: int) -> None:
        """A change at message `index`: if Claude's conversation saw that far,
        it no longer matches the chat, and the next turn starts a new one."""
        if (s.get("claude") or {}).get("seen", 0) > index:
            s.pop("claude", None)

    def rewind(self, sid: str, to: Any) -> Optional[dict[str, Any]]:
        """Keep the first `to` messages; everything after goes."""
        s = self.get(sid)
        if s is None:
            return None
        if isinstance(to, bool) or not isinstance(to, int) or not 0 <= to <= len(s["messages"]):
            raise Invalid(f"to is a message count, 0..{len(s['messages'])}")
        s["messages"] = s["messages"][:to]
        self._forget_claude_past(s, to)
        s["updated"] = _now()
        self._write(s)
        return s

    def attach_file(self, sid: str, index: int, entry: dict[str, Any]) -> Optional[dict[str, Any]]:
        """A stored file joins message `index` (an answer said aloud joins
        the answer). None when there is no such session."""
        s = self.get(sid)
        if s is None:
            return None
        if not 0 <= index < len(s["messages"]):
            raise Invalid(f"no message {index}")
        s["messages"][index].setdefault("files", []).append(entry)
        s["updated"] = _now()
        self._write(s)
        return s

    def drop_message(self, sid: str, index: int) -> Optional[dict[str, Any]]:
        """Take one message out, leaving the rest where they were."""
        s = self.get(sid)
        if s is None:
            return None
        if not 0 <= index < len(s["messages"]):
            raise Invalid(f"no message {index}")
        del s["messages"][index]
        self._forget_claude_past(s, index)
        s["updated"] = _now()
        self._write(s)
        return s

    def fork(self, sid: str, at: Any) -> Optional[dict[str, Any]]:
        """A new chat in the same project: the first `at` messages, their
        files, the same settings - and, when Claude answers there, its own
        conversation, with this history handed over."""
        s = self.get(sid)
        if s is None:
            return None
        if isinstance(at, bool) or not isinstance(at, int) or not 1 <= at <= len(s["messages"]):
            raise Invalid(f"at is a message count, 1..{len(s['messages'])}")
        title = f"{s['title'] or 'untitled'} (fork)"[:MAX_TITLE]
        new = self.create(folder=s["folder"], title=title, settings=s["settings"],
                          messages=s["messages"][:at])
        dst = self.files_dir(new["id"])
        for m in new["messages"]:
            for f in m.get("files") or []:
                p = self.file_path(sid, f["name"])
                if p is not None:
                    dst.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst / f["name"])
        return new

    def record(self, sid: str, prompt: str, answer: str, meta: dict[str, Any],
               keep: Optional[int] = None, claude_session: Optional[str] = None,
               files: Optional[list[dict[str, Any]]] = None,
               answer_files: Optional[list[dict[str, Any]]] = None,
               source: Optional[str] = None, by: str = "") -> Optional[dict[str, Any]]:
        """A turn that came back. keep cuts the thread first (a retry
        replaces the exchange it retried). A session with no title is named
        after its first prompt. claude_session: Claude answered, in that CLI
        session, and has now seen the whole thread. files: what the prompt
        carried; answer_files: what the answer brought back. source: the
        prompt was not the person's but fed in (the game table: "table"),
        shown under `by`; the chat remembers every source that spoke in it."""
        s = self.get(sid)
        if s is None:
            return None
        if keep is not None:
            s["messages"] = s["messages"][:keep]
            self._forget_claude_past(s, keep)
        now = _now()
        asked: dict[str, Any] = {"role": "user", "content": prompt, "at": now}
        if files:
            asked["files"] = files
        if source:
            asked["source"] = source[:40]
            asked["by"] = (by or source)[:80]
            s["sources"] = sorted(set(s.get("sources") or []) | {source[:40]})
        said: dict[str, Any] = {"role": "assistant", "content": answer, "at": now, "meta": meta}
        if answer_files:
            said["files"] = answer_files
        s["messages"] += [asked, said]
        if not s["title"]:
            s["title"] = " ".join(prompt.split())[:48]
        if claude_session:
            s["claude"] = {"session": claude_session, "seen": len(s["messages"])}
        s["updated"] = now
        self._write(s)
        return s
