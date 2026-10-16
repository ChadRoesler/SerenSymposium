"""
seren_symposium.attachments
════════════════════════════════════════════════════════════════════════

Files in a chat, both ways. Stored beside the session (sessions.py); this is
what each voice is told about them, and how an answer brings one back.

GOING IN:
    Claude opens files itself, so it is given each file's path (and the CLI
    is pointed at the chat's folder with --add-dir). A local model cannot
    open anything: text files are put into the prompt, up to
    files.inline_max_chars; an image or other binary is named, so the model
    at least knows something was there. (A model that can see is Lodestar's
    business, and a later one.)

COMING BACK:
    An answer shows an image as Markdown. One from the web,
    ![caption](https://...), is fetched and saved into the chat - so the chat
    still shows it when the link has rotted - and the answer is rewritten to
    point at the saved copy, files/<name>. A file the answer SAVED into the
    chat's folder (Claude, when allowed to write there) is adopted the same
    way. Only real images are kept: the bytes are checked, not the header.

SERVED BACK TO THE SHELL:
    From Symposium's own origin, which is the origin that can drive the API -
    so nothing comes back as a page. Pictures are served as pictures, text as
    text/plain, everything else as a download, all with nosniff and a sandbox
    CSP: an uploaded .html or .svg is never a script running as us.
"""
from __future__ import annotations

import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .sessions import SessionStore, file_entry, safe_name

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".ts",
    ".tsx", ".jsx", ".py", ".pyi", ".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".bat", ".cmd",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".lua", ".sql",
    ".r", ".swift", ".m", ".pl", ".svg", ".gitignore", ".dockerfile", ".tf", ".hcl", ".srt", ".vtt",
}
# Shown inline in the shell: the formats every webview draws. SVG can carry
# script, which an <img> never runs, and which the sandbox CSP every file is
# served with stops if one is opened on its own.
INLINE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"}
_MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png", ".png"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
          (b"GIF87a", "image/gif", ".gif"), (b"GIF89a", "image/gif", ".gif"),
          (b"BM", "image/bmp", ".bmp")]
# Tests put a MockTransport here; nothing else should.
FETCH_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_MD_IMAGE = re.compile(r'!\[([^\]]*)\]\(\s*<?(https?://[^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')


def sniff_image(data: bytes) -> Optional[tuple[str, str]]:
    """(mime, extension) when the bytes are a picture the shell can show."""
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def head(path: Path, n: int = 16) -> bytes:
    """The first bytes of a file: enough to tell what it is."""
    with open(path, "rb") as f:
        return f.read(n)


def guess_type(name: str, data: bytes = b"") -> str:
    if data and (seen := sniff_image(data)):
        return seen[0]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def is_text(entry: dict[str, Any]) -> bool:
    mime = entry.get("type") or ""
    return mime.startswith("text/") or Path(entry.get("orig") or entry["name"]).suffix.lower() in TEXT_EXT \
        or mime in ("application/json", "application/xml", "application/x-yaml")


def size_label(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def for_claude(entries: list[dict[str, Any]], folder: Path) -> str:
    """The lines in front of a Claude prompt that say what came with it."""
    if not entries:
        return ""
    lines = ["Attached to this message:"]
    for e in entries:
        what = f"a screen share of {e.get('source') or 'the desktop'}" if e.get("screen") else (e.get("orig") or e["name"])
        lines.append(f"- {folder / e['name']}  ({what}, {e.get('type')}, "
                     f"{size_label(int(e.get('size') or 0))})")
    return "\n".join(lines)


def for_local(entries: list[dict[str, Any]], store: SessionStore, sid: str, budget: int) -> str:
    """What a local model is given for a message's files: the text of the
    text ones, while `budget` lasts, and a line naming the rest."""
    parts = []
    for e in entries:
        label = e.get("orig") or e["name"]
        path = store.file_path(sid, e["name"])
        if path is not None and is_text(e) and budget > 0:
            text = path.read_bytes().decode("utf-8", "replace")
            cut = len(text) > budget
            text = text[:budget]
            budget -= len(text)
            parts.append(f"--- attached: {label} ---\n{text}\n--- end of {label}"
                         + (" (cut short: too long to send whole) ---" if cut else " ---"))
        else:
            kind = ("a screen share" if e.get("screen")
                    else "an image" if (e.get("type") or "").startswith("image/") else "a file")
            parts.append(f"[attached {kind}: {label} - this model is not shown its contents]")
    return "\n\n".join(parts)


def with_files(prompt: str, note: str) -> str:
    return f"{prompt}\n\n{note}" if note else prompt


def serve_headers(entry_type: str) -> tuple[str, dict[str, str]]:
    """(media type, headers) a stored file goes back to the shell with."""
    headers = {"X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
               "Cache-Control": "private, max-age=86400"}
    if entry_type in INLINE_IMAGES:
        return entry_type, headers
    if entry_type.startswith("text/") or entry_type in ("application/json",):
        return "text/plain; charset=utf-8", headers
    return "application/octet-stream", {**headers, "Content-Disposition": "attachment"}


async def capture_images(answer: str, store: SessionStore, sid: str, max_bytes: int,
                         client: Optional[httpx.AsyncClient] = None) -> tuple[str, list[dict[str, Any]]]:
    """Images the answer shows from the web, saved into the chat; the answer
    rewritten to point at the copies. One that cannot be fetched, is too big
    or is not a picture stays a plain link."""
    found = list(dict.fromkeys(url for _, url in _MD_IMAGE.findall(answer)))
    if not found:
        return answer, []
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True, transport=FETCH_TRANSPORT,
                                         headers={"User-Agent": "SerenSymposium (+https://github.com/ChadRoesler/SerenSymposium)"})
    saved: dict[str, dict[str, Any]] = {}
    try:
        for url in found[:8]:
            try:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        continue
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data += chunk
                        if len(data) > max_bytes:
                            break
                if len(data) > max_bytes or not (kind := sniff_image(data)):
                    continue
            except httpx.HTTPError:
                continue
            orig = Path(httpx.URL(url).path).name or "image"
            if not orig.lower().endswith(kind[1]):
                orig = f"{Path(orig).stem or 'image'}{kind[1]}"
            entry = store.add_file(sid, orig, bytes(data), kind[0])
            if entry is not None:
                saved[url] = entry
    finally:
        if own:
            await client.aclose()
    if not saved:
        return answer, []

    def point(m: re.Match) -> str:
        e = saved.get(m.group(2))
        return f"![{m.group(1)}](files/{e['name']})" if e else m.group(0)
    return _MD_IMAGE.sub(point, answer), list(saved.values())


_SAVED_NAME = re.compile(r"[^\"'\\/\s]+\.(?:png|jpe?g|webp|gif)", re.I)


def keep_tool_images(images: list[dict[str, Any]], store: SessionStore, sid: str, max_bytes: int,
                     steps: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """Pictures a tool sent back as image content (make_image, drawn on
    another box), kept in the chat as the answer's files. Each is named for
    the file the tool says it saved, when it says one, so the copy here and
    the one in the gallery match; else picture-<time>-<n>."""
    said: dict[Any, list[str]] = {}
    for s in steps or []:
        if s.get("kind") == "tool" and s.get("result"):
            said[s.get("id")] = _SAVED_NAME.findall(s["result"])
    used: dict[Any, int] = {}
    out = []
    for i, im in enumerate(images):
        data = im.get("data") or b""
        kind = sniff_image(data)
        if not kind or len(data) > max_bytes:
            continue
        n = used.get(im.get("tool_use_id"), 0)
        used[im.get("tool_use_id")] = n + 1
        names = said.get(im.get("tool_use_id")) or []
        name = names[n] if n < len(names) else time.strftime(f"picture-%H%M%S-{i + 1}{kind[1]}")
        if not name.lower().endswith(kind[1]):
            name = f"{Path(name).stem}{kind[1]}"
        entry = store.add_file(sid, name, data, kind[0])
        if entry is not None:
            out.append(entry)
    return out


def adopt_saved(answer: str, store: SessionStore, sid: str, before: set[str]) -> tuple[str, list[dict[str, Any]]]:
    """Files the answer wrote into the chat's folder during the turn, kept,
    and every mention of their old names pointed at the stored ones."""
    entries = []
    folder = store.files_dir(sid)
    for old, new in store.adopt(sid, before):
        path = store.file_path(sid, new)
        if path is None:
            continue
        entries.append(file_entry(new, old, guess_type(old, head(path)), path.stat().st_size))
        if old != new:
            for ref in (str(folder / old), str(folder / old).replace("\\", "/"), f"files/{old}"):
                answer = answer.replace(ref, f"files/{new}")
        answer = answer.replace(str(folder / new), f"files/{new}").replace(
            str(folder / new).replace("\\", "/"), f"files/{new}")
    return answer, entries


__all__ = ["for_claude", "for_local", "with_files", "capture_images", "adopt_saved", "keep_tool_images", "serve_headers",
           "guess_type", "head", "sniff_image", "is_text", "safe_name"]
