"""
seren_symposium.avatars
════════════════════════════════════════════════════════════════════════

The face a voice wears in the window: a picture by a seat's name, and one
`default` for everyone who has not picked.

WHY THEY ARE NOT THE KITCHEN'S MAGNETS (Chad, 17 Nov 2026):
    "magnets are its own thing and serve a direct purpose, but like avatars
    should be more open and free. like if you wanted to have one thats of
    you, or of a robot, or of like a really cool space ship, ridden by a
    possum, powered by a hamster on a wheel, or wanted to grab one from
    online you can. its an area where being open to creativity matters to
    me, even if theres a default avatar for the main time."

    A magnet says who holds a repo. An avatar says nothing at all; it is
    worn because whoever wears it liked it. So it is not looked up anywhere:
    it is SET, by whoever is wearing it or by the person, from any picture.

HOW ONE IS SET:
    By the one wearing it, in an answer: an image whose caption is `avatar`,

        ![avatar](files/possum-spaceship.svg)     a file it saved into the chat
        ![avatar](https://example.org/robot.png)  one from the web (fetched and
                                                  kept, as any image shown is)
        ![avatar](124218-42-a-wren.png)           one a tool drew this turn, by
                                                  the file name the tool gave

    A seat sets its own; the Claude of a chat with no seats sets `default`.
    The last one in the answer wins. It is still an image in the answer, so
    the chat shows the new face where it was put on.

TWO SCOPES (Chad, 17 Nov 2026: "we can set a default in the settings main
tab, but the per chat avatars would be good as well, so that if you decide
you want a specific avatar for cheese tag, or whatever, you have one"):
    this chat    `![avatar](...)`. Kept in the chat's own settings (`faces`:
                 who -> one of its files), so it goes where the chat goes and
                 is gone with it. The face a chat sees is its own first.
    every chat   `![avatar everywhere](...)`. Kept here, by name; what is
                 worn wherever a chat has not said otherwise.
    The person sets either from the same sheet (this chat / every chat), and
    the every-chat ones from the Settings tab too.

    By the person, in the window (⚙ -> seats): a file, a link, or any picture
    already in the chat - which is how one drawn in the picture panel (through
    Lodestar, on the image node) gets worn.

WHERE THEY LIVE:
    Beside the chats (<sessions>/../avatars): a small index and the pictures.
    By NAME, not by chat: the seat called kitchen wears the same face in
    every room it sits in. No network; the one fetch (a link) is the same
    fetch an answer's web image already gets.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import attachments, room

DEFAULT = "default"
MAX_BYTES = 8 * 1024 * 1024
_REF = re.compile(r'!\[\s*(?:my\s+)?avatar(\s+everywhere)?\s*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)', re.I)
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
        "image/bmp": ".bmp", "image/svg+xml": ".svg"}


class BadAvatar(ValueError):
    pass


def valid(name: Any) -> bool:
    return name == DEFAULT or room.valid_name(name)


def root(cfg) -> Path:
    return cfg.sessions_dir().parent / "avatars"


def wanted(answer: str, files: list[dict[str, Any]]) -> Optional[tuple[str, str, bool]]:
    """The picture an answer puts on as its avatar: (what it wrote in the
    image's brackets, the chat file that is, whether it is for every chat -
    captioned `avatar everywhere` - or only this one). `files` are the
    answer's own - saved, fetched, or sent back by a tool this turn - for one
    named by the file name a tool gave rather than by where the chat keeps it."""
    refs = _REF.findall(answer or "")
    if not refs:
        return None
    everywhere, ref = bool(refs[-1][0]), refs[-1][1]
    if ref.startswith("files/"):
        return ref, ref[len("files/"):], everywhere
    base = ref.replace("\\", "/").rsplit("/", 1)[-1]
    for f in files:
        if base and base in (f.get("orig"), f.get("name")):
            return ref, f["name"], everywhere
    # The same picture under another extension. make_image says it saved
    # `…-my-avatar.png` and sends back a JPEG of it, which the chat keeps as
    # `…-my-avatar.jpg`: the name a seat was given never matched the file the
    # chat had (18 Nov 2026, the first tangle on 5.7.0 - three seats drew a
    # face, wrote the name they were told, and Chad had to put each on by hand).
    stem = base.rsplit(".", 1)[0] if "." in base else base
    for f in files:
        if stem and stem in {str(n).rsplit(".", 1)[0] for n in (f.get("orig"), f.get("name")) if n}:
            return ref, f["name"], everywhere
    return None


class Avatars:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _index(self) -> dict[str, dict[str, Any]]:
        try:
            got = json.loads((self.folder / "avatars.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in got.items() if isinstance(v, dict) and isinstance(v.get("file"), str)} \
            if isinstance(got, dict) else {}

    def _write(self, index: dict[str, dict[str, Any]]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / f"avatars.json.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(json.dumps(index, indent=1), encoding="utf-8")
        os.replace(tmp, self.folder / "avatars.json")

    def all(self) -> dict[str, dict[str, Any]]:
        """{name: {type, at}} - who wears one, and since when (the window
        asks for a face by name and `at`, so a new one is not a cached old one)."""
        return {k: {"type": v.get("type") or "", "at": v.get("at") or 0} for k, v in self._index().items()
                if (self.folder / v["file"]).is_file()}

    def path(self, name: str) -> Optional[tuple[Path, str]]:
        entry = self._index().get(name)
        if entry is None:
            return None
        p = self.folder / Path(entry["file"]).name
        return (p, entry.get("type") or "") if p.is_file() else None

    def wear(self, name: str, data: bytes, mime: str = "") -> dict[str, Any]:
        """`name` wears this picture from now on. What the bytes are decides
        the type; an SVG cannot be told by its first bytes, so it is taken
        on the word of whoever stored it and checked for looking like one."""
        if not valid(name):
            raise BadAvatar("an avatar is worn by a seat's name, or default")
        if not data or len(data) > MAX_BYTES:
            raise BadAvatar(f"an avatar is a picture of at most {MAX_BYTES // (1024 * 1024)} MB")
        seen = attachments.sniff_image(data)
        kind = seen[0] if seen else (mime if mime == "image/svg+xml" and b"<svg" in data[:4096].lower() else "")
        if kind not in _EXT:
            raise BadAvatar("that is not a picture the window can show (png, jpeg, gif, webp, bmp, svg)")
        index = self._index()
        old = index.get(name)
        self.folder.mkdir(parents=True, exist_ok=True)
        file = f"{uuid.uuid4().hex[:12]}{_EXT[kind]}"
        (self.folder / file).write_bytes(data)
        index[name] = {"file": file, "type": kind, "at": round(time.time(), 3)}
        self._write(index)
        if old:
            (self.folder / Path(old["file"]).name).unlink(missing_ok=True)
        return self.all()

    def wear_file(self, name: str, path: Optional[Path]) -> dict[str, Any]:
        """The same, from a file already in a chat."""
        if path is None or not path.is_file():
            raise BadAvatar("no such picture in this chat")
        if path.stat().st_size > MAX_BYTES:
            raise BadAvatar(f"an avatar is a picture of at most {MAX_BYTES // (1024 * 1024)} MB")
        return self.wear(name, path.read_bytes(), attachments.guess_type(path.name))

    def drop(self, name: str) -> bool:
        index = self._index()
        old = index.pop(name, None)
        if old is None:
            return False
        self._write(index)
        (self.folder / Path(old["file"]).name).unlink(missing_ok=True)
        return True
