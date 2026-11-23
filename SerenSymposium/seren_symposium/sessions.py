"""
seren_symposium.sessions
════════════════════════════════════════════════════════════════════════

Conversations, kept. One JSON file per session in the sessions dir
(SymposiumConfig.sessions_dir):

    {"id", "folder", "title", "created", "updated",
     "settings": {voice, system_prompt, system_inherit, model_override, temperature, max_tokens,
                  repeat_penalty, permission_mode, allowed_tools, tools_inherit},
     "messages": [{"role", "content", "at", "meta"?, "files"?}, ...],
     "claude"?: {"session", "seen"},
     "claude_init"?: {...}}

`claude_init` is what the claude CLI said it loaded on the chat's last turn
(claude_voice.digest_init, and when): its tools, MCP servers, skills and the
rest, for the prompt stack (seren_symposium.stack).

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

A chat PICKED UP from a terminal (pick_up, seren_symposium.terminal) starts
with `claude` already set - that conversation's id, every message seen - and
`"fork": true` when the terminal may still have it open: its first turn here
resumes into a new id, and the record of that turn drops the flag.

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
    # false: this chat's prompt only, its project's left out (22 Oct 2026:
    # the prompts add up, project then chat, unless a chat says not to)
    "system_inherit": bool,
    "model_override": str,
    "temperature": (int, float),
    "max_tokens": int,
    "repeat_penalty": (int, float),
    # the claude voice's permissions for this chat (seren_symposium.permissions)
    "permission_mode": str,
    "allowed_tools": list,
    "tools_inherit": bool,
    # how hard the claude voice thinks in this chat and what it shows of it
    # (claude --effort / --thinking-display), over its project's, every
    # chat's and the carabiner's (seren_symposium.permissions.thinking)
    "effort": str,
    "thinking_display": str,
    # the folder the claude voice runs in for this chat, over claude.cwd: a
    # chat picked up from a terminal runs where that conversation was started
    "cwd": str,
    # the colour of each voice's bubbles in this chat, as a hue: {who: 0-359}.
    # who: `you` (the person), `default` (the chat's one Claude), a local
    # model's or a plugin's name. A seat keeps its own (its `hue`). Chad, 17
    # Nov 2026: "color bubbles for all, set up in the per chat settings".
    "colors": dict,
    # the face each voice wears in THIS chat: {who: a picture in this chat's
    # files}. Over the one it wears everywhere (seren_symposium.avatars), so a
    # chat can have its own - Chad, 17 Nov 2026: "if you decide you want a
    # specific avatar for cheese tag, or whatever, you have one".
    "faces": dict,
    # how what you send while an answer is coming goes once it is in, over
    # every chat's (ui.queue): meld (as one message), each (one at a time) or
    # hold (they wait for you). Chad, 17 Nov 2026: "they should also be a per
    # chat with a default setting as well".
    "queue": str,
    # what the picture panel's translate is drafted from in this chat, over
    # every chat's (ui.translate): plain (your words), chat (and the recent
    # messages) or fork (a throwaway copy of the chat's Claude conversation)
    "translate": str,
    # when what you send mid-answer goes, over every chat's (ui.interrupt):
    # round (when the answer or the whole round is in), seat (when the seat
    # speaking is done; the rest of the round is not asked) or now (the answer
    # is stopped and it goes). A message can say otherwise as it is sent.
    "interrupt": str,
    # how this chat's pictures are laid out in the log, over every chat's
    # (ui.pictures): full, strip (side by side when there are several) or
    # folded (one line per message, opened with a click)
    "pictures": str,
    # the same for the pictures you send in this chat, over every chat's
    # (ui.my_pictures); unset, yours go as `pictures` says
    "my_pictures": str,
}
QUEUE_MODES = ("meld", "each", "hold")
INTERRUPT_MODES = ("round", "seat", "now")
TRANSLATE_MODES = ("plain", "chat", "fork")
PICTURE_MODES = ("full", "strip", "folded")
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
        if key in ("tools_inherit", "system_inherit"):
            if not isinstance(value, bool):
                raise Invalid(f"{key} is true or false")
            out[key] = value
            continue
        if key == "colors":
            if not isinstance(value, dict) or len(value) > 64:
                raise Invalid("colors is {who: hue}")
            for who, hue in value.items():
                if (not isinstance(who, str) or not 0 < len(who) <= 64 or isinstance(hue, bool)
                        or not isinstance(hue, int) or not 0 <= hue < 360):
                    raise Invalid("colors is {who: a hue from 0 to 359}")
            if value:
                out[key] = dict(value)
            continue
        if key == "faces":
            if not isinstance(value, dict) or len(value) > 64:
                raise Invalid("faces is {who: a picture in this chat}")
            for who, name in value.items():
                if (not isinstance(who, str) or not 0 < len(who) <= 64 or not isinstance(name, str)
                        or not 0 < len(name) <= 200 or "/" in name or "\\" in name or name.startswith(".")):
                    raise Invalid("faces is {who: the name of a picture in this chat's files}")
            if value:
                out[key] = dict(value)
            continue
        if key == "allowed_tools":
            from .permissions import clean_rules
            try:
                rules = clean_rules(value)
            except ValueError as ex:
                raise Invalid(str(ex)) from None
            if rules:
                out[key] = rules
            continue
        if isinstance(value, bool) or not isinstance(value, SETTINGS[key]):
            raise Invalid(f"{key} has the wrong type")
        if key == "permission_mode":
            from .permissions import WINDOW_MODES
            if value not in WINDOW_MODES:
                raise Invalid(f"permission_mode is one of {', '.join(m for m in WINDOW_MODES if m)}")
        if key in ("effort", "thinking_display"):
            from .permissions import EFFORTS, THINKING_DISPLAYS
            ok = EFFORTS if key == "effort" else THINKING_DISPLAYS
            if value not in ok:
                raise Invalid(f"{key} is one of {', '.join(v for v in ok if v)}")
        if key == "queue" and value not in QUEUE_MODES:
            raise Invalid(f"queue is one of {', '.join(QUEUE_MODES)}")
        if key == "interrupt" and value not in INTERRUPT_MODES:
            raise Invalid(f"interrupt is one of {', '.join(INTERRUPT_MODES)}")
        if key == "translate" and value not in TRANSLATE_MODES:
            raise Invalid(f"translate is one of {', '.join(TRANSLATE_MODES)}")
        if key in ("pictures", "my_pictures") and value not in PICTURE_MODES:
            raise Invalid(f"pictures is one of {', '.join(PICTURE_MODES)}")
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
        # An answer from a seat of a room (seren_symposium.room): whose it was.
        if m["role"] == "assistant" and isinstance(m.get("seat"), str) and m["seat"]:
            kept["seat"] = m["seat"][:32]
            # ...and its last piece, which is what the other seats are handed
            # (dropped if an edit left the answer without those words).
            if isinstance(m.get("final"), str) and m["final"] and m["final"] in m["content"]:
                kept["final"] = m["final"]
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
            "sources": list(s.get("sources") or []),
            # a room's seats, by name (seren_symposium.room)
            "seats": [x["name"] for x in s.get("seats") or []],
            # put away: out of its project's list, still searched, one click back
            "archived": s.get("archived")}


SNIP = 90          # characters of a message shown around a search hit


def _snippet(text: str, at: int, width: int) -> str:
    """`width` characters of `text` around position `at`, on one line, with
    an ellipsis where it was cut."""
    lo = max(0, at - width // 3)
    hi = min(len(text), lo + width)
    lo = max(0, hi - width)
    piece = " ".join(text[lo:hi].split())
    return ("…" if lo > 0 else "") + piece + ("…" if hi < len(text) else "")


def _terms(query: str) -> list[str]:
    return [w for w in query.lower().split() if w][:12]


def _anchor(low: str, terms: list[str]) -> int:
    """Where to cut a hit's snippet: at the words as typed, together, when
    the message has them so; else at the first word's earliest place that
    has the next word within a snippet of it; else just the first word.
    ("reading glasses" found "Symposium reading it" first, three paragraphs
    from the glasses.)"""
    phrase = " ".join(terms)
    at = low.find(phrase)
    if at >= 0 or len(terms) == 1:
        return at if at >= 0 else low.find(terms[0])
    pos = low.find(terms[0])
    while pos >= 0:
        if terms[1] in low[max(0, pos - SNIP // 2):pos + SNIP]:
            return pos
        pos = low.find(terms[0], pos + 1)
    return low.find(terms[0])


def search_session(s: dict[str, Any], terms: list[str], per_chat: int = 3) -> Optional[dict[str, Any]]:
    """Where in chat `s` every one of `terms` turns up: in its title, or all
    in one message. None when nowhere. Messages newest first, `per_chat` of
    them shown, `hits` counting them all."""
    title = s.get("title") or ""
    title_hit = all(w in title.lower() for w in terms)
    found = []
    msgs = s.get("messages") or []
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        text = m.get("content") or ""
        low = text.lower()            # lower, not casefold: it keeps the length, so positions still line up
        if not all(w in low for w in terms):
            continue
        if len(found) < per_chat:
            found.append({"index": i, "role": m.get("role"), "seat": m.get("seat"), "at": m.get("at"),
                          "snippet": _snippet(text, _anchor(low, terms), SNIP)})
        else:
            found.append(None)            # counted, not shown
    if not title_hit and not found:
        return None
    return {**summary(s), "title_hit": title_hit, "hits": len(found),
            "matches": [f for f in found if f is not None]}


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        # The files this Symposium put in each chat's folder itself (add_file),
        # by stored name: so a file that turns up during a turn is only taken
        # for the answer's when nothing here put it there (adopt).
        self._put: dict[str, set[str]] = {}

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
        self._put.setdefault(sid, set()).add(name)
        return file_entry(name, orig, mime, len(data))

    def listing(self, sid: str) -> set[str]:
        d = self.files_dir(sid)
        return {p.name for p in d.iterdir()} if d is not None and d.is_dir() else set()

    def adopt(self, sid: str, before: set[str]) -> list[tuple[str, str]]:
        """Files that appeared in the session's folder since `before` (the
        answer saved them there), renamed into stored names: [(old, new)].

        Not the ones Symposium put there itself meanwhile (add_file): what you
        attach while an answer is coming is uploaded at once, to wait its turn
        with your message, and "new in the folder" took it for the answer's.
        18 Nov 2026, the first tangle on 5.7.0 - Chad: "an interruption with
        an image gives it to the next answerer"."""
        d = self.files_dir(sid)
        if d is None or not d.is_dir():
            return []
        ours = self._put.get(sid, ())
        out = []
        for p in sorted(d.iterdir()):
            if p.name in before or p.name in ours or not p.is_file():
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

    def search(self, query: str, limit: int = 40, per_chat: int = 3) -> list[dict[str, Any]]:
        """Every chat here - archived ones too - where `query` turns up: its
        words all in the title, or all in one message (any order, any case).
        Title hits first, then the most recently spoken in. Read straight off
        the files: at a few hundred chats that is still nothing."""
        terms = _terms(query if isinstance(query, str) else "")
        if not terms or not self.root.is_dir():
            return []
        out = []
        for path in self.root.glob("*.json"):
            s = self.get(path.stem)
            if s is None:
                continue
            hit = search_session(s, terms, per_chat)
            if hit is not None:
                out.append(hit)
        out.sort(key=lambda h: (not h["title_hit"], -h["updated"]))
        return out[:limit]

    def search_one(self, sid: str, query: str, most: int = 2000) -> Optional[dict[str, Any]]:
        """Every message of one chat where `query` turns up, newest first
        (the sidebar's "and N more in this chat", opened: 23 Nov 2026, Chad
        looking for something said many messages back). None when it is not
        there."""
        terms = _terms(query if isinstance(query, str) else "")
        s = self.get(sid) if terms else None
        return search_session(s, terms, most) if s is not None else None

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

    def claude_ids(self) -> frozenset[str]:
        """Every CLI conversation a chat here is in - already picked up, or
        begun here - so the list of a terminal's leaves them out."""
        out = set()
        for summ in self.list():
            s = self.get(summ["id"]) or {}
            for holder in [s, *(s.get("seats") or [])]:      # a chat's own, and each seat's
                if (holder.get("claude") or {}).get("session"):
                    out.add(holder["claude"]["session"])
        return frozenset(out)

    def pick_up(self, conversation: dict[str, Any], folder: str = "", fork: bool = False) -> dict[str, Any]:
        """A chat that IS a CLI conversation already under way (a terminal's,
        read by terminal.transcript): its words as the messages, its folder
        as the chat's cwd, and Claude's next turn resuming it - nothing
        handed over, since that conversation has seen it all."""
        settings = {"voice": "claude", **({"cwd": conversation["cwd"]} if conversation.get("cwd") else {})}
        s = self.create(folder=folder, title=conversation.get("title") or "", settings=settings,
                        messages=conversation.get("messages") or [])
        s["claude"] = {"session": conversation["id"], "seen": len(s["messages"]),
                       **({"fork": True} if fork else {})}
        self._write(s)
        return s

    def update(self, sid: str, changes: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Rename (title), move (folder), change settings, or put it away
        (archived: true/false). Settings are merged: a key sent as null drops
        back to the folder's value. Archiving alone leaves `updated` as it
        was: putting a chat away is not something said in it, and it comes
        back where it sat."""
        s = self.get(sid)
        if s is None:
            return None
        for key in changes:
            if key not in ("title", "folder", "settings", "archived"):
                raise Invalid(f"{key} cannot be changed")
        if "archived" in changes:
            if not isinstance(changes["archived"], bool):
                raise Invalid("archived is true or false")
            if changes["archived"]:
                s["archived"] = s.get("archived") or _now()
            else:
                s.pop("archived", None)
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
        if set(changes) - {"archived"}:
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

    def set_face(self, sid: str, who: str, name: Optional[str]) -> Optional[dict[str, Any]]:
        """`who` wears the picture `name` (one of this chat's files) in this
        chat; None takes it off, back to what they wear everywhere. A chat's
        `updated` stays: a face put on is not something said in it."""
        s = self.get(sid)
        if s is None:
            return None
        faces = dict(s["settings"].get("faces") or {})
        if name:
            if self.file_path(sid, name) is None:
                raise Invalid("no such picture in this chat")
            faces[who] = name
        else:
            faces.pop(who, None)
        s["settings"] = clean_settings({**s["settings"], "faces": faces})
        self._write(s)
        return s

    def remove(self, sid: str) -> Optional[dict[str, Any]]:
        """Take a chat OUT of Symposium without destroying anything (Chad,
        17 Nov 2026: a terminal's conversation loaded in by accident should
        be removable "without deleting"). Its file and its attachments are
        set aside under removed/, where nothing here looks; a Claude
        conversation it carried was never Symposium's to delete and is free
        again - to be picked up once more, or carried on in a terminal.
        The chat as it was, or None when there is no such chat. Moving the
        two back puts it back."""
        path = self._path(sid)
        s = self.get(sid)
        if s is None or path is None:
            return None
        aside = self.root / "removed"
        aside.mkdir(parents=True, exist_ok=True)
        files = self.files_dir(sid)
        for src in (path, files):
            if not src.exists():
                continue
            dst = aside / src.name
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)       # a copy set aside before, then put back: this is newer
            elif dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))
        return s

    @staticmethod
    def _forget_claude_past(s: dict[str, Any], index: int) -> None:
        """A change at message `index`: if Claude's conversation saw that far,
        it no longer matches the chat, and the next turn starts a new one -
        for each seat of a room the same, one by one."""
        if (s.get("claude") or {}).get("seen", 0) > index:
            s.pop("claude", None)
        for seat in s.get("seats") or []:
            if (seat.get("claude") or {}).get("seen", 0) > index:
                seat.pop("claude", None)

    # ── A room's seats (seren_symposium.room) ───────────────────────────

    def add_seat(self, sid: str, name: str, cwd: str = "", conversation: Optional[str] = None,
                 fork: bool = False, host: str = "host") -> Optional[dict[str, Any]]:
        """A seat at this chat's table: a name, the folder it works in, and -
        picked up from a terminal - the CLI conversation it carries on. That
        conversation has heard nothing of this room yet (seen 0), so its
        first turn here is handed the room so far.

        The FIRST seat added to a chat that already has a Claude in it makes
        that Claude a seat too, named `host`, keeping its conversation and
        all it has seen: inviting someone doesn't throw out who was there."""
        from . import room
        s = self.get(sid)
        if s is None:
            return None
        if not room.valid_name(name):
            raise Invalid("a seat's name is 1-32 letters, digits, _ . - or ·")
        if not isinstance(cwd, str):
            raise Invalid("cwd is a string")
        if not s.get("seats") and s.get("claude"):
            if not room.valid_name(host) or host == name:
                raise Invalid(f"the chat's own Claude takes a seat too: name it something besides {name}")
            s["seats"] = [{"name": host, "cwd": s["settings"].get("cwd") or "", "hue": room.next_hue([]),
                           "claude": s.pop("claude")}]
        seats = s.setdefault("seats", [])
        if room.seat(s, name) is not None:
            raise Invalid(f"there is already a seat called {name}")
        seat: dict[str, Any] = {"name": name, "cwd": cwd, "hue": room.next_hue(seats)}
        # Someone who sat here before, invited back by name, is herself again:
        # the conversation she left with, what she had heard by then (so she
        # is handed only what she missed), her folder and her colour.
        back = next((g for g in s.get("left") or [] if g.get("name") == name), None)
        if back is not None:
            s["left"] = [g for g in s["left"] if g.get("name") != name]
            if not s["left"]:
                s.pop("left")
        if conversation:
            seat["claude"] = {"session": conversation, "seen": 0, **({"fork": True} if fork else {})}
        elif back is not None:
            seat["cwd"] = cwd or back.get("cwd") or ""
            if isinstance(back.get("claude"), dict):
                seat["claude"] = dict(back["claude"])
            if isinstance(back.get("hue"), int) and back["hue"] not in {x.get("hue") for x in seats}:
                seat["hue"] = back["hue"]
        seats.append(seat)
        s["updated"] = _now()
        self._write(s)
        return s

    def drop_seat(self, sid: str, name: str) -> Optional[dict[str, Any]]:
        """The seat leaves; what it said stays in the chat, under its name,
        with its files and its face.

        And she is not forgotten. The chat's only record of WHICH conversation
        a seat is was on the seat itself, so removing her threw that away: the
        transcript still on disk, nothing pointing at it, and the same name
        invited again was a stranger handed the whole chat (found 19 Nov 2026,
        before Chad's "what happens if I remove a wren" test). Now who left is
        kept (`left`: name, folder, colour, conversation, what she had heard),
        and add_seat brings her back by name. The LAST seat to leave hands her
        conversation to the chat itself - the mirror of the first invitation,
        which made the chat's own Claude a seat - so a room that empties is a
        plain chat with someone still in it."""
        s = self.get(sid)
        if s is None:
            return None
        gone = next((x for x in s.get("seats") or [] if x.get("name") == name), None)
        if gone is None:
            raise Invalid(f"no seat called {name}")
        s["seats"] = [x for x in s["seats"] if x.get("name") != name]
        s["left"] = [g for g in s.get("left") or [] if g.get("name") != name][-15:] + [
            {k: gone[k] for k in ("name", "cwd", "hue", "claude") if k in gone}]
        if not s["seats"]:
            s.pop("seats")
            if isinstance(gone.get("claude"), dict) and gone["claude"].get("session") and not s.get("claude"):
                s["claude"] = dict(gone["claude"])
                if gone.get("cwd") and not s["settings"].get("cwd"):
                    s["settings"]["cwd"] = gone["cwd"]
        s["updated"] = _now()
        self._write(s)
        return s

    def order_seats(self, sid: str, order: Any) -> Optional[dict[str, Any]]:
        """Who sits where: `order` is every seat's name, once, in the order
        the table goes round."""
        s = self.get(sid)
        if s is None:
            return None
        seats = s.get("seats") or []
        names = [x["name"] for x in seats]
        if not isinstance(order, list) or sorted(map(str, order)) != sorted(names):
            raise Invalid(f"order is every seat's name, once (the seats: {', '.join(names) or 'none'})")
        s["seats"] = [next(x for x in seats if x["name"] == n) for n in order]
        s["updated"] = _now()
        self._write(s)
        return s

    def change_seat(self, sid: str, name: str, changes: Any) -> Optional[dict[str, Any]]:
        """{name?, hue?}: call a seat something else, or give it another
        colour. A new name goes on what it already said too, so the chat
        reads as one seat's and the others are handed it under one name;
        its conversation and what it has heard are untouched."""
        from . import room
        s = self.get(sid)
        if s is None:
            return None
        seat = room.seat(s, name)
        if seat is None:
            raise Invalid(f"no seat called {name}")
        if not isinstance(changes, dict) or not changes or set(changes) - {"name", "hue"}:
            raise Invalid("a seat's name or hue can be changed")
        if "hue" in changes:
            hue = changes["hue"]
            if isinstance(hue, bool) or not isinstance(hue, int) or not 0 <= hue < 360:
                raise Invalid("hue is a whole number from 0 to 359")
            seat["hue"] = hue
        new = changes.get("name", name)
        if new != name:
            if not room.valid_name(new):
                raise Invalid("a seat's name is 1-32 letters, digits, _ . - or ·")
            if room.seat(s, new) is not None:
                raise Invalid(f"there is already a seat called {new}")
            seat["name"] = new
            for m in s["messages"]:
                if m.get("seat") == name:
                    m["seat"] = new
        s["updated"] = _now()
        self._write(s)
        return s

    def record_seat(self, sid: str, name: str, prompt: Optional[str], answer: Optional[str],
                    meta: Optional[dict[str, Any]], claude_session: str,
                    files: Optional[list[dict[str, Any]]] = None,
                    answer_files: Optional[list[dict[str, Any]]] = None,
                    claude_init: Optional[dict[str, Any]] = None,
                    final: Optional[str] = None, heard_it: bool = True,
                    stopped: bool = False) -> Optional[dict[str, Any]]:
        """One seat's turn in a round. `prompt`: the person's message, which
        the FIRST seat to come back writes in (the round's others find it
        there). `answer` None: the seat passed - nothing of its goes in, but
        it has heard the round. Either way the seat has now seen it all.
        `final`: the answer's last piece, when it was said in several - what
        the other seats are handed of it (room.heard). `heard_it` False: a
        turn stopped before its CLI had even started, so the seat's
        conversation has seen none of this and is handed it next time."""
        s = self.get(sid)
        if s is None:
            return None
        from . import room
        seat = room.seat(s, name)
        if seat is None:
            raise Invalid(f"no seat called {name}")
        now = _now()
        if prompt is not None:
            asked: dict[str, Any] = {"role": "user", "content": prompt, "at": now}
            if files:
                asked["files"] = files
            s["messages"].append(asked)
            s.pop("archived", None)           # talked in again: it is back in its project
            if not s["title"]:
                s["title"] = " ".join(prompt.split())[:48]
        if answer is not None:
            said: dict[str, Any] = {"role": "assistant", "content": answer, "at": now, "seat": name,
                                    "meta": meta or {}}
            if answer_files:
                said["files"] = answer_files
            if final and final != answer:
                said["final"] = final
            s["messages"].append(said)
        if heard_it:
            # `stopped`: counted as having seen everything but its own
            # cut-short answer, which is handed back to it (see record).
            seat["claude"] = {"session": claude_session,
                              "seen": len(s["messages"]) - (1 if stopped and answer is not None else 0)}
        if claude_init:
            seat["claude_init"] = {**claude_init, "at": now}
        s["updated"] = now
        self._write(s)
        return s

    def record_woken(self, sid: str, seat: Optional[str], answer: str, meta: dict[str, Any], claude_session: str,
                     answer_files: Optional[list[dict[str, Any]]] = None,
                     final: Optional[str] = None) -> Optional[dict[str, Any]]:
        """What a conversation said when it came back ON ITS OWN - a task it
        had left running ended (claude_voice, "A process that outlives its
        answer"). Nobody asked, so there is no message of the person's with
        it: only the answer, under the seat's name (or the chat's one voice).

        It has seen its own words. Whether it has seen the rest: only if it
        had heard everything in the chat up to now, in which case it still
        has; otherwise what it missed is still handed to it next time, its
        own late answer among it."""
        s = self.get(sid)
        if s is None:
            return None
        from . import room
        now = _now()
        holder = room.seat(s, seat) if seat else s
        known = holder.get("claude") if isinstance(holder, dict) else None
        caught_up = (isinstance(known, dict) and known.get("session") == claude_session
                     and known.get("seen") == len(s["messages"]))
        said: dict[str, Any] = {"role": "assistant", "content": answer, "at": now, "meta": meta or {}}
        if seat:
            said["seat"] = seat
        if answer_files:
            said["files"] = answer_files
        if final and final != answer:
            said["final"] = final
        s["messages"].append(said)
        s.pop("archived", None)               # it spoke up: back in its project, to be seen
        if caught_up:
            known["seen"] = len(s["messages"])
        s["updated"] = now
        self._write(s)
        return s

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
               source: Optional[str] = None, by: str = "",
               claude_init: Optional[dict[str, Any]] = None,
               stopped: bool = False) -> Optional[dict[str, Any]]:
        """A turn that came back. keep cuts the thread first (a retry
        replaces the exchange it retried). A session with no title is named
        after its first prompt. claude_session: Claude answered, in that CLI
        session, and has now seen the whole thread. files: what the prompt
        carried; answer_files: what the answer brought back. source: the
        prompt was not the person's but fed in (the game table: "table"),
        shown under `by`; the chat remembers every source that spoke in it.
        `stopped`: the answer was cut short, and its conversation is counted
        as having seen the prompt but NOT that answer - a CLI killed mid-turn
        may or may not have written its own words down - so the next turn
        hands them back."""
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
        s.pop("archived", None)               # talked in again: it is back in its project
        if not s["title"]:
            s["title"] = " ".join(prompt.split())[:48]
        if claude_session:
            s["claude"] = {"session": claude_session, "seen": len(s["messages"]) - (1 if stopped else 0)}
        if claude_init:
            s["claude_init"] = {**claude_init, "at": now}
        s["updated"] = now
        self._write(s)
        return s
