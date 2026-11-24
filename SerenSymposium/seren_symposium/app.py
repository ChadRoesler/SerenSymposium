"""
seren_symposium.app
════════════════════════════════════════════════════════════════════════

A LOOPBACK SHIM. Symposium serves its own UI on 127.0.0.1 and proxies chat to
Lodestar with the bearer attached - or, in a chat whose voice is "claude",
runs the claude CLI on this machine instead (seren_symposium.claude_voice).

WHY A LOCAL SERVER AND NOT JUST JS -> LODESTAR:
    Two reasons, both practical rather than architectural. The bearer stays in
    Python instead of being handed to a webview's JavaScript, and Lodestar
    never needs CORS opened for a desktop app. The cost is one loopback port.

WHAT IT DOES NOT DO:
    No inbound auth. It binds 127.0.0.1 only - anything reaching it is already
    on the machine. If you ever widen ui.host beyond loopback you have built a
    different, unauthenticated service; don't.

BUT LOOPBACK IS NOT A WALL AGAINST THE BROWSER. Any page open in any browser
on this machine can send a request to 127.0.0.1:7426, and /api/chat runs
Lodestar's tools with our bearer. So every request passes same_origin_only:
the Host must be a loopback name (a DNS-rebound evil.example pointing at
127.0.0.1 still says evil.example), an Origin, when sent, must be us, and a
POST or PATCH must be application/json - which no cross-site form can send
without a CORS preflight, and we answer no preflight.

Serves:
    GET    /                      the shell (chat + tabs pointing at sibling viewers)
    GET    /api/config            what the UI needs to render: viewers, folders, version
    POST   /api/chat              proxied to Lodestar, bearer injected (or answered by claude)
    POST   /api/chat/stream       the same, streamed (Lodestar's NDJSON, or claude's in its shape)
    GET    /api/models            the models Lodestar's llama node has
    GET    /api/sessions          every session, newest first (POST: new, or import)
    GET    /api/sessions/{id}     one session, whole - also the export (PATCH, DELETE)
    GET    /api/sessions/{id}/live          the turn running in it now, replayed then followed (LiveTurn)
    POST   /api/sessions/{id}/files         attach a file (JSON, base64) - GET .../files/{name} reads it
    GET    /api/sessions/{id}/files/{name}/settings   what made a picture drawn here (pictures.made_of)
    PATCH  /api/prefs             your name, the claude voice's settings, confirm - kept beside the chats
    GET    /api/claude            the claude voice's settings and the carabiner clipped on this box
    GET    /api/claude/sessions   the CLI's own conversations on this box (a terminal's), to pick up
    POST   /api/claude/sessions/pickup      a new chat that IS one of them (seren_symposium.terminal)
    POST   /api/permit            the claude voice asking may-I (from seren_symposium.permit)
    POST   /api/permit/answer     your answer: once, always, deny
    POST   /api/projects          a new project (PATCH: change or rename one; .../remove)
    POST   /api/sessions/{id}/rewind        keep the first `to` messages
    POST   /api/sessions/{id}/fork          a new chat from the first `at` messages
    DELETE /api/sessions/{id}/messages/{i}  take one message out
    GET    /api/screen/sources    the displays and windows that can be shared
    POST   /api/screen            take a still of one (GET /api/screen/latest reads it)
    GET    /vendor/...            the Markdown renderer and highlighter, shipped with us
    GET    /health                liveness, and whether Lodestar is reachable
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from seren_meninges import get_version
from seren_meninges.viewer import render_from_dir

from . import __version__ as _fallback_version
from . import attachments, avatars, behind, preface, carabiner, claude_voice, ears, pictures, room, screen, stack, terminal
from . import permissions
from .config import FolderPreset, SymposiumConfig, load_config
from .prefs import Invalid as BadPref
from .prefs import Prefs
from .sessions import TRANSLATE_MODES, Invalid, SessionStore, file_entry, summary

APP_VERSION = get_version("seren-symposium", fallback=_fallback_version)
# Symposium: olive-sage, the wreath at the table - and the one empty stretch
# of the family's wheel (Probe's mint is the only other green).
ACCENT = "#a3b18a"
log = logging.getLogger("seren_symposium")

UI_DIR = Path(__file__).resolve().parent / "ui"
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _hostname(host_header: str) -> str:
    """"127.0.0.1:7426" -> "127.0.0.1", "[::1]:7426" -> "::1"."""
    try:
        return (urlparse(f"//{host_header}").hostname or "").lower()
    except ValueError:
        return ""


def refuse_cross_site(cfg: SymposiumConfig, method: str, headers) -> Optional[tuple[int, str]]:
    """(status, why) when a request may not come in; None when it may. See
    the module docstring for what each check stops."""
    host = headers.get("host", "")
    if not cfg.ui.allow_open_lan and _hostname(host) not in LOOPBACK_NAMES | {cfg.ui.host.lower()}:
        return 403, f"Host {host!r} is not this machine's loopback"
    origin = headers.get("origin")
    if origin is not None and origin.lower() != f"{urlparse(origin).scheme}://{host}".lower():
        return 403, f"Origin {origin!r} is not this shell"
    if method in ("POST", "PUT", "PATCH"):
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return 415, "send application/json"
    return None


def _trim_history(history: Any, max_chars: int, also: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Keep the newest whole messages that fit in max_chars, oldest dropped first.

    Whole messages, never partial ones: half a turn reads as the model having
    been told something it was not. Dropping from the oldest end is what makes
    a long session degrade into a short-memory one instead of failing outright.
    Only role and content go on: a session's timestamps and receipts are ours
    (and `also`: a room's handover needs who said each one - seat, by).
    """
    if not history or not isinstance(history, list):
        return []
    kept: list[dict[str, Any]] = []
    budget = max_chars
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        cost = len(str(message.get("content", "")))
        if cost > budget:
            break
        budget -= cost
        kept.append({"role": message.get("role"), "content": message.get("content", ""),
                     **{k: message[k] for k in also if message.get(k)}})
    kept.reverse()
    return kept


def to_lodestar_payload(cfg: SymposiumConfig, body: dict[str, Any]) -> dict[str, Any]:
    """Symposium's request shape -> Lodestar's.

    Lodestar wants a flat {prompt, system_prompt, history, model_override,
    temperature, max_tokens, repeat_penalty}; it has never heard of a folder.
    Resolving the preset here rather than in the shell's JavaScript is what
    makes the layer precedence - folder default, then explicit per-session
    override - assertable in pytest.

    Keys are omitted rather than sent empty: Lodestar tests `req.get(...)` for
    truthiness, so a blank system_prompt and an absent one mean the same thing
    to it, and omitting keeps the wire readable when debugging.
    """
    preset = cfg.folder(body["folder"]) if body.get("folder") else None
    out: dict[str, Any] = {"prompt": body.get("prompt", "")}

    if history := _trim_history(body.get("history"), cfg.lodestar.history_max_chars):
        out["history"] = history
    # The prompts add up: the folder's, then the chat's (unless the chat says
    # system_inherit false), seren_symposium.stack.own_prompt.
    if system_prompt := stack.own_prompt(preset, body):
        out["system_prompt"] = system_prompt
    if model := (body.get("model_override") or (preset.model if preset else "")):
        out["model_override"] = model

    # Numbers by `is None`, never truthiness: temperature 0.0 is real.
    for key in ("temperature", "max_tokens", "repeat_penalty"):
        value = body.get(key)
        if value is None and preset is not None:
            value = getattr(preset, key)
        if value is not None:
            out[key] = value

    return out


class Turn:
    """One message's worth of work: what goes to Lodestar - or, with
    `claude` set, what the claude CLI is asked - and where the answer is
    written when it comes back (nowhere, without a session)."""

    def __init__(self, payload: dict[str, Any], session: Optional[str] = None,
                 prompt: str = "", keep: Optional[int] = None,
                 claude: Optional[dict[str, Any]] = None,
                 files: Optional[list[dict[str, Any]]] = None):
        self.payload, self.session, self.prompt, self.keep = payload, session, prompt, keep
        self.claude = claude
        self.files = files or []
        self.started = time.monotonic()
        # Who fed the prompt when it was not the person: set by the server
        # side that did (a game feed, say), never from a request body. The
        # shell draws such a message as its source's, never as the person's.
        self.source: Optional[str] = None
        self.by: str = ""
        # A room (seren_symposium.room): the seats this message goes round,
        # in order. Then claude is planned seat by seat, as the round goes.
        self.room: Optional[list[str]] = None
        # ...and the person asked for the round to end with the seat speaking.
        self.cut = False
        self.layered: dict[str, Any] = {}
        # One seat's turn in that round: whose, and whether it writes the
        # person's message in (the first seat to come back does).
        self.seat: Optional[str] = None
        self.with_prompt = True

    def receipt(self, reply: dict[str, Any], tools: Optional[list[str]] = None,
                steps: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """What sits under an answer: who said it, where, and what it reached
        for - and, folded away, how it got there (its steps)."""
        meta = {k: reply[k] for k in ("model", "node", "tool_rounds") if reply.get(k) is not None}
        if tools:
            meta["tools"] = tools
        if steps:
            meta["steps"] = steps
        meta["seconds"] = round(time.monotonic() - self.started, 1)
        return meta


def plan_claude(cfg: SymposiumConfig, store: SessionStore, s: dict[str, Any], layered: dict[str, Any],
                messages: list[dict[str, Any]], prompt: str,
                files: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """What the claude CLI is asked, for a turn in session `s`.

    The CLI keeps its own copy of the conversation, so no history goes on the
    wire - except the messages this conversation never saw (all of them on
    its first turn, or those another voice answered since), handed over in
    front of the prompt. `messages` is the thread this turn follows: shorter
    than what the CLI saw when this is a retry, and then the conversation it
    would resume remembers the answer being replaced - so it starts over,
    with the thread handed over, rather than answering with that in mind.

    The folder's model only counts when the folder's voice is claude too: a
    local model's name means nothing to the CLI. Files are named by path, and
    the chat's folder is added to what the CLI may read - and, with
    claude.save_files, the one place it may write without being asked."""
    preset = cfg.folder(s["folder"])
    known = s.get("claude") or {}
    resume = known.get("session") if known.get("seen", 0) <= len(messages) else None
    missed = messages[known.get("seen", 0):] if resume else messages
    folder = store.files_dir(s["id"])
    told = [{**(t := as_told(m)), "content": attachments.with_files(
                t["content"], attachments.for_claude(m.get("files") or [], folder))}
            for m in missed if not terminal.harness_notice(m)]
    # The carabiner's session block, read once for the turn: its permissions,
    # its model and how hard it thinks are the layer under every chat's.
    ks = permissions.carabiner_session(cfg)
    model = layered.get("model_override") or (
        preset.model if preset and preset.voice == "claude" else "") or cfg.claude.model or ks.get("model") or ""
    system, replace = claude_system(cfg, stack.own_prompt(preset, layered), str(folder))
    # Permissions in layers: the CLI's own, the carabiner's, every chat's, the
    # project's, this chat's (seren_symposium.permissions). The chat's own
    # folder is always writable when save_files is on, whatever they say.
    perms = permissions.effective(permissions.layers(cfg, preset, layered, ks))
    think = permissions.thinking(cfg, preset, layered, ks)
    return {
        "prompt": claude_voice.handover(_trim_history(told, cfg.claude.handover_max_chars, also=("stopped",)),
                                        attachments.with_files(prompt, attachments.for_claude(files or [], folder)),
                                        resumed=bool(resume)),
        "resume": resume, "model": model, "system_prompt": system, "system_replace": replace,
        "add_dirs": [str(folder)],
        "allow": ([claude_voice.folder_rule(str(folder))] if cfg.claude.save_files else []) + perms["allow"],
        "mode": perms["mode"],
        "effort": think["effort"], "thinking_display": think["thinking_display"],
        # Where it runs (a picked-up chat: where its conversation began), and
        # whether its first turn here forks it off a terminal still holding it.
        "cwd": layered.get("cwd") or "",
        # A conversation an earlier Symposium let go is still running in its
        # own process: this turn forks off it rather than write the same
        # transcript beside it (seren_symposium.behind); it reports on its own.
        "fork": bool(resume and (known.get("fork") or resume in claude_voice.LET_GO)),
    }


def plan_seat(cfg: SymposiumConfig, store: SessionStore, s: dict[str, Any], seat: dict[str, Any],
              layered: dict[str, Any], prompt: str, files: Optional[list[dict[str, Any]]],
              pending: bool) -> dict[str, Any]:
    """plan_claude for one seat of a room (seren_symposium.room): the same
    permissions, model and files - but its own conversation and folder, a
    handover where every line says who said it, and the room's framing after
    the system prompt. `pending`: the person's message isn't in the chat yet
    (no seat has come back this round), so it ends the handover from here."""
    # Its own system prompt, after the project's and the chat's: the world,
    # the place, then who this seat is (stack.own_parts, 23 Nov 2026).
    # And what it runs on, over the chat's: its model, how hard it thinks, what
    # it shows of it (permissions.thinking reads seat_* as the top layer).
    plan = plan_claude(cfg, store, {**s, "claude": seat.get("claude")},
                       {**layered, "cwd": seat.get("cwd") or layered.get("cwd") or "",
                        "seat_prompt": seat.get("system_prompt") or "",
                        **({"model_override": seat["model"]} if seat.get("model") else {}),
                        "seat_effort": seat.get("effort") or "",
                        "seat_thinking_display": seat.get("thinking_display") or ""},
                       s["messages"], prompt, files)
    known = seat.get("claude") or {}
    thread = list(s["messages"]) + ([{"role": "user", "content": prompt, "files": files or []}] if pending else [])
    resumed = plan["resume"] is not None
    missed = thread[known.get("seen", 0):] if resumed else thread
    folder = store.files_dir(s["id"])
    told = [{**(t := as_told(m, room.heard(m))), "content": attachments.with_files(
                t["content"], attachments.for_claude(m.get("files") or [], folder))}
            for m in missed if not terminal.harness_notice(m)]
    plan["prompt"] = room.handover(_trim_history(told, cfg.claude.handover_max_chars, also=("seat", "by", "stopped")),
                                   seat["name"], cfg.ui.you, cfg.claude.name, resumed)
    names = [x["name"] for x in s.get("seats") or []]
    plan["system_prompt"] = "\n\n".join(p for p in (
        plan["system_prompt"], room.framing(cfg.claude.room_framing, names, seat["name"], cfg.ui.you)) if p)
    return plan


def claude_system(cfg: SymposiumConfig, own: str, files: str) -> tuple[str, bool]:
    """What the CLI is told in its system prompt, and whether that REPLACES
    the CLI's own: the preface (Margin's page, seren_symposium.preface) when
    there is one, then the chat's or folder's own prompt, then the framing.
    Without a preface the same text is appended to the CLI's prompt."""
    pre = preface.text(cfg.claude.preface)
    return "\n\n".join(p for p in (pre, own, claude_framing(cfg, files)) if p), bool(pre)


def claude_framing(cfg: SymposiumConfig, files: str) -> str:
    """The framing as a turn is told it: where it is answering ({files} is
    the chat's folder), then - with claude.auto_memory at last_resort - that
    Claude Code's own MEMORY.md comes after the house's memory tools."""
    framing = cfg.claude.framing.replace("{files}", files) if cfg.claude.framing else ""
    # The house's own tools, named so they are not forgotten (claude.tools_note).
    tools = (cfg.claude.tools_note.replace("{servers}", ", ".join(cfg.claude.mcp_servers))
             if cfg.claude.tools_note and cfg.claude.mcp_servers else "")
    note = cfg.claude.auto_memory_note if cfg.claude.auto_memory == "last_resort" else ""
    # A long command goes in the background and the turn ends (claude.background).
    behind = cfg.claude.background_note if cfg.claude.background else ""
    return "\n\n".join(p for p in (framing, tools, note, behind) if p)


_FENCE = re.compile(r"```.*?```", re.S)
_THINK = re.compile(r"^\s*<think>[\s\S]*?(</think>|$)")


def spoken_text(content: str, max_chars: int) -> tuple[str, bool]:
    """What of an answer is said: no <think>, no code fences (the code is
    for the eye), markdown marks dropped, cut at a sentence past max_chars."""
    text = _THINK.sub("", content or "")
    text = _FENCE.sub(" ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s?)", "", text, flags=re.M)
    text = re.sub(r"[*_]{1,3}(\S[^*_]*?)[*_]{1,3}", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    cut = False
    if len(text) > max_chars:
        head = text[:max_chars]
        stop = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind("\n"))
        text, cut = (head[:stop + 1] if stop > max_chars // 2 else head).strip(), True
    return text, cut


def _named_files(store: SessionStore, sid: str, raw: Any) -> "list[dict[str, Any]] | str":
    """The files a message names, checked against what is on disk: the
    entries, or why not. Size and type are read from the file, not trusted."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        return "files is a list"
    out = []
    for f in raw:
        name = f.get("name") if isinstance(f, dict) else f
        path = store.file_path(sid, name) if isinstance(name, str) else None
        if path is None:
            return f"no file {name!r} in this chat"
        orig = f.get("orig") if isinstance(f, dict) and isinstance(f.get("orig"), str) else name
        entry = file_entry(name, orig, attachments.guess_type(orig, attachments.head(path)), path.stat().st_size)
        # A screen-share frame sent again (an edit) stays one.
        if isinstance(f, dict) and f.get("screen") is True:
            entry["screen"] = True
            if isinstance(f.get("source"), str):
                entry["source"] = f["source"][:200]
        # An answer said aloud stays one, with the voice and how long.
        if isinstance(f, dict) and f.get("said") is True:
            entry["said"] = True
            if isinstance(f.get("seconds"), (int, float)) and not isinstance(f.get("seconds"), bool):
                entry["seconds"] = float(f["seconds"])
            if isinstance(f.get("voice"), str):
                entry["voice"] = f["voice"][:64]
        # A clip from the microphone stays one, with how long it is.
        if isinstance(f, dict) and f.get("ears") is True:
            entry["ears"] = True
            if isinstance(f.get("seconds"), (int, float)) and not isinstance(f.get("seconds"), bool):
                entry["seconds"] = float(f["seconds"])
            if isinstance(f.get("device"), str):
                entry["device"] = f["device"][:120]
        out.append(entry)
    return out


def plan_turn(cfg: SymposiumConfig, store: SessionStore, body: Any,
              frame: Optional["screen.Frame"] = None) -> "Turn | JSONResponse":
    """The request -> a Turn, or the error to answer with.

    Without `session` it is the old stateless shape: the shell sends its own
    `history`. With one, the thread and the settings come from the session
    file, and the answer is written back to it. `retry` asks the last prompt
    again; the exchange it replaces is only dropped once the new one is in.

    The voice - who answers - layers like any setting: the session's, else
    the folder's, else lodestar. Claude needs a session to keep its
    conversation in.

    `files` names attachments already in the chat (POST .../files put them
    there); `screen: true` adds the newest screen-share frame as one more.
    """
    if not isinstance(body, dict):
        return JSONResponse({"error": "send a JSON object"}, status_code=400)
    sid = body.get("session")
    if not sid:
        if body.get("voice") == "claude":
            return JSONResponse({"error": "the claude voice answers in a session"}, status_code=400)
        return Turn(to_lodestar_payload(cfg, body))
    s = store.get(sid)
    if s is None:
        return JSONResponse({"error": "no such session", "session": sid}, status_code=404)
    messages, keep = s["messages"], None
    prompt = body.get("prompt") or ""
    files = _named_files(store, sid, body.get("files"))
    if isinstance(files, str):
        return JSONResponse({"error": files}, status_code=400)
    if body.get("retry"):
        last = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user"), None)
        if last is None:
            return JSONResponse({"error": "nothing to retry"}, status_code=400)
        prompt, keep, messages = messages[last]["content"], last, messages[:last]
        files = s["messages"][last].get("files") or []
    elif body.get("screen"):
        if frame is None:
            return JSONResponse({"error": "no screen frame yet", "hint": "take one first (POST /api/screen)"},
                                status_code=409)
        shot = store.add_file(sid, time.strftime("screen-%Y%m%d-%H%M%S.jpg", time.localtime(frame.at)),
                              frame.jpeg, "image/jpeg")
        # Marked, so the shell keeps it in the screen-share panel - not in the
        # conversation, not in Files.
        shot.update(screen=True, source=frame.window or f"display {frame.monitor}")
        files = files + [shot]
    if not prompt and not files:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    layered = {**s["settings"],
               **{k: v for k, v in body.items() if k not in ("session", "retry", "history", "files", "screen", "to")},
               "prompt": prompt, "folder": s["folder"], "history": messages}
    preset = cfg.folder(s["folder"])
    if s.get("seats"):
        return plan_room(cfg, s, layered, prompt, files, keep, body.get("to"))
    voice = layered.get("voice") or (preset.voice if preset else "") or "lodestar"
    if voice == "claude":
        if not claude_voice.available(cfg.claude):
            return JSONResponse({"error": "the claude voice is not available here",
                                 "hint": "claude.enabled is off, or claude.command is not "
                                         "on this machine's PATH"}, status_code=409)
        where = layered.get("cwd") or ""
        if where and not Path(os.path.expanduser(where)).is_dir():
            return JSONResponse({"error": f"this chat works in {where}, which is not a folder on this machine",
                                 "hint": "change the chat's folder (⚙), or clear it to use claude.cwd"},
                                status_code=409)
        return Turn({}, sid, prompt, keep, files=files,
                    claude=plan_claude(cfg, store, s, layered, messages, prompt, files))
    layered["prompt"] = attachments.with_files(
        prompt, attachments.for_local(files, store, sid, cfg.files.inline_max_chars))
    return Turn(to_lodestar_payload(cfg, layered), sid, prompt, keep, files=files)


def _seat_name_for(folder: str, fallback: str) -> str:
    """A seat's name from the folder it works in, the way the kitchen names a
    hand: SerenLodestar -> lodestar. `fallback` when that makes no name."""
    base = re.sub(r"^seren", "", Path(os.path.expanduser(folder or "")).name, flags=re.I).lower()
    base = re.sub(r"[^\w·.-]+", "", base)[:32]
    return base if room.valid_name(base) else fallback


def plan_room(cfg: SymposiumConfig, s: dict[str, Any], layered: dict[str, Any], prompt: str,
              files: list[dict[str, Any]], keep: Optional[int], to: Any) -> "Turn | JSONResponse":
    """A message into a room: who answers it (room.who_answers), checked
    before anything runs. Each seat's own plan waits for its turn."""
    if keep is not None:
        return JSONResponse({"error": "a room can't retry a round yet",
                             "hint": "rewind to before the message and send it again"}, status_code=409)
    if not claude_voice.available(cfg.claude):
        return JSONResponse({"error": "the claude voice is not available here",
                             "hint": "a room's seats are claude conversations"}, status_code=409)
    names = room.who_answers(s["seats"], prompt, to)
    if isinstance(names, str):
        return JSONResponse({"error": names}, status_code=400)
    for name in names:
        where = (room.seat(s, name) or {}).get("cwd") or ""
        if where and not Path(os.path.expanduser(where)).is_dir():
            return JSONResponse({"error": f"{name} works in {where}, which is not a folder on this machine",
                                 "hint": "give the seat another folder, or let it go"}, status_code=409)
    turn = Turn({}, s["id"], prompt, None, files=files)
    turn.room, turn.layered = names, layered
    return turn


async def bring_back(cfg: SymposiumConfig, store: SessionStore, turn: Turn, answer: str,
                     before: Optional[set[str]] = None) -> tuple[str, list[dict[str, Any]]]:
    """The answer as it is kept: files it saved into the chat adopted, images
    it showed from the web fetched - and pointed at the copies."""
    found: list[dict[str, Any]] = []
    if before is not None:
        answer, found = attachments.adopt_saved(answer, store, turn.session, before)
    if cfg.files.fetch_images:
        answer, fetched = await attachments.capture_images(answer, store, turn.session,
                                                           int(cfg.files.max_mb * 1024 * 1024))
        found += fetched
    return answer, found


# Between the pieces of an answer while bring_back rewrites them as one text
# (an image fetched, a saved file adopted): a line no rewrite touches, so the
# pieces can be told apart again afterwards.
_SAID_SEP = "\n\n<!-- symposium:said -->\n\n"


async def bring_back_said(cfg: SymposiumConfig, store: SessionStore, turn: Turn, heard: "claude_voice.ClaudeTurn",
                          answer: str, before: Optional[set[str]]) -> tuple[str, list[dict[str, Any]]]:
    """bring_back for a claude answer said in pieces: the pieces go through it
    as one text, then are split again, so each `said` step shows the same
    words the kept answer has. Should the split not come back whole, the
    answer is kept as one text and the pieces lose their places (the old
    layout), never their words."""
    said = heard.said()
    if len(said) < 2:
        return await bring_back(cfg, store, turn, answer, before)
    kept, found = await bring_back(cfg, store, turn, _SAID_SEP.join(s["text"].strip() for s in said), before)
    parts = kept.split(_SAID_SEP)
    if len(parts) != len(said):
        heard.steps = [s for s in heard.steps if s["kind"] != "said"]
        return kept.replace(_SAID_SEP, "\n\n"), found
    for s, text in zip(said, parts):
        s["text"] = text
    return "\n\n".join(parts), found


def keep_stopped(cfg: SymposiumConfig, store: SessionStore, turn: Turn,
                 heard: "claude_voice.ClaudeTurn") -> Optional[dict[str, Any]]:
    """A claude turn stopped mid-answer is KEPT as far as it got: the
    person's message, the words said so far, the steps taken, marked
    `stopped` in its receipt. The session as kept, or None.

    Chad, 17 Nov 2026, the first stop: the window showed the stopped answer,
    handed his message back to the composer, and a reload showed neither -
    while the CLI's own conversation remembered both. "both should be kept,
    because of the rule you state in the cancellation that things that have
    been said are already there." What was said was said; a chat that drops
    it is a chat that disagrees with the conversation behind it.

    Whether that conversation saw the message: the CLI had started (its init
    came) when it was stopped, so yes, and the next turn resumes it with
    nothing to hand over. Stopped before that, the exchange is kept in the
    chat and handed over next time like anything else it missed."""
    if not turn.session:
        return None
    answer = heard.answer("")
    meta = turn.receipt({"model": heard.model, "node": cfg.claude.name}, heard.tools, heard.kept_steps())
    meta["stopped"] = True
    saw = bool(heard.init)
    if turn.seat is not None:
        return store.record_seat(turn.session, turn.seat, turn.prompt if turn.with_prompt else None,
                                 answer, meta, heard.session_id,
                                 files=turn.files if turn.with_prompt else None,
                                 claude_init=heard.init or None, heard_it=saw, stopped=True)
    return store.record(turn.session, turn.prompt, answer, meta, keep=turn.keep,
                        claude_session=heard.session_id if saw else None, files=turn.files,
                        source=turn.source, by=turn.by, claude_init=heard.init or None, stopped=True)


def as_told(m: dict[str, Any], words: Optional[str] = None) -> dict[str, Any]:
    """Message `m` as a conversation that missed it is handed it (`words`:
    its content, when not all of it goes). An answer that was stopped goes
    back marked so, with the tools it had started: the CLI killed in that
    turn may have kept none of it (17 Nov 2026: the chat showed a whole
    answer that its own conversation could not recall saying)."""
    text = m.get("content", "") if words is None else words
    if not (m.get("meta") or {}).get("stopped"):
        return {**m, "content": text}
    tools = [f"{s.get('tool') or 'a tool'}: {s.get('summary') or ''}".rstrip(": ")
             for s in (m.get("meta") or {}).get("steps") or [] if s.get("kind") == "tool"]
    used = ("Tools you had started before the stop (their results may not have come back): " + "; ".join(tools)
            if tools else "")
    return {**m, "content": "\n\n".join(p for p in (text, used) if p), "stopped": True}


def put_on_avatar(cfg: SymposiumConfig, store: SessionStore, turn: Turn, heard: "claude_voice.ClaudeTurn",
                  event: dict[str, Any], found: list[dict[str, Any]]) -> Optional[tuple[str, str]]:
    """An answer that shows a picture captioned `avatar` wears it from now on
    (seren_symposium.avatars): a seat its own, a chat's one Claude the
    default - in THIS chat, or in every chat when captioned `avatar
    everywhere`. (Whose avatar changed, "chat" or "everywhere"), or None. A
    picture named by the file a tool gave is pointed at the chat's copy, so
    the answer shows it too."""
    want = avatars.wanted(event.get("response") or "", found)
    if want is None:
        return None
    ref, name, everywhere = want
    who = turn.seat or avatars.DEFAULT
    try:
        if everywhere:
            avatars.Avatars(avatars.root(cfg)).wear_file(who, store.file_path(turn.session, name))
        else:
            wear_in_chat(store, turn.session, who, name)
    except (avatars.BadAvatar, OSError) as ex:
        log.info("avatar for %s not put on: %s", who, ex)
        return None
    if not ref.startswith("files/"):
        old, new = f"]({ref})", f"](files/{name})"
        event["response"] = event["response"].replace(old, new)
        for s in heard.said():
            s["text"] = s["text"].replace(old, new)
    return who, "everywhere" if everywhere else "chat"


def wear_in_chat(store: SessionStore, sid: str, who: str, name: str) -> dict[str, Any]:
    """`who` wears the picture `name`, one of this chat's files, in this chat
    only (the chat's `faces` setting). The chat, as it now is."""
    if not avatars.valid(who):
        raise avatars.BadAvatar("an avatar is worn by a seat's name, default, you, or a voice's name")
    path = store.file_path(sid, name)
    if path is None:
        raise avatars.BadAvatar("no such picture in this chat")
    if attachments.guess_type(name, attachments.head(path)) not in attachments.INLINE_IMAGES:
        raise avatars.BadAvatar("that is not a picture the window can show (png, jpeg, gif, webp, bmp, svg)")
    try:
        s = store.set_face(sid, who, name)
    except Invalid as ex:
        raise avatars.BadAvatar(str(ex)) from None
    if s is None:
        raise avatars.BadAvatar("no such chat")
    return s


class PermitDesk:
    """One claude turn's may-I questions: the CLI's permission tool asks
    (POST /api/permit), the ask goes into the turn's stream as a
    `permission` event, and waits here for your answer (/api/permit/answer)."""

    def __init__(self, session: str, folder: str = "", allowed: Optional[list[str]] = None):
        self.session = session
        self.folder = folder                            # its project, for an Always kept there
        self.ticket = secrets.token_urlsafe(24)
        self.events: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, tuple[asyncio.Future, list[str]]] = {}
        self.asked: dict[str, dict[str, Any]] = {}      # id -> the ask, for the turn's steps
        self.always: set[str] = set()
        # The turn's allow list as worked out in layers: an ask a rule on it
        # already covers is answered yes here (permissions.covers).
        self.allowed: list[str] = list(allowed or [])
        self.turn: Optional[claude_voice.ClaudeTurn] = None

    def close(self, why: str) -> None:
        for fut, _ in self.pending.values():
            if not fut.done():
                fut.set_result({"behavior": "deny", "message": why})
        self.pending.clear()


def _permit_url(cfg: SymposiumConfig) -> str:
    host = cfg.ui.host if cfg.ui.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    return f"http://{f'[{host}]' if ':' in host else host}:{cfg.ui.port}/api/permit"


def waker(cfg: SymposiumConfig, store: SessionStore, session: str, seat: Optional[str]):
    """What a set-aside conversation calls when it answers on its own - a
    task it left running has ended (claude_voice.events, `on_wake`): the
    answer is kept in the chat like any other, under the seat's name, marked
    as having come back by itself and with what the CLI said of the task. A
    seat with nothing to say about it (a pass) puts nothing in."""
    async def woke(event: dict[str, Any], heard: "claude_voice.ClaudeTurn", why: list[str]) -> None:
        if event["type"] != "done":
            log.info("a set-aside turn in %s (%s) ended without an answer: %s", session, seat or "the chat", event.get("error"))
            return
        if seat is not None and room.passed(event.get("final") or event.get("response") or ""):
            return
        late = Turn({}, session, "")
        late.seat = seat
        store.files_dir(session).mkdir(parents=True, exist_ok=True)
        answer, found = await bring_back_said(cfg, store, late, heard, event.get("response") or "", None)
        found += attachments.keep_tool_images(heard.images, store, session,
                                              int(cfg.files.max_mb * 1024 * 1024), heard.steps)
        meta = late.receipt(event, heard.tools, heard.kept_steps())
        meta.pop("seconds", None)                   # nobody was waiting, so there is nothing to time
        meta["woke"] = why or ["a task it had left running ended"]
        pieces = heard.said()
        store.record_woken(session, seat, answer, meta, heard.session_id, answer_files=found,
                           final=pieces[-1]["text"].strip() if len(pieces) > 1 else None)
    return woke


def pickup_handlers(cfg: SymposiumConfig, store: SessionStore, state: Any):
    """What behind.pick_up is handed, for the conversations an earlier
    Symposium let go (or lost in a crash) and this one finds still running:
    where each one's transcript is, and how the chat is told it ended."""
    root = terminal.projects_dir(cfg.claude.projects_dir)

    def transcript_of(conversation: str) -> Optional[Path]:
        return terminal.find(root, conversation)

    async def quiet(chat: str) -> None:
        for _ in range(600):                        # a turn running there finishes first (10 min at most)
            if chat not in state.answering:
                return
            await asyncio.sleep(1.0)

    def what(entry: dict[str, Any]) -> str:
        return " · ".join(str(t.get("description") or t.get("type") or "a task") for t in entry.get("tasks") or []) \
            or "a task"

    async def on_told(entry: dict[str, Any], told: list[str]) -> None:
        """It said how it went, in its transcript, while nobody was reading:
        kept in the chat as it would have been, marked as picked up."""
        await quiet(entry["chat"])
        for text in told:
            meta = {"node": cfg.claude.name, "picked_up": True,
                    "woke": [f"{what(entry)} - ended while Symposium was away"]}
            store.record_woken(entry["chat"], entry.get("seat"), text, meta, entry["conversation"])

    async def on_silent(entry: dict[str, Any]) -> None:
        """Its process went before it could say anything (the box went down,
        say): the conversation is resumed with a note so it says, in its own
        words, how it went - never a line under its name it did not write."""
        chat, seat = entry["chat"], entry.get("seat")
        s = store.get(chat)
        if s is None:
            return
        await quiet(chat)
        s = store.get(chat) or s
        ends = []
        for t in entry.get("tasks") or []:
            info = terminal.task_info(None, entry["conversation"], str(t.get("id") or ""))
            out = info.get("output")
            ends.append(f"- {t.get('description') or t.get('type') or 'a task'} (id {t.get('id')}): "
                        + (f"its output file ends:\n```\n{out['tail'][-1500:]}\n```" if out else "no output file"))
        note = ("[Symposium, not the person] Symposium went away (a restart or a crash) while you had "
                "background work running, and your process ended before it could report. What it had going:\n"
                + "\n".join(ends) + "\nSay in the chat how it went, from what it left.")
        conv = entry["conversation"]
        layered = {**s["settings"], "folder": s["folder"]}
        if seat:
            spot = room.seat(s, seat)
            if not isinstance(spot, dict):
                return
            plan = plan_seat(cfg, store, s, {**spot, "claude": {"session": conv, "seen": len(s["messages"])}},
                             layered, note, None, pending=False)
        else:
            plan = plan_claude(cfg, store, {**s, "claude": {"session": conv, "seen": len(s["messages"])}},
                               layered, s["messages"], note)
        heard, done = claude_voice.ClaudeTurn(conv), None
        state.answering.add(chat)
        try:
            async for ev in claude_voice.events(
                    cfg.claude, prompt=plan["prompt"], resume=conv, model=plan["model"],
                    system_prompt=plan["system_prompt"], add_dirs=plan.get("add_dirs"), allow=plan.get("allow"),
                    turn=heard, replace=bool(plan.get("system_replace")), mode=plan.get("mode"), own_tools=False,
                    cwd=entry.get("cwd") or plan.get("cwd") or "", effort=plan.get("effort") or "",
                    thinking_display=plan.get("thinking_display") or "",
                    on_wake=waker(cfg, store, chat, seat), owner={"chat": chat, "seat": seat}):
                if ev["type"] in ("done", "error"):
                    done = ev
        finally:
            state.answering.discard(chat)
        if done is not None:
            await waker(cfg, store, chat, seat)(done, heard, [f"{what(entry)} - ended while Symposium was away"])

    return transcript_of, on_told, on_silent


def background_of(s: dict[str, Any]) -> list[dict[str, Any]]:
    """What this chat's conversations have running in the background, for the
    window: [{seat (None: the chat's one voice), conversation, tasks, since}]."""
    out = []
    for name, holder in [(None, s), *[(x.get("name"), x) for x in s.get("seats") or []]]:
        known = holder.get("claude") if isinstance(holder.get("claude"), dict) else {}
        got = claude_voice.parked(known.get("session"))
        if got:
            out.append({"seat": name, "conversation": known["session"], **got})
    # Let go by an earlier start and still running, though the chat has moved
    # on to a fork of it since (plan_claude): still this chat's, still shown.
    listed = {b["conversation"] for b in out}
    for conv, entry in list(claude_voice.LET_GO.items()):
        if entry.get("chat") == s.get("id") and conv not in listed:
            got = claude_voice.parked(conv)
            if got:
                out.append({"seat": entry.get("seat"), "conversation": conv, **got})
    return out


async def claude_answer(cfg: SymposiumConfig, store: SessionStore, turn: Turn,
                        busy: set[str], permits: Optional[dict[str, PermitDesk]] = None):
    """A claude turn, as stream events, written to the session when it
    comes back - then `saved`, as after a Lodestar turn.

    One turn at a time per session: two at once would both resume the same
    CLI conversation, and which answer it remembers would be a race.

    With `permits` (a streamed turn, someone is watching) and claude.ask on,
    a tool not on the list is asked about in the chat - its `permission`
    events come in among the answer's own."""
    if turn.session in busy:
        yield {"type": "error", "error": "this chat is still answering the last message"}
        return
    busy.add(turn.session)
    ask = turn.claude
    desk = (PermitDesk(turn.session, (store.get(turn.session) or {}).get("folder") or "", ask.get("allow"))
            if permits is not None and cfg.claude.ask else None)
    # A conversation set aside with a task running is the process this turn
    # is handed to; its permission asks still carry its first turn's ticket.
    old_ticket = claude_voice.parked_ticket(ask["resume"]) if desk is not None else None
    if desk is not None:
        permits[desk.ticket] = desk
        if old_ticket:
            permits[old_ticket] = desk
    heard: Optional[claude_voice.ClaudeTurn] = None
    try:
        heard = claude_voice.ClaudeTurn(ask["resume"] or str(uuid.uuid4()))
        if desk is not None:
            desk.turn = heard
        # Made before the CLI is pointed at it: --add-dir wants a folder that is there.
        store.files_dir(turn.session).mkdir(parents=True, exist_ok=True)
        before = store.listing(turn.session)
        spoken = claude_voice.events(cfg.claude, prompt=ask["prompt"], resume=ask["resume"],
                                     model=ask["model"], system_prompt=ask["system_prompt"],
                                     add_dirs=ask.get("add_dirs"), allow=ask.get("allow"), turn=heard,
                                     permit={"url": _permit_url(cfg), "ticket": desk.ticket} if desk else None,
                                     replace=bool(ask.get("system_replace")),
                                     mode=ask.get("mode"), own_tools="mode" not in ask,
                                     cwd=ask.get("cwd") or "", fork=bool(ask.get("fork")),
                                     effort=ask.get("effort") or "", thinking_display=ask.get("thinking_display") or "",
                                     on_wake=waker(cfg, store, turn.session, turn.seat) if turn.session else None,
                                     owner={"chat": turn.session, "seat": turn.seat} if turn.session else None)
        async for event in (_with_asks(spoken, desk) if desk else spoken):
            # A seat of a room with nothing to add says so (room.passed): it
            # has heard the round, and nothing of its goes in the chat. Judged
            # on the LAST thing it said: a seat that read the code first and
            # then found nothing new has still passed.
            passing = (event["type"] == "done" and turn.seat is not None
                       and room.passed(event.get("final") or event.get("response") or ""))
            found: list[dict[str, Any]] = []
            worn: Optional[tuple[str, str]] = None
            if event["type"] == "done" and not passing:
                event["response"], found = await bring_back_said(cfg, store, turn, heard,
                                                                 event.get("response") or "", before)
                # Pictures a tool sent back (make_image draws on another box):
                # kept in the chat beside the answer, not left as a path there.
                found += attachments.keep_tool_images(heard.images, store, turn.session,
                                                      int(cfg.files.max_mb * 1024 * 1024), heard.steps)
                if found:
                    event["files"] = found
                # An image captioned `avatar` is the face this voice wears
                # from now on (seren_symposium.avatars).
                worn = put_on_avatar(cfg, store, turn, heard, event, found)
            yield {"type": "pass", "model": event.get("model"), "node": event.get("node")} if passing else event
            if event["type"] == "done":
                if turn.seat is not None:
                    # What the other seats are handed of this answer: its last
                    # piece, when it was said in several (room.heard).
                    pieces = heard.said()
                    saved = store.record_seat(
                        turn.session, turn.seat, turn.prompt if turn.with_prompt else None,
                        None if passing else event["response"],
                        None if passing else turn.receipt(event, heard.tools, heard.kept_steps()),
                        heard.session_id, files=turn.files if turn.with_prompt else None,
                        answer_files=found, claude_init=heard.init,
                        final=pieces[-1]["text"].strip() if len(pieces) > 1 and not passing else None)
                else:
                    saved = store.record(turn.session, turn.prompt, event["response"],
                                         turn.receipt(event, heard.tools, heard.kept_steps()), keep=turn.keep,
                                         claude_session=heard.session_id, files=turn.files,
                                         answer_files=found, source=turn.source, by=turn.by,
                                         claude_init=heard.init)
                if saved is not None:
                    yield {"type": "saved", "session": summary(saved)}
                    if worn:
                        yield {"type": "avatar", "name": worn[0], "scope": worn[1]}
    except asyncio.CancelledError:
        # Stopped where it was (the person's stop, or a shutdown): the CLI
        # is gone with its reader, and what it had said and done is kept
        # (keep_stopped). The turn ENDS here rather than passing the
        # cancellation on, so whoever follows it hears `stopped` and `saved`
        # like any other ending.
        saved = None
        if heard is not None:
            try:
                saved = keep_stopped(cfg, store, turn, heard)
            except Exception:  # noqa: BLE001 - a stop must end the turn whatever the keeping does
                log.exception("a stopped turn in %s could not be kept", turn.session)
        yield {"type": "stopped", "kept": saved is not None}
        if saved is not None:
            yield {"type": "saved", "session": summary(saved)}
    finally:
        busy.discard(turn.session)
        if desk is not None:
            permits.pop(desk.ticket, None)
            if old_ticket:
                permits.pop(old_ticket, None)
            desk.close("the turn ended before anyone answered")


async def room_answer(cfg: SymposiumConfig, store: SessionStore, turn: Turn,
                      permits: Optional[dict[str, PermitDesk]] = None):
    """A message going round a room's table (seren_symposium.room): each seat
    in `turn.room`, in order, as a claude turn of its own - its conversation,
    its folder - planned only when its turn comes, so it is handed what the
    seats before it said this round. Every event says whose it is: a `seat`
    line opens each one's part, and the round ends with `round`.

    The person's message goes into the chat with the first seat to come back
    (a turn that fails writes nothing, as anywhere here); one that fails or
    passes doesn't stop the round.

    A SEAT CAN CALL ON A SEAT (room, "Seats call on each other"): one whose
    last message names another with @name is answered by it next. A seat
    still due this round only moves up; one that has spoken, or was not
    asked, gets a turn the person did not ask for - a volley - and those
    are counted against claude.room_volleys. At the limit the round ends
    (`capped`) and the person referees."""
    written = False
    queue, answered = list(turn.room or []), []
    called_by: dict[str, str] = {}
    volleys, limit, capped = 0, max(0, cfg.claude.room_volleys), False
    while queue:
        name = queue.pop(0)
        s = store.get(turn.session)
        seat = room.seat(s, name) if s is not None else None
        caller = called_by.pop(name, None)
        yield {"type": "seat", "seat": name, **({"called_by": caller} if caller else {})}
        if seat is None:
            yield {"type": "error", "seat": name, "error": f"{name} has left the room"}
            continue
        one = Turn({}, turn.session, turn.prompt, files=turn.files if not written else [])
        one.seat, one.with_prompt = name, not written
        one.claude = plan_seat(cfg, store, s, seat, turn.layered, turn.prompt, turn.files, pending=not written)
        said, halted = "", False
        async for ev in claude_answer(cfg, store, one, set(), permits):
            if ev["type"] == "saved":
                written = True
            if ev["type"] == "done":                   # a pass is not one: it calls on nobody
                said = ev.get("final") or ev.get("response") or ""
            if ev["type"] == "stopped":                # the person said stop: the round ends with this seat
                halted = True
            yield {**ev, "seat": name}
        answered.append(name)
        if halted:
            yield {"type": "round", "seats": answered, "volleys": volleys, "stopped": True}
            return
        # The person has something to say and asked for it to go when this
        # seat was done (POST .../stop {after: seat}): the seats still due are
        # not asked, and whoever this seat called on is not called. Only once
        # the person's first message is in the chat - it is written with the
        # first seat to come back, and a round cut before that would lose it.
        if turn.cut and written and queue:
            yield {"type": "round", "seats": answered, "volleys": volleys, "cut": True, "not_asked": list(queue)}
            return
        if not (said and limit):
            continue
        names = [x["name"] for x in (store.get(turn.session) or {}).get("seats") or []]
        for other in reversed([n for n in room.mentioned(names, said)[0] if n != name]):
            if other in queue:
                queue.remove(other)                    # due anyway: it only moves up
            elif volleys >= limit:
                capped = True
                continue
            else:
                volleys += 1
            queue.insert(0, other)
            called_by[other] = name
    yield {"type": "round", "seats": answered, "volleys": volleys, **({"capped": True} if capped else {})}


async def _with_asks(spoken, desk: PermitDesk):
    """The CLI's events and the desk's asks, as one stream, in the order
    they happen. The CLI is read in a task of its own: while it waits on an
    answer it says nothing, and the ask must still get out."""
    async def pump():
        try:
            async for ev in spoken:
                await desk.events.put(ev)
        finally:
            await desk.events.put(None)
    task = asyncio.create_task(pump())
    try:
        while (ev := await desk.events.get()) is not None:
            yield ev
    finally:
        if not task.done():
            task.cancel()
        desk.close("the turn ended before anyone answered")


async def lodestar_answer(cfg: SymposiumConfig, store: SessionStore, turn: Turn, r: httpx.Response):
    """Lodestar's /chat/stream, already open as `r`, as events - line for
    line, nothing renamed - written to the session when `done` comes back,
    then `saved`. Mirrors claude_answer for the other voice."""
    tools: list[str] = []
    try:
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            yield event
            if event.get("type") == "tool_status" and event.get("tool"):
                tools.append(event["tool"])
            elif event.get("type") == "done" and turn.session:
                kept, found = await bring_back(cfg, store, turn, event.get("response") or "")
                saved = store.record(turn.session, turn.prompt, kept, turn.receipt(event, tools), keep=turn.keep,
                                     files=turn.files, answer_files=found, source=turn.source, by=turn.by)
                if saved is not None:
                    yield {"type": "saved", "session": summary(saved)}
    except httpx.HTTPError as ex:
        yield {"type": "error", "error": "lodestar stopped mid-answer", "detail": f"{type(ex).__name__}: {ex}"}
    finally:
        await r.aclose()


def asked_of(turn: Turn) -> dict[str, Any]:
    """What a window opened mid-answer draws above the answer: the prompt and
    its files, whose they were (a plugin's feed is drawn as the plugin's), and
    whether the turn is a retry - then there is no new question, and the
    answer being replaced comes off the screen (22 Oct 2026)."""
    return {"prompt": turn.prompt, "files": turn.files, "source": turn.source, "by": turn.by,
            "retry": turn.keep is not None}


class LiveTurn:
    """A turn in flight, kept apart from whoever is watching it.

    The voice (the CLI, or Lodestar's stream) is read by a task of this
    object's own and every event it says is kept here. A window FOLLOWS the
    turn - from the first event, however late it arrives - and may stop
    following without stopping anything. Before this the reader WAS the
    response: close the window, or click another chat while the answer was
    coming, and the response generator closed, which killed the CLI
    mid-thought, and the answer was gone along with whatever it had reasoned
    and every tool it had run. Now the turn ends when the voice is done, is
    recorded then, and the window is only ever a spectator.

    One per session at a time (app.state.live, by session id), and the
    session is in app.state.answering from start to finish: a second message
    into THAT chat is refused with 409, any other chat is free."""

    def __init__(self, session: str, answering: set[str], live: dict[str, "LiveTurn"],
                 asked: Optional[dict[str, Any]] = None):
        self.session = session
        # The question the turn is answering, for a window that opens the chat
        # while it runs: the session file has neither until the answer is in.
        self.asked = asked
        self.events: list[dict[str, Any]] = []
        self.finished = asyncio.Event()
        self.changed = asyncio.Condition()
        self._answering, self._live = answering, live
        self.task: Optional[asyncio.Task] = None
        self.stopped = False                # the person stopped it (stop), not a shutdown
        # When it began: a window that comes back to the chat counts from
        # here, not from when it looked (17 Nov 2026: the timer started over
        # at 0 on every visit, which read as an answer that had only just begun).
        self.started = time.time()

    def stop(self) -> None:
        """The person says stop: the voice is cut where it is (the CLI is
        killed with its reader). What was already kept stays - in a room,
        every seat that had come back - and a claude turn caught mid-air is
        kept as far as it got (keep_stopped)."""
        self.stopped = True
        if self.task is not None:
            self.task.cancel()

    @classmethod
    def begin(cls, request: Request, session: str, source,
              asked: Optional[dict[str, Any]] = None) -> "LiveTurn":
        """Start `source` (an async iterator of events) running on its own,
        registered for the session. The caller checked nobody else is."""
        return cls.start(request.app.state, session, source, asked)

    @classmethod
    def start(cls, state, session: str, source, asked: Optional[dict[str, Any]] = None) -> "LiveTurn":
        """The same, from the app's state rather than a request: for a turn
        nobody asked for over HTTP (something on this desk speaking into a
        chat on its own - such a turn is marked with its source, see Turn)."""
        turn = cls(session, state.answering, state.live, asked)
        state.answering.add(session)
        state.live[session] = turn
        turn.task = asyncio.create_task(turn._run(source))
        return turn

    async def _run(self, source) -> None:
        try:
            async for ev in source:
                await self._push(ev)
        except asyncio.CancelledError:
            # Only a voice that keeps nothing of an unfinished answer gets here
            # (a local model's stream): a claude turn ends itself, kept (keep_stopped).
            await self._push({"type": "error", "error": "the turn was stopped", **(
                {"stopped": True, "detail": "you stopped it, and nothing of this answer was kept"} if self.stopped
                else {"detail": "Symposium shut down while the answer was coming"})})
            if self.stopped:
                return                      # asked for: an ending, not a cancellation to pass on
            raise
        except Exception as ex:  # noqa: BLE001 - a follower must hear the end, whatever it was
            await self._push({"type": "error", "error": "the turn stopped unexpectedly",
                              "detail": f"{type(ex).__name__}: {ex}"})
        finally:
            self._answering.discard(self.session)
            if self._live.get(self.session) is self:
                del self._live[self.session]
            self.finished.set()
            async with self.changed:
                self.changed.notify_all()

    async def _push(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        async with self.changed:
            self.changed.notify_all()

    async def follow(self):
        """Every event so far, then each new one as it comes, until the turn
        is over. Stopping early (the window went away) stops only this."""
        i = 0
        while True:
            while i < len(self.events):
                yield self.events[i]
                i += 1
            if self.finished.is_set():
                return
            async with self.changed:
                if i >= len(self.events) and not self.finished.is_set():
                    await self.changed.wait()


def _ndjson(events) -> StreamingResponse:
    async def lines():
        async for ev in events:
            yield json.dumps(ev) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


def _annotate_401(payload: dict[str, Any]) -> dict[str, Any]:
    # The one error we ANNOTATE rather than relay. Lodestar answers a bad
    # bearer with {"detail": "unauthorized"}, which is correct and useless
    # here - it cannot know the token it rejected came from a yaml file on
    # this machine. Saying which file to edit is knowledge only the client
    # has, and without it the UI renders a flat "401 unauthorized" that
    # sends people looking at Lodestar, where nothing is wrong.
    payload = dict(payload)
    payload["error"] = "lodestar rejected our bearer token"
    payload["hint"] = ("check lodestar.bearer_token in seren-symposium.yaml "
                       "against the token Lodestar is configured with")
    return payload


def _relay(r: httpx.Response, content: bytes) -> JSONResponse:
    """A non-200 from Lodestar, passed on as JSON whatever it was."""
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            payload = {"response": payload}
    except ValueError:
        payload = {"text": content.decode("utf-8", "replace")}
    if r.status_code == 401:
        payload = _annotate_401(payload)
    return JSONResponse(payload, status_code=r.status_code)


def _busy() -> JSONResponse:
    return JSONResponse({"error": "this chat is still answering the last message"}, status_code=409)


def _unreachable(cfg: SymposiumConfig, ex: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "lodestar unreachable", "detail": f"{type(ex).__name__}: {ex}",
         "url": cfg.lodestar.url}, status_code=502)


async def _json(request: Request) -> Any:
    """The body, or None when it is not JSON (the caller answers 400)."""
    try:
        return await request.json()
    except ValueError:
        return None


def create_app(config: Optional[SymposiumConfig] = None) -> FastAPI:
    cfg = config or load_config()
    # What was set in the window, laid over the yaml now - before anything
    # reads a project, a name or the confirm setting (seren_symposium.prefs).
    prefs = Prefs(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg
        app.state.sessions = SessionStore(cfg.sessions_dir())
        app.state.answering = set()     # sessions with a turn running (either voice)
        app.state.live = {}             # session id -> LiveTurn, while it runs
        app.state.screen = None         # the newest screen-share frame (screen.Frame)
        app.state.ears = ears.Recorder(cfg.ears.max_seconds)   # the microphone, one clip at a time
        # trust_env=False for a loopback head, ON PURPOSE.
        #
        # httpx reads HTTP_PROXY/HTTPS_PROXY from the environment by default.
        # On a machine behind an intercepting corporate proxy (Zscaler and
        # friends) that means a call to 127.0.0.1:6361 gets routed OUT to the
        # proxy and back - or more often just fails - unless NO_PROXY happens
        # to list localhost. Nobody debugging "why can't the desktop app reach
        # the service on the same machine" guesses that first.
        #
        # A remote Lodestar is a different matter: that traffic SHOULD respect
        # the environment's proxy settings, so only loopback opts out.
        _host = (urlparse(cfg.lodestar.url).hostname or "").lower()
        _loopback = _host in ("127.0.0.1", "localhost", "::1", "")
        bearer = cfg.lodestar.resolve_bearer()
        app.state.client = httpx.AsyncClient(
            base_url=cfg.lodestar.url.rstrip("/"),
            timeout=cfg.lodestar.timeout_seconds,
            trust_env=not _loopback,
            headers=({"Authorization": f"Bearer {bearer}"} if bearer else {}),
        )

        # "Is there a newer symposium". Cosmetic - catch EVERYTHING, because a
        # version check must never stop the window from opening.
        try:
            from seren_meninges.updates import UpdateChecker
            app.state.updates = UpdateChecker(
                "seren-symposium",
                enabled=cfg.updates.enabled,
                index_url=cfg.updates.index_url,
                ttl_seconds=cfg.updates.check_interval_hours * 3600.0,
                allow_prerelease=cfg.updates.allow_prerelease,
                fallback_version=APP_VERSION,
            )
        except Exception as exc:  # noqa: BLE001
            app.state.updates = None
            log.info("update checking unavailable (%s)", exc)

        # What the chats leave running is written down, so it outlives us, and
        # what an earlier start let go (or lost in a crash) is picked up
        # (seren_symposium.behind, 23 Nov 2026).
        claude_voice.LEDGER = behind.Ledger(cfg.sessions_dir().parent / behind.FILE_NAME)
        transcript_of, on_told, on_silent = pickup_handlers(cfg, app.state.sessions, app.state)
        app.state.pickup = asyncio.create_task(behind.pick_up(
            claude_voice.LEDGER, claude_voice.LET_GO, transcript_of, on_told, on_silent))

        yield
        # What the conversations have running runs on (claude.let_go): the
        # ledger keeps it and the next start picks it up. Killed instead, the
        # CLI went but its task ran on unseen, with no one to report it -
        # measured on the real CLI, 23 Nov 2026.
        if cfg.claude.let_go:
            claude_voice.let_go_all_parked()
        else:
            claude_voice.stop_all_parked()
        app.state.pickup.cancel()
        claude_voice.LET_GO.clear()
        claude_voice.LEDGER = None
        await app.state.client.aclose()

    app = FastAPI(title="SerenSymposium", version=APP_VERSION, lifespan=lifespan)
    app.state.screen = None
    app.state.prefs = prefs
    app.state.permits = {}             # ticket -> PermitDesk, one per claude turn being watched
    app.mount("/vendor", StaticFiles(directory=UI_DIR / "vendor"), name="vendor")

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        refused = refuse_cross_site(cfg, request.method, request.headers)
        if refused is not None:
            status, why = refused
            log.warning("refused %s %s: %s", request.method, request.url.path, why)
            return JSONResponse({"error": "refused", "detail": why}, status_code=status)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        # The same baseplate every sibling's /viewer wears; ui/ holds only
        # Symposium's bricks (tabs, body, styles, scripts, the status pill).
        return HTMLResponse(headers={"Cache-Control": "no-store, must-revalidate"},
                            content=render_from_dir(
            UI_DIR,
            title="SerenSymposium",
            brand="Seren<b>Symposium</b>",
            subtitle=f"v{APP_VERSION} · the front door",
            accent=ACCENT,
        ))

    @app.get("/api/config")
    async def api_config(request: Request):
        """Everything the shell needs to draw itself. No secrets: the bearer
        stays in Python and is never serialised to the UI."""
        from seren_meninges.updates import updates_payload
        return {
            "service": "SerenSymposium",
            "version": APP_VERSION,
            "accent": ACCENT,
            "lodestar": cfg.lodestar.url,
            "viewers": cfg.viewers.shown(),
            "folders": [f.model_dump() for f in cfg.folders],
            "sessions_dir": str(cfg.sessions_dir()),
            "claude": {"name": cfg.claude.name, "available": claude_voice.available(cfg.claude),
                       "models": cfg.claude.models, "cwd": cfg.claude.cwd},
            "project_defaults": prefs.project_defaults(),
            "you": cfg.ui.you,
            "prefs_file": str(prefs.path),
            "files": {"max_mb": cfg.files.max_mb, "fetch_images": cfg.files.fetch_images},
            "screen": {"enabled": cfg.screen.enabled, "available": screen.available(),
                       "monitors": screen.monitors() if cfg.screen.enabled else []},
            "ears": {"enabled": cfg.ears.enabled, "available": ears.available(), "device": cfg.ears.device,
                     "language": cfg.ears.language, "max_seconds": cfg.ears.max_seconds,
                     "devices": ears.devices() if cfg.ears.enabled else []},
            "speech": {"enabled": cfg.speech.enabled, "voice": cfg.speech.voice, "max_chars": cfg.speech.max_chars},
            "confirm_destructive": cfg.ui.confirm_destructive,
            "queue": cfg.ui.queue,
            "translate": cfg.ui.translate,
            "interrupt": cfg.ui.interrupt,
            "pictures": cfg.ui.pictures,
            "my_pictures": cfg.ui.my_pictures,
            "updates": await updates_payload(
                getattr(request.app.state, "updates", None),
                distribution="seren-symposium", installed=APP_VERSION),
        }

    @app.get("/health")
    async def health(request: Request):
        """Ours is trivial; the useful half is the cluster head.

        TWO probes, because "the process is up" and "I can actually talk to it"
        are different questions and only the second one is the one you care
        about. Lodestar's /health is PUBLIC - it answers 200 for a client whose
        bearer is wrong, missing, or meant for a different machine. Probing only
        that yields a green dot on an app where every single message comes back
        401, which is the most confusing failure this client can produce.

        So the second probe is /api/v1/chat/health, which sits behind the bearer
        AND reports whether a node is actually serving llama. That splits the
        outcome four ways, and each one tells the operator a different thing to
        go fix:

            unreachable   - nothing answered. Is Lodestar running?
            unauthorized  - it answered, and rejected our token.
            no_inference  - we are in, but no node is serving llama.
            ok            - ready.
        """
        client = request.app.state.client
        url = cfg.lodestar.url

        try:
            r = await client.get("/health")
        except Exception as ex:  # noqa: BLE001
            return {"ok": True, "service": "SerenSymposium", "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": False, "status": "unreachable",
                "detail": f"{type(ex).__name__}: {ex}"}}

        if r.status_code != 200:
            return {"ok": True, "service": "SerenSymposium", "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": False, "status": "unreachable",
                "detail": f"HTTP {r.status_code}"}}

        try:
            chat_health = await client.get("/api/v1/chat/health")
        except Exception as ex:  # noqa: BLE001
            # The head answered a moment ago, so this is a flap rather than a
            # cold service. Say so instead of claiming it is down.
            return {"ok": True, "service": "SerenSymposium", "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "unreachable",
                "detail": f"chat backend probe failed - {type(ex).__name__}: {ex}"}}

        if chat_health.status_code == 401:
            return {"ok": True, "service": "SerenSymposium", "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "unauthorized",
                "detail": "Lodestar rejected our bearer token - check "
                          "lodestar.bearer_token in seren-symposium.yaml"}}

        if chat_health.status_code != 200:
            return {"ok": True, "service": "SerenSymposium", "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "no_inference",
                "detail": f"chat backend returned HTTP {chat_health.status_code}"}}

        payload = chat_health.json()
        if not payload.get("ok"):
            return {"ok": True, "service": "SerenSymposium", "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "no_inference",
                "detail": payload.get("reason") or "no node is serving llama"}}

        node = payload.get("node")
        return {"ok": True, "service": "SerenSymposium", "version": APP_VERSION, "lodestar": {
            "url": url, "reachable": True, "status": "ok",
            "detail": f"inference on {node}" if node else "ready"}}

    @app.post("/api/chat")
    async def chat(request: Request):
        """Proxy to Lodestar with the bearer attached.

        Deliberately thin: Symposium does not know what a tool is, does not
        route, does not touch MCP. Lodestar owns all of that. If logic starts
        accumulating here, it belongs in the cluster head instead.

        The one translation that IS ours is the folder preset, because folders
        are a Symposium concept the cluster head has never heard of. Lodestar's
        own field names are never renamed - they are the contract, and a second
        set of names would just be a second thing to keep in sync.
        """
        store: SessionStore = request.app.state.sessions
        turn = plan_turn(cfg, store, await _json(request), request.app.state.screen)
        if isinstance(turn, JSONResponse):
            return turn
        if turn.room is not None:
            return JSONResponse({"error": "a room's round is several answers; it streams",
                                 "hint": "POST /api/chat/stream"}, status_code=400)
        if turn.claude is not None:
            if turn.session in request.app.state.answering:
                return _busy()
            # claude_answer keeps its own one-at-a-time guard for direct callers;
            # here the LiveTurn holds the session in `answering`, so it gets a
            # set of its own rather than finding itself already there.
            live = LiveTurn.begin(request, turn.session, claude_answer(cfg, store, turn, set()), asked_of(turn))
            out: dict[str, Any] = {}
            async for event in live.follow():
                if event["type"] in ("done", "error"):
                    out = event
            if out.get("type") != "done":
                return JSONResponse({k: v for k, v in out.items() if k != "type"}, status_code=502)
            return {**{k: v for k, v in out.items() if k != "type"}, "session": turn.session}
        try:
            r = await request.app.state.client.post("/api/v1/chat", json=turn.payload)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        if r.status_code != 200:
            return _relay(r, r.content)
        payload = r.json()
        if turn.session:
            payload["response"], found = await bring_back(cfg, store, turn, payload.get("response") or "")
            store.record(turn.session, turn.prompt, payload["response"],
                         turn.receipt(payload), keep=turn.keep, files=turn.files, answer_files=found,
                         source=turn.source, by=turn.by)
            payload["session"] = turn.session
        return JSONResponse(payload)

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request):
        """Lodestar's /chat/stream, line for line: `chunk` as the words come,
        `tool_status` before a tool runs, then `done` (the whole answer) or
        `error`. Nothing is renamed. The one line that is ours comes last:
        `{"type": "saved", "session": {...}}` once a session has the turn.
        A claude turn speaks the same lines (claude_voice translates).

        With a session the turn is a LiveTurn: it runs on its own and this
        response only follows it. Close the window or open another chat and
        the answer still comes and is still kept; GET /api/sessions/{sid}/live
        picks it up again. One turn per chat at a time: 409 while it runs."""
        store: SessionStore = request.app.state.sessions
        turn = plan_turn(cfg, store, await _json(request), request.app.state.screen)
        if isinstance(turn, JSONResponse):
            return turn
        if turn.session and turn.session in request.app.state.answering:
            return _busy()
        if turn.room is not None:
            # A room: the round is one LiveTurn, holding the chat from the
            # first seat to the last (seren_symposium.room).
            source = room_answer(cfg, store, turn, request.app.state.permits)
            live = LiveTurn.begin(request, turn.session, source, asked_of(turn))
            live.round = turn                           # so the round can be told to end with the seat speaking
            return _ndjson(live.follow())
        if turn.claude is not None:
            # A set of its own: see /api/chat.
            source = claude_answer(cfg, store, turn, set(), request.app.state.permits)
            return _ndjson(LiveTurn.begin(request, turn.session, source, asked_of(turn)).follow())
        client: httpx.AsyncClient = request.app.state.client
        try:
            r = await client.send(client.build_request("POST", "/api/v1/chat/stream", json=turn.payload),
                                  stream=True)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        if r.status_code != 200:
            content = await r.aread()
            await r.aclose()
            return _relay(r, content)
        source = lodestar_answer(cfg, store, turn, r)
        if not turn.session:
            return _ndjson(source)       # nothing is kept, so there is nothing to come back to
        return _ndjson(LiveTurn.begin(request, turn.session, source, asked_of(turn)).follow())

    @app.get("/api/models")
    async def models(request: Request):
        """What the session settings can pick from: the models on Lodestar's
        llama node, flattened to names (Lodestar wraps the Observatory's
        {"models": [...]} in its own {"node", "models"})."""
        try:
            r = await request.app.state.client.get("/api/v1/service/llama/models")
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        if r.status_code != 200:
            return _relay(r, r.content)
        body = r.json()
        found = body.get("models")
        if isinstance(found, dict):
            found = found.get("models")
        names = [m if isinstance(m, str) else (m or {}).get("name") for m in (found or [])]
        return {"node": body.get("node"), "models": [n for n in names if n]}

    # ── Sessions ────────────────────────────────────────────────────────
    # Kept here, on this machine; nothing below talks to Lodestar.

    @app.get("/api/sessions")
    async def sessions_list(request: Request):
        """Each with `answering`: a turn is running in it right now (follow
        it at /api/sessions/{sid}/live)."""
        answering = request.app.state.answering
        return {"sessions": [{**s, "answering": s["id"] in answering} for s in request.app.state.sessions.list()]}

    @app.get("/api/sessions/search")
    async def sessions_search(request: Request, q: str = "", chat: str = ""):
        """?q=words - every chat, archived ones too, whose title holds all the
        words or one of whose messages does; each with its newest few
        `matches` ({index, role, seat, snippet}) and how many `hits` in all.
        With &chat=<id>, that one chat with every match, not just the newest.
        This chat history only: what Wren remembers is Memory's to search."""
        if chat:
            one = await asyncio.to_thread(request.app.state.sessions.search_one, chat, q[:200])
            return {"query": q[:200], "sessions": [one] if one else []}
        found = await asyncio.to_thread(request.app.state.sessions.search, q[:200])
        return {"query": q[:200], "sessions": found}

    @app.post("/api/sessions")
    async def sessions_create(request: Request):
        """{folder, title?, settings?, messages?} - with messages, an import."""
        body = await _json(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "send a JSON object"}, status_code=400)
        try:
            s = request.app.state.sessions.create(
                folder=body.get("folder") or "", title=body.get("title") or "",
                settings=body.get("settings"), messages=body.get("messages"))
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return JSONResponse(s, status_code=201)

    @app.get("/api/sessions/{sid}")
    async def sessions_get(request: Request, sid: str):
        s = request.app.state.sessions.get(sid)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        live = request.app.state.live.get(sid)
        # claude_init is the prompt stack's (GET /api/stack), not the chat's.
        return {**{k: v for k, v in s.items() if k != "claude_init"},
                **({"seats": [{k: v for k, v in x.items() if k != "claude_init"} for x in s["seats"]]}
                   if s.get("seats") else {}),
                "answering": sid in request.app.state.answering,
                "pending": live.asked if live is not None else None,
                "started": live.started if live is not None else None,
                # what its conversations left running behind an answer (claude.background)
                "background": background_of(s)}

    @app.post("/api/sessions/{sid}/background/stop")
    async def sessions_background_stop(request: Request, sid: str):
        """End what this chat's conversations have running in the background
        ({seat?}: only that seat's; a chat's one voice is seat null). The
        task is killed with the process that held it, and nobody is woken."""
        s = request.app.state.sessions.get(sid)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        only = body.get("seat") if isinstance(body, dict) and "seat" in body else ...
        stopped = [b["seat"] for b in background_of(s)
                   if (only is ... or b["seat"] == only) and claude_voice.stop_parked(b["conversation"])]
        return {"ok": True, "stopped": stopped, "background": background_of(s)}

    @app.get("/api/sessions/{sid}/background/{task_id}")
    async def sessions_background_task(request: Request, sid: str, task_id: str):
        """One task this chat left running, looked into: what the CLI says it
        is, how long it has been going, the tool call that started it (from the
        transcript) and the end of its output so far (the CLI's task file).
        For the task found still going hours later, by someone who no longer
        remembers starting it (23 Nov 2026)."""
        s = request.app.state.sessions.get(sid)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        for b in background_of(s):
            task = next((d for d in b.get("details") or [] if d.get("id") == task_id), None)
            if task is None:
                continue
            root = terminal.projects_dir(cfg.claude.projects_dir)
            found = await asyncio.to_thread(
                lambda: terminal.task_info(terminal.find(root, b["conversation"]), b["conversation"], task_id))
            return {**task, "seat": b["seat"], "now": time.time(), **found}
        return JSONResponse({"error": "nothing by that id is running in this chat (it may have just ended)"},
                            status_code=404)

    @app.get("/api/sessions/{sid}/steps/{tool_id}")
    async def sessions_step(request: Request, sid: str, tool_id: str, cli: str = ""):
        """One tool call of this chat's in full: what it was handed and all
        that came back, from the CLI's own transcript (a turn keeps only a
        preview of each). Looked in: the conversation the window names (`cli`,
        for a step of a turn still running, not kept yet), the one a kept step
        names, the chat's, its seats'."""
        s = request.app.state.sessions.get(sid)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        named = [st.get("sid") for m in s.get("messages") or [] for st in (m.get("meta") or {}).get("steps") or []
                 if isinstance(st, dict) and st.get("id") == tool_id]
        ids = [cli, *named, (s.get("claude") or {}).get("session"),
               *[(x.get("claude") or {}).get("session") for x in s.get("seats") or []]]
        root = terminal.projects_dir(cfg.claude.projects_dir)

        def look() -> Optional[dict[str, Any]]:
            for cli_id in dict.fromkeys(i for i in ids if terminal.is_id(i)):
                path = terminal.find(root, cli_id)
                found = terminal.tool_step(path, tool_id) if path else None
                if found:
                    return found
            return None
        found = await asyncio.to_thread(look)
        if found is None:
            return JSONResponse({"error": "the CLI's transcript does not have that tool call "
                                          "(a side copy keeps none, and a deleted one is gone)"}, status_code=404)
        return found

    @app.get("/api/sessions/{sid}/live")
    async def sessions_live(request: Request, sid: str):
        """The turn running in this chat, from its first event to its end,
        as /api/chat/stream would have shown it - for a window that opened
        (or came back) while the answer was coming. 404 when nothing is."""
        live = request.app.state.live.get(sid)
        if live is None:
            return JSONResponse({"error": "nothing is answering in this chat"}, status_code=404)
        return _ndjson(live.follow())

    @app.post("/api/sessions/{sid}/stop")
    async def sessions_stop(request: Request, sid: str):
        """Stop the turn running in this chat. What it had already kept stays
        (in a room: every seat that had come back), and a claude answer in
        mid-air is kept as far as it got. 404 when nothing is answering.

        {after: "seat"}: nobody is cut off - a room's round ends when the seat
        speaking is done, and the seats still due are not asked. In a chat
        with one voice there is no round to cut, so the answer just finishes
        (the reply says which: `after` is "seat" or "answer")."""
        live = request.app.state.live.get(sid)
        if live is None:
            return JSONResponse({"error": "nothing is answering in this chat"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("after") == "seat":
            round_ = getattr(live, "round", None)
            if round_ is not None:
                round_.cut = True
            return {"ok": True, "after": "seat" if round_ is not None else "answer"}
        live.stop()
        return {"ok": True}

    @app.patch("/api/sessions/{sid}")
    async def sessions_update(request: Request, sid: str):
        """{title?, folder?, settings?} - rename, move to another folder, or
        change settings (a setting sent as null goes back to the folder's)."""
        body = await _json(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "send a JSON object"}, status_code=400)
        try:
            s = request.app.state.sessions.update(sid, body)
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return s if s is not None else JSONResponse({"error": "no such session"}, status_code=404)

    @app.post("/api/sessions/{sid}/remove")
    async def sessions_remove(request: Request, sid: str):
        """Take a chat out of Symposium and delete NOTHING: its file and its
        attachments are set aside (`kept`, under removed/ beside the chats),
        and the Claude conversations it carried (`conversations` - its own,
        each seat's) are left as they are, free to be picked up again or
        carried on in a terminal. For the chat that was loaded in by
        accident; Delete is the one that throws things away."""
        if sid in request.app.state.answering:
            return _busy()
        store: SessionStore = request.app.state.sessions
        s = await asyncio.to_thread(store.remove, sid)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        freed = [h["claude"]["session"] for h in [s, *(s.get("seats") or [])]
                 if isinstance(h.get("claude"), dict) and h["claude"].get("session")]
        for conversation in freed:                  # nothing of this chat's is left running behind it
            claude_voice.stop_parked(conversation)
        return {"ok": True, "conversations": freed, "kept": str(store.root / "removed")}

    @app.delete("/api/sessions/{sid}")
    async def sessions_delete(request: Request, sid: str):
        if sid in request.app.state.answering:
            return _busy()
        gone = request.app.state.sessions.get(sid)
        if not request.app.state.sessions.delete(sid):
            return JSONResponse({"error": "no such session"}, status_code=404)
        for b in background_of(gone or {}):         # nothing of this chat's is left running behind it
            claude_voice.stop_parked(b["conversation"])
        return {"ok": True}

    # ── What the window sets: the app's settings, the projects ──────────
    # Kept in the prefs file, never the yaml: a reinstall writes the yaml.

    def _answering_in(request: Request, folder: str) -> bool:
        return any(s["folder"] == folder and s["id"] in request.app.state.answering
                   for s in request.app.state.sessions.list())

    @app.patch("/api/prefs")
    async def prefs_change(request: Request):
        """{you?, claude_name?, confirm_destructive?, project_defaults?}"""
        body = await _json(request)
        try:
            prefs.set_app(body)
        except BadPref as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        out = {"you": cfg.ui.you, "claude_name": cfg.claude.name,
               "confirm_destructive": cfg.ui.confirm_destructive}
        if isinstance(body, dict) and "project_defaults" in body:
            out["project_defaults"] = prefs.project_defaults()
        return out

    @app.get("/api/claude/models")
    async def claude_models(request: Request, fresh: int = 0):
        """What a Claude model field suggests: the carabiner's list (it asks
        Anthropic, or knows the list it shipped with), else claude.models from
        this yaml. Every item is {value, label}; any model id can still be typed."""
        found = await asyncio.to_thread(carabiner.models, cfg.claude, bool(fresh))
        if found:
            return {"source": "carabiner", "models": found}
        return {"source": "symposium", "models": [{"value": m, "label": m} for m in cfg.claude.models]}

    @app.get("/api/claude")
    async def claude_settings(request: Request, session: str = "", folder: str = "", settings: str = ""):
        """The claude voice's settings as they stand, and the carabiner on
        this box (never its tokens). With `session` (or `folder` and a JSON
        `settings` for a chat not made yet), also what a turn there would run
        with: its permissions worked out in layers, and where each came from."""
        c = cfg.claude
        out = {"available": claude_voice.available(c), "name": c.name, "cwd": c.cwd,
               "mode": c.permission_mode, "ask": c.ask, "tools": c.allowed_tools, "dirs": c.add_dirs,
               "effort": c.effort, "thinking_display": c.thinking_display,
               "modes": list(permissions.WINDOW_MODES),
               "carabiner": await asyncio.to_thread(carabiner.describe, c),
               "carabiner_session": await asyncio.to_thread(permissions.carabiner_session, cfg)}
        if session or folder or settings:
            own: dict[str, Any] = {}
            if session:
                s = request.app.state.sessions.get(session)
                if s is None:
                    return JSONResponse({"error": "no such session"}, status_code=404)
                folder, own = s["folder"], s["settings"]
            elif settings:
                try:
                    own = json.loads(settings)
                except ValueError:
                    return JSONResponse({"error": "settings is JSON"}, status_code=400)
                own = own if isinstance(own, dict) else {}
            out["effective"] = await asyncio.to_thread(permissions.for_chat, cfg, folder, own)
        return out

    # ── Picking up a terminal's conversation (seren_symposium.terminal) ──
    # Files on this box, read only: the CLI's own transcripts. No network.

    @app.get("/api/claude/sessions")
    async def terminal_sessions(request: Request):
        """The CLI's newest conversations on this box that are not a chat
        here yet, newest first: {id, cwd, title, updated, said, warm}."""
        store = request.app.state.sessions
        root = terminal.projects_dir(cfg.claude.projects_dir)
        rows = await asyncio.to_thread(lambda: terminal.listing(root, exclude=store.claude_ids()))
        return {"sessions": rows, "dir": str(root), "warm_seconds": terminal.WARM_SECONDS}

    @app.post("/api/claude/sessions/pickup")
    async def terminal_pickup(request: Request):
        """{id, folder?, fork?} - a new chat that IS that conversation. fork
        defaults to whether it is warm (a terminal may still be in it)."""
        body = await _json(request)
        if not isinstance(body, dict) or not terminal.is_id(body.get("id")):
            return JSONResponse({"error": "id is a claude conversation id"}, status_code=400)
        store = request.app.state.sessions
        if body["id"] in await asyncio.to_thread(store.claude_ids):
            return JSONResponse({"error": "that conversation is already a chat here"}, status_code=409)
        root = terminal.projects_dir(cfg.claude.projects_dir)
        path = await asyncio.to_thread(terminal.find, root, body["id"])
        if path is None:
            return JSONResponse({"error": "no such conversation on this box"}, status_code=404)
        folder = body.get("folder") or ""
        if not isinstance(folder, str):
            return JSONResponse({"error": "folder is a string"}, status_code=400)
        fork = body.get("fork")
        if fork is None:
            fork = time.time() - path.stat().st_mtime < terminal.WARM_SECONDS
        conversation = await asyncio.to_thread(terminal.transcript, path)
        if not conversation["terminal"]:
            return JSONResponse({"error": "that conversation was not started at a terminal",
                                 "hint": "a headless run - the hippocampus waking a session, a ripple - "
                                         "stays where it was started"}, status_code=403)
        try:
            s = store.pick_up(conversation, folder=folder, fork=bool(fork))
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return JSONResponse(s, status_code=201)

    # ── A room's seats (seren_symposium.room) ───────────────────────────
    # The chat's file, and - for a seat picked up from a terminal - the CLI's
    # transcripts, read only. No network.

    @app.post("/api/sessions/{sid}/seats")
    async def seat_add(request: Request, sid: str):
        """{name, cwd?, pickup?, fork?, host?} - a seat at this chat's table.
        `pickup`: a terminal's conversation (an id from /api/claude/sessions)
        that carries on as this seat, in its own folder unless `cwd` says
        otherwise; `fork` as for a pickup. `host`: what the chat's own Claude
        is called when it takes the first seat (else from its folder)."""
        body = await _json(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "send a JSON object"}, status_code=400)
        store: SessionStore = request.app.state.sessions
        s = store.get(sid)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        if sid in request.app.state.answering:
            return _busy()
        cwd, conversation, fork = body.get("cwd") or "", None, False
        if body.get("pickup") is not None:
            if not terminal.is_id(body["pickup"]):
                return JSONResponse({"error": "pickup is a claude conversation id"}, status_code=400)
            if body["pickup"] in await asyncio.to_thread(store.claude_ids):
                return JSONResponse({"error": "that conversation is already a chat or a seat here"}, status_code=409)
            path = await asyncio.to_thread(terminal.find, terminal.projects_dir(cfg.claude.projects_dir), body["pickup"])
            if path is None:
                return JSONResponse({"error": "no such conversation on this box"}, status_code=404)
            told = await asyncio.to_thread(terminal.transcript, path)
            if not told["terminal"]:
                return JSONResponse({"error": "that conversation was not started at a terminal"}, status_code=403)
            conversation, cwd = told["id"], cwd or told["cwd"]
            fork = body.get("fork")
            if fork is None:
                fork = time.time() - path.stat().st_mtime < terminal.WARM_SECONDS
        if cwd and not Path(os.path.expanduser(str(cwd))).is_dir():
            return JSONResponse({"error": f"{cwd} is not a folder on this machine"}, status_code=400)
        host = body.get("host") or _seat_name_for(s["settings"].get("cwd") or cfg.claude.cwd, "host")
        try:
            s = store.add_seat(sid, body.get("name") or "", cwd=cwd, conversation=conversation,
                               fork=bool(fork), host=host, system_prompt=body.get("system_prompt") or "",
                               own={k: body.get(k) for k in ("model", "effort", "thinking_display") if body.get(k)})
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return JSONResponse(s, status_code=201)

    @app.patch("/api/sessions/{sid}/seats")
    async def seats_order(request: Request, sid: str):
        """{order: [names]} - who sits where: the order the table goes round."""
        body = await _json(request)
        if sid in request.app.state.answering:
            return _busy()
        try:
            s = request.app.state.sessions.order_seats(sid, body.get("order") if isinstance(body, dict) else None)
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return s if s is not None else JSONResponse({"error": "no such session"}, status_code=404)

    @app.patch("/api/sessions/{sid}/seats/{name}")
    async def seat_change(request: Request, sid: str, name: str):
        """{name?, hue?, system_prompt?} - call a seat something else (what
        it said goes with it), give it another colour, or say who it is: its
        own system prompt, after the project's and the chat's."""
        body = await _json(request)
        if sid in request.app.state.answering:
            return _busy()
        try:
            s = request.app.state.sessions.change_seat(sid, name, body)
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=404 if str(ex).startswith("no seat") else 400)
        return s if s is not None else JSONResponse({"error": "no such session"}, status_code=404)

    @app.delete("/api/sessions/{sid}/seats/{name}")
    async def seat_drop(request: Request, sid: str, name: str):
        store: SessionStore = request.app.state.sessions
        if sid in request.app.state.answering:
            return _busy()
        try:
            s = store.drop_seat(sid, name)
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=404)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        return s

    # ── Avatars (seren_symposium.avatars) ───────────────────────────────
    # Files beside the chats. The one fetch - a link - is the fetch an
    # answer's web image already gets, into the chat it was asked from.

    @app.get("/api/avatars")
    async def avatars_list():
        """{avatars: {name: {type, at}}} - who wears a picture: seats by
        name, and `default` for everyone who has not picked."""
        return {"avatars": avatars.Avatars(avatars.root(cfg)).all()}

    @app.get("/api/avatars/{name}")
    async def avatars_get(name: str):
        got = avatars.Avatars(avatars.root(cfg)).path(name) if avatars.valid(name) else None
        if got is None:
            return JSONResponse({"error": "nobody of that name wears a picture"}, status_code=404)
        media, headers = attachments.serve_headers(got[1])
        return FileResponse(got[0], media_type=media, headers=headers)

    @app.post("/api/avatars/{name}")
    async def avatars_wear(request: Request, name: str):
        """{session, file} - a picture already in that chat (uploaded, drawn
        in the picture panel, saved by an answer); or {session, url} - one
        from the web, fetched into that chat first and kept there too; or
        {data, filename?} - the picture itself, base64, with no chat at all
        (the Settings tab). `name` wears it from now on: a seat's name,
        `default`, `you`, a voice's name.

        `scope`: "chat" - in that chat only (its `faces` setting; needs
        `session`), over what `name` wears everywhere; else every chat. The
        answer is {avatars} and, for a chat's, its {faces}."""
        body = await _json(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "send {session, file}, {session, url} or {data}"}, status_code=400)
        if not avatars.valid(name):
            return JSONResponse({"error": "an avatar is worn by a seat's name, default, you, or a voice's name"},
                                status_code=400)
        in_chat = body.get("scope") == "chat"
        shelf = avatars.Avatars(avatars.root(cfg))
        if isinstance(body.get("data"), str) and not in_chat and not body.get("session"):
            try:
                data = base64.b64decode(body["data"], validate=True)
                mime = attachments.guess_type(str(body.get("filename") or ""), data[:16])
                return {"avatars": await asyncio.to_thread(shelf.wear, name, data, mime)}
            except (binascii.Error, ValueError) as ex:      # BadAvatar is a ValueError
                return JSONResponse({"error": str(ex) if isinstance(ex, avatars.BadAvatar) else "data is not base64"},
                                    status_code=400)
        if not isinstance(body.get("session"), str):
            return JSONResponse({"error": "send {session, file}, {session, url} or {data}"}, status_code=400)
        store: SessionStore = request.app.state.sessions
        sid = body["session"]
        if store.get(sid) is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        file = body.get("file")
        if isinstance(body.get("url"), str) and body["url"]:
            if not cfg.files.fetch_images:
                return JSONResponse({"error": "fetching pictures from the web is off (files.fetch_images)"},
                                    status_code=409)
            _, fetched = await attachments.capture_images(f"![avatar]({body['url'].strip()})", store, sid,
                                                          avatars.MAX_BYTES)
            if not fetched:
                return JSONResponse({"error": "that link did not give a picture",
                                     "hint": "it has to be the image itself (png, jpeg, gif, webp), not a page about it"},
                                    status_code=400)
            file = fetched[0]["name"]
        if not isinstance(file, str) or not file:
            return JSONResponse({"error": "send {session, file}, {session, url} or {data}"}, status_code=400)
        try:
            if in_chat:
                s = await asyncio.to_thread(wear_in_chat, store, sid, name, file)
                return {"avatars": shelf.all(), "faces": s["settings"].get("faces") or {}}
            worn = await asyncio.to_thread(shelf.wear_file, name, store.file_path(sid, file))
        except avatars.BadAvatar as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return {"avatars": worn}

    @app.delete("/api/avatars/{name}")
    async def avatars_drop(name: str):
        if not avatars.valid(name) or not avatars.Avatars(avatars.root(cfg)).drop(name):
            return JSONResponse({"error": "nobody of that name wears a picture"}, status_code=404)
        return {"avatars": avatars.Avatars(avatars.root(cfg)).all()}

    # ── May I? The claude voice's permission asks ───────────────────────
    # Not a door for anyone to open: an ask needs the ticket of a turn that
    # is running (it rode to the CLI in its environment), and an answer only
    # resolves an ask that is waiting - and both still pass same_origin_only.

    @app.post("/api/permit")
    async def permit_ask(request: Request):
        body = await _json(request)
        desk = request.app.state.permits.get(body.get("ticket")) if isinstance(body, dict) else None
        if desk is None:
            return {"behavior": "deny", "message": "No turn is waiting on that - refused."}
        tool, tool_input = str(body.get("tool_name") or "a tool"), body.get("input") or {}
        rules = claude_voice.suggest_rules(tool, tool_input)
        if any(permissions.covers(r, tool, tool_input) for r in [*desk.always, *desk.allowed]):
            return {"behavior": "allow"}
        pid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        desk.pending[pid] = (fut, rules)
        desk.asked[pid] = {"tool": tool, "summary": claude_voice.summarize(tool, tool_input), "rule": rules[0]}
        # Always comes in widths (narrowest first) and scopes: this chat, its
        # project when it has one, every chat (22 Oct 2026).
        scopes = ["chat", *(["project"] if desk.folder else []), "all"]
        await desk.events.put({"type": "permission", "id": pid, **desk.asked[pid], "rules": rules, "scopes": scopes})
        try:
            return await asyncio.wait_for(fut, timeout=cfg.claude.timeout_seconds)
        except asyncio.TimeoutError:
            return {"behavior": "deny", "message": "Nobody answered in the window."}
        finally:
            desk.pending.pop(pid, None)

    @app.post("/api/permit/answer")
    async def permit_answer(request: Request):
        """{id, answer: once | always | deny, rule?, scope?: chat | project | all} -
        an Always names one of the widths it was offered (else the narrowest)
        and where it is kept: this chat, its project, or every chat (the
        default, as before)."""
        body = await _json(request)
        if not isinstance(body, dict) or body.get("answer") not in ("once", "always", "deny"):
            return JSONResponse({"error": "send {id, answer: once | always | deny}"}, status_code=400)
        scope = body.get("scope") or "all"
        if scope not in ("chat", "project", "all"):
            return JSONResponse({"error": "scope is chat, project or all"}, status_code=400)
        for desk in list(request.app.state.permits.values()):
            fut, rules = desk.pending.get(body.get("id"), (None, []))
            if fut is None or fut.done():
                continue
            rule = body.get("rule") if body.get("rule") in rules else rules[0]
            if scope == "project" and not desk.folder:
                scope = "chat"
            if desk.turn is not None:
                desk.turn.step({"kind": "asked", **desk.asked.get(body["id"], {}), "answer": body["answer"],
                                **({"rule": rule, "scope": scope} if body["answer"] == "always" else {})})
            if body["answer"] == "deny":
                fut.set_result({"behavior": "deny", "message": f"{cfg.ui.you} said no, in the window."})
            else:
                if body["answer"] == "always":
                    desk.always.add(rule)
                    try:
                        keep_always(prefs, cfg, request.app.state.sessions, desk, rule, scope)
                    except (BadPref, Invalid) as ex:
                        log.warning("could not keep %r as always-allowed (%s): %s", rule, scope, ex)
                fut.set_result({"behavior": "allow"})
            # Into the turn's own stream too: a window that follows the turn
            # later sees the ask AND that it was answered, not a card waiting.
            await desk.events.put({"type": "permission_answered", "id": body["id"], "answer": body["answer"],
                                   "rule": rule, "scope": scope})
            return {"ok": True, "answer": body["answer"], "rule": rule, "scope": scope}
        return JSONResponse({"error": "nothing is waiting on that"}, status_code=404)

    @app.get("/api/claude/system")
    async def claude_system_peek(folder: str = ""):
        """What the claude voice would be told in a chat of this folder, as
        the CLI receives it: the preface when there is one, the folder's own
        prompt, the framing. `replace` says whether that is the whole system
        prompt or an addition to the CLI's own. The settings tab shows it,
        so nobody has to guess what reaches the model."""
        preset = cfg.folder(folder)
        system, replace = claude_system(cfg, stack.own_prompt(preset, {}), "<the chat's folder>")
        return {"folder": folder, "replace": replace, "system": system, "chars": len(system)}

    @app.get("/api/stack")
    async def prompt_stack(request: Request, session: str = "", folder: str = "", settings: str = "",
                           seat: str = ""):
        """What a chat's model is given, layer by layer (seren_symposium.stack):
        for `session`, or for `folder` and a JSON `settings` (a chat not made
        yet, or a project's own view with none). Read only: files on this
        box, Margin's preface as a turn reads it, and for a local model
        Lodestar's own /chat/inspect.

        A room has a stack PER SEAT (`seat`, else the first one): each is its
        own conversation in its own folder, so what one is given - its
        folder's memory files and hooks, the tools its CLI loaded, what it
        has yet to hear - is not what the next one is. The answer names the
        `seats` and which `seat` this is."""
        store = request.app.state.sessions
        s: Optional[dict[str, Any]] = None
        own: dict[str, Any] = {}
        at: Optional[dict[str, Any]] = None
        if session:
            s = store.get(session)
            if s is None:
                return JSONResponse({"error": "no such session"}, status_code=404)
            folder, own = s["folder"], s["settings"]
            if s.get("seats"):
                at = room.seat(s, seat) if seat else s["seats"][0]
                if at is None:
                    return JSONResponse({"error": f"no seat called {seat} here"}, status_code=404)
                # The seat's own conversation, start-up report and folder, in the chat's place.
                s = {**{k: v for k, v in s.items() if k not in ("claude", "claude_init")},
                     **{k: at[k] for k in ("claude", "claude_init") if at.get(k)}}
                own = {**own, "voice": "claude", **({"cwd": at["cwd"]} if at.get("cwd") else {}),
                       "seat_name": at["name"], "seat_prompt": at.get("system_prompt") or "",
                       **({"model_override": at["model"]} if at.get("model") else {}),
                       "seat_effort": at.get("effort") or "", "seat_thinking_display": at.get("thinking_display") or ""}
        elif settings:
            try:
                own = json.loads(settings)
            except ValueError:
                return JSONResponse({"error": "settings is JSON"}, status_code=400)
            own = own if isinstance(own, dict) else {}
        preset = cfg.folder(folder)
        voice = own.get("voice") or (preset.voice if preset else "") or "lodestar"
        messages = (s or {}).get("messages") or []
        if voice == "claude":
            out = await asyncio.to_thread(_claude_stack, store, s, folder, preset, own, messages, at)
        else:
            out = await _local_stack(request.app.state.client, folder, preset, own, messages)
        return {"voice": voice, "folder": folder, "session": session or None, "layers": out,
                **({"seat": at["name"], "seats": [x["name"] for x in s["seats"]]} if at else {}),
                **stack.summarize(out)}

    def _claude_stack(store: SessionStore, s: Optional[dict[str, Any]], folder: str,
                      preset: Optional[FolderPreset], own: dict[str, Any],
                      messages: list[dict[str, Any]],
                      seat: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        init, init_from = (s or {}).get("claude_init") or {}, "this chat's last answer"
        if seat is not None:
            init_from = f"{seat['name']}'s last answer"
            if not init:
                # A seat that has yet to answer: the seat beside it loaded much the same.
                beside = next((x for x in (s or {}).get("seats") or [] if x.get("claude_init")), None)
                if beside is not None:
                    init, init_from = beside["claude_init"], f"{beside['name']}'s last answer (the seat beside it)"
                    init = {**init, "cwd": seat.get("cwd") or ""}
        if not init:
            # Before a chat's first answer: the newest another chat here kept,
            # else any chat's - the CLI loads much the same whoever asks.
            near = None
            for row in store.list()[:60]:
                other = store.get(row["id"]) or {}
                if other.get("claude_init"):
                    if other.get("folder") == folder:
                        near = other
                        break
                    near = near or other
            if near is not None:
                init, init_from = near["claude_init"], f"another chat ({near.get('title') or 'untitled'})"
                if seat is not None and seat.get("cwd"):
                    init = {**init, "cwd": seat["cwd"]}     # borrowed, but this seat's folder is its own
        pre = preface.text(cfg.claude.preface)
        files_dir = str(store.files_dir(s["id"])) if s else "<the chat's folder>"
        framing = claude_framing(cfg, files_dir)
        if seat is not None:
            names = [x["name"] for x in (s or {}).get("seats") or []]
            framing = "\n\n".join(p for p in (
                framing, room.framing(cfg.claude.room_framing, names, seat["name"], cfg.ui.you)) if p)
        known = (s or {}).get("claude") or {}
        resumes = bool(known.get("session")) and known.get("seen", 0) <= len(messages)
        handed = messages[known.get("seen", 0):] if resumes else messages
        chars = sum(len(m.get("content") or "") for m in handed)
        turn = {"where": "Claude Code keeps its own copy of the conversation",
                "state": "on" if handed or resumes else "empty",
                "resumes": resumes, "handed": len(handed),
                "chars": min(chars, cfg.claude.handover_max_chars),
                "note": ((f"The next turn resumes the CLI's conversation"
                          + (f" and hands it the {len(handed)} message(s) another voice answered since." if handed
                             else "; nothing is handed over.")) if resumes
                         else (f"The next turn starts a new CLI conversation and is handed the chat so far "
                               f"({len(handed)} message(s), up to claude.handover_max_chars)." if handed
                               else "The next turn starts a new CLI conversation."))}
        perms = permissions.effective(permissions.layers(cfg, preset, own))
        return stack.claude_stack(cfg, preset, folder, own, preface=pre, framing=framing,
                                  init=init, init_from=init_from if init else "", perms=perms, turn=turn)

    async def _local_stack(client: httpx.AsyncClient, folder: str, preset: Optional[FolderPreset],
                           own: dict[str, Any], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        payload = to_lodestar_payload(cfg, {**own, "folder": folder, "prompt": ""})
        inspect, why = None, ""
        try:
            r = await client.post("/api/v1/chat/inspect", timeout=15,
                                  json={k: v for k, v in payload.items() if k in ("system_prompt", "prompt")})
            if r.status_code == 200:
                inspect = r.json()
            else:
                why = f"HTTP {r.status_code}" + (": this Lodestar has no /chat/inspect" if r.status_code == 404 else "")
        except (httpx.HTTPError, ValueError) as ex:
            why = f"{type(ex).__name__}: {ex}"
        kept = _trim_history(messages, cfg.lodestar.history_max_chars)
        chars = sum(len(m.get("content") or "") for m in kept)
        history = {"where": "sent whole on every turn", "chars": chars, "messages": len(kept),
                   "of": len(messages), "state": "on" if kept else "empty",
                   "note": (f"The last {len(kept)} of {len(messages)} message(s), up to "
                            f"lodestar.history_max_chars ({cfg.lodestar.history_max_chars})." if messages
                            else "Nothing yet.")}
        sampling = {k: payload.get(k) for k in ("model_override", "temperature", "max_tokens", "repeat_penalty")}
        return stack.local_stack(cfg, preset, folder, own, inspect=inspect, inspect_error=why,
                                 history=history, sampling=sampling)

    @app.post("/api/projects")
    async def projects_add(request: Request):
        """{name, voice?, system_prompt?, model?, temperature?, max_tokens?, repeat_penalty?} -
        a setting left out takes the new-project defaults; one sent as null
        or blank means no opinion."""
        try:
            preset = prefs.add_project(await _json(request))
        except BadPref as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return JSONResponse(preset.model_dump(), status_code=201)

    @app.patch("/api/projects")
    async def projects_change(request: Request):
        """{name, set?: {setting: value|null}, rename?} - a rename takes the
        project's chats along; into a project that exists, they merge."""
        body = await _json(request)
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            return JSONResponse({"error": "send {name, set?, rename?}"}, status_code=400)
        name, rename = body["name"], body.get("rename")
        if rename is not None and _answering_in(request, name):
            return _busy()
        try:
            preset = prefs.change_project(name, body.get("set") or {}, rename)
        except BadPref as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        moved = request.app.state.sessions.rename_folder(name, preset.name) if preset.name != name else 0
        return {**preset.model_dump(), "moved": moved}

    @app.post("/api/projects/remove")
    async def projects_remove(request: Request):
        """{name} - the project goes; its chats stay, out of any project."""
        body = await _json(request)
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            return JSONResponse({"error": "send {name}"}, status_code=400)
        if _answering_in(request, body["name"]):
            return _busy()
        removed = prefs.remove_project(body["name"])
        moved = request.app.state.sessions.rename_folder(body["name"], "")
        if not removed and not moved:
            return JSONResponse({"error": "no such project"}, status_code=404)
        return {"removed": body["name"], "moved": moved}

    # ── Files ───────────────────────────────────────────────────────────
    # JSON with the bytes in base64, not multipart: every POST here must be
    # application/json (same_origin_only), and that rule is what keeps
    # another page's form out. A third bigger on the wire is the price.

    @app.post("/api/sessions/{sid}/files")
    async def files_add(request: Request, sid: str):
        """{name, data} - data is the file, base64. Answers the entry a
        message names it by."""
        body = await _json(request)
        if not isinstance(body, dict) or not isinstance(body.get("data"), str):
            return JSONResponse({"error": "send {name, data: base64}"}, status_code=400)
        limit = int(cfg.files.max_mb * 1024 * 1024)
        if len(body["data"]) * 3 // 4 > limit:
            return JSONResponse({"error": f"bigger than files.max_mb ({cfg.files.max_mb:g} MB)"},
                                status_code=413)
        try:
            data = base64.b64decode(body["data"], validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse({"error": "data is not base64"}, status_code=400)
        name = body.get("name") if isinstance(body.get("name"), str) else "file"
        entry = request.app.state.sessions.add_file(sid, name, data, attachments.guess_type(name, data[:16]))
        if entry is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        return JSONResponse(entry, status_code=201)

    @app.get("/api/sessions/{sid}/files/{name}")
    async def files_get(request: Request, sid: str, name: str, download: int = 0):
        """A stored file. `?download=1` hands it over to be saved, under the
        name it came with (20 Nov 2026: Chad could see the txt Wren sent and
        not keep it - a file in a chat has to be one you can take away)."""
        path = request.app.state.sessions.file_path(sid, name)
        if path is None:
            return JSONResponse({"error": "no such file"}, status_code=404)
        media, headers = attachments.serve_headers(attachments.guess_type(name, attachments.head(path)))
        if download:
            headers = {**headers, "Content-Disposition": attachments.save_as(name)}
        return FileResponse(path, media_type=media, headers=headers)

    @app.get("/api/sessions/{sid}/files/{name}/settings")
    async def files_settings(request: Request, sid: str, name: str):
        """What made a picture: {name, kept: all|receipt|none, drawn, note}.
        A picture from before its settings were kept answers 200 with
        kept "none" or "receipt" and says so in `note` - it is not missing,
        it just has less to say."""
        s = request.app.state.sessions.get(sid)
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        made = pictures.made_of(s, name)
        if made is None:
            return JSONResponse({"error": "no such file in this chat"}, status_code=404)
        return made

    # ── Rewind, fork, take one out ──────────────────────────────────────
    # Not while Claude is answering: the turn would land on a thread that
    # moved under it.

    # ── pictures: the request panel (seren_symposium.pictures) ────────────
    # Lodestar plans and draws; Symposium asks, shows and keeps. Every one of
    # these is a thin relay with the bearer attached, except draw, which also
    # puts the picture into the chat.

    @app.get("/api/pictures/options")
    async def pictures_options(request: Request, model: str = ""):
        """What can be drawn: the models (how each wants to be prompted),
        the nodes; with ?model=, the LoRAs that fit it."""
        client = request.app.state.client
        try:
            if model:
                r = await client.get("/api/v1/comfy/compatible", params={"model": model}, timeout=150)
            else:
                r = await client.get("/api/v1/comfy/catalog", timeout=150)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        if r.status_code != 200:
            return _relay(r, r.content)
        return r.json() if model else pictures.shape_options(r.json())

    @app.post("/api/pictures/translate")
    async def pictures_translate(request: Request):
        """Words -> a prompt in the chosen model's style, drafted by whoever
        answers the chat: {text, guide, session? | folder?, mode?}. A Claude
        chat asks Claude; a local chat asks its own model, which Lodestar
        routes to the node that model lives on.

        What it is drafted FROM is `mode` (else the chat's `translate`
        setting, else ui.translate): `plain`, the words alone; `chat`, the
        words and the chat's recent messages; `fork`, a throwaway copy of the
        chat's own Claude conversation. A fork needs one Claude conversation
        to copy, so a room, a local model's chat or a chat with nothing said
        yet is given `chat`, and the answer says so (`mode`, `instead_of`)."""
        body = await _json(request)
        if not isinstance(body, dict) or not str(body.get("text") or "").strip():
            return JSONResponse({"error": "say what you want drawn"}, status_code=400)
        store: SessionStore = request.app.state.sessions
        s = store.get(str(body.get("session") or "")) if body.get("session") else None
        settings = (s or {}).get("settings") or {}
        preset = cfg.folder((s or {}).get("folder") or str(body.get("folder") or ""))
        voice = settings.get("voice") or (preset.voice if preset else "") or "lodestar"
        use_claude = voice == "claude" and claude_voice.available(cfg.claude)
        local_model = "" if use_claude else (settings.get("model_override") or (preset.model if preset else "") or "")
        claude_model = settings.get("model_override") if use_claude and preset and settings.get("voice") == "claude" else ""
        text, guide = str(body["text"]).strip(), str(body.get("guide") or "")
        asked = body.get("mode") or settings.get("translate") or cfg.ui.translate
        if asked not in TRANSLATE_MODES:
            return JSONResponse({"error": f"mode is one of {', '.join(TRANSLATE_MODES)}"}, status_code=400)
        mode, why = asked, ""
        if mode == "fork":
            known = (s or {}).get("claude") or {}
            why = ("a room has a conversation per seat, not one to copy" if (s or {}).get("seats")
                   else "only a Claude chat has a conversation to copy" if not use_claude
                   else "" if known.get("session") else "this chat has no Claude conversation yet")
            if why:
                mode = "chat"
        if mode == "fork":
            # The copy is given what a turn here is given - the same system
            # prompt, model, folders and place - so it IS this conversation and
            # the CLI's cache of it is reused. Not its permissions: nothing is
            # allowed, asks go to a ticket no one holds (refused), and a tool
            # reached for stops it (claude_voice.side_copy).
            plan = plan_claude(cfg, store, s, {**settings, "folder": s["folder"]}, s["messages"], "")
            turn = {"resume": known["session"], "model": plan["model"], "system_prompt": plan["system_prompt"],
                    "replace": bool(plan["system_replace"]), "add_dirs": plan["add_dirs"], "allow": [],
                    "mode": "default", "own_tools": False, "cwd": plan["cwd"],
                    "permit": ({"url": _permit_url(cfg), "ticket": f"side-copy-{uuid.uuid4().hex}"}
                               if cfg.claude.ask else None)}
            out = await pictures.translate_fork(cfg.claude, turn, text, guide, cfg.ui.you)
        else:
            context = ""
            if mode == "chat" and s:
                context = pictures.chat_context(s["messages"], cfg.ui.you, cfg.claude.name if use_claude else "assistant",
                                                cfg.ui.translate_messages, cfg.ui.translate_max_chars)
            if mode == "chat" and not context:
                mode = "plain"                          # nothing has been said here yet: the words are all there is
            out = await pictures.translate(request.app.state.client, text, guide,
                                           model_override=str(local_model or ""), claude=cfg.claude if use_claude else None,
                                           claude_model=str(claude_model or "") if use_claude else "", context=context)
        return JSONResponse({**out, "voice": "claude" if use_claude else "lodestar", "mode": mode,
                             **({"instead_of": asked, "why": why} if why else {})},
                            status_code=200 if out.get("ok") else 502)

    @app.post("/api/pictures/plan")
    async def pictures_plan(request: Request):
        """What would be drawn, and where, without drawing: Lodestar's plan."""
        body = await _json(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "a JSON body"}, status_code=400)
        try:
            r = await request.app.state.client.post("/api/v1/image/plan", json=body, timeout=150)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        if r.status_code != 200:
            return _relay(r, r.content)
        return r.json()

    @app.post("/api/pictures/draw")
    async def pictures_draw(request: Request):
        """Draw it, and keep it in the chat. {session, asked, why, request:
        {prompt, negative?, model?, loras?, width?, height?, seed?, count?} or
        {workflow, node}}. Without a session the pictures come back base64
        and are kept nowhere."""
        body = await _json(request)
        if not isinstance(body, dict) or not isinstance(body.get("request"), dict):
            return JSONResponse({"error": "a JSON body with a request"}, status_code=400)
        store: SessionStore = request.app.state.sessions
        sid = str(body.get("session") or "")
        if sid and store.get(sid) is None:
            return JSONResponse({"error": f"no session '{sid}'"}, status_code=404)
        req = {**body["request"], "holder": "symposium", "save_as": "symposium"}
        try:
            r = await request.app.state.client.post("/api/v1/image", json=req, timeout=pictures.DRAW_TIMEOUT)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        if r.status_code != 200:
            return _relay(r, r.content)
        out = r.json()
        asked = str(body.get("asked") or req.get("prompt") or "").strip()
        why = str(body.get("why") or "").strip()
        if not sid:
            return {k: v for k, v in out.items()}
        kept = await asyncio.to_thread(pictures.keep_drawn, store, sid, asked, why, out, body["request"])
        if kept is None:
            return JSONResponse({"error": "the node finished but sent no picture back", **{k: v for k, v in out.items() if k != "images"}},
                                status_code=502)
        return {"ok": True, "session": sid, "files": kept["files"], "answer": kept["answer"], "meta": kept["meta"],
                **{k: v for k, v in out.items() if k not in ("images", "ok")}}

    @app.get("/api/pictures/gallery")
    async def pictures_gallery(request: Request):
        try:
            r = await request.app.state.client.get("/api/v1/gallery", timeout=60)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        return r.json() if r.status_code == 200 else _relay(r, r.content)

    @app.get("/api/pictures/gallery/{node}/{day}")
    async def pictures_gallery_day(request: Request, node: str, day: str):
        try:
            r = await request.app.state.client.get(f"/api/v1/gallery/{node}/{day}", timeout=60)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        return r.json() if r.status_code == 200 else _relay(r, r.content)

    @app.get("/api/pictures/gallery/{node}/{day}/{name}")
    async def pictures_gallery_file(request: Request, node: str, day: str, name: str):
        """One gallery picture at full size, streamed through."""
        client = request.app.state.client
        try:
            r = await client.send(client.build_request("GET", f"/api/v1/gallery/{node}/{day}/{name}", timeout=300),
                                  stream=True)
        except Exception as ex:  # noqa: BLE001
            return _unreachable(cfg, ex)
        if r.status_code != 200:
            body = await r.aread()
            await r.aclose()
            return _relay(r, body)

        async def stream_body():
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
            finally:
                await r.aclose()
        return StreamingResponse(stream_body(), media_type=r.headers.get("content-type") or "application/octet-stream",
                                 headers={"X-Content-Type-Options": "nosniff",
                                          "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:",
                                          "Cache-Control": "private, max-age=3600"})

    async def _change(request: Request, sid: str, act, needs_body: bool = True):
        if sid in request.app.state.answering:
            return _busy()
        body = await _json(request) if needs_body else {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "send a JSON object"}, status_code=400)
        try:
            s = act(request.app.state.sessions, body)
        except Invalid as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return s if s is not None else JSONResponse({"error": "no such session"}, status_code=404)

    @app.post("/api/sessions/{sid}/rewind")
    async def sessions_rewind(request: Request, sid: str):
        """{to} - keep the first `to` messages. An edit is a rewind to just
        before the message, and the message sent again."""
        return await _change(request, sid, lambda store, body: store.rewind(sid, body.get("to")))

    @app.post("/api/sessions/{sid}/fork")
    async def sessions_fork(request: Request, sid: str):
        """{at} - a new chat holding the first `at` messages."""
        out = await _change(request, sid, lambda store, body: store.fork(sid, body.get("at")))
        return JSONResponse(out, status_code=201) if isinstance(out, dict) else out

    @app.delete("/api/sessions/{sid}/messages/{index}")
    async def sessions_drop(request: Request, sid: str, index: int):
        return await _change(request, sid, lambda store, body: store.drop_message(sid, index),
                             needs_body=False)

    # ── Screen share ────────────────────────────────────────────────────

    @app.get("/api/screen/sources")
    async def screen_sources(request: Request):
        """What can be shared: the displays (`primary` marks the main one, the
        default) and, on Windows, the open windows - asked fresh each time."""
        if not cfg.screen.enabled:
            return {"monitors": [], "windows": [], "primary": 1}
        mons, wins = await asyncio.gather(asyncio.to_thread(screen.monitors), asyncio.to_thread(screen.windows))
        return {"monitors": mons, "windows": wins, "primary": screen.primary(mons)}

    @app.post("/api/screen")
    async def screen_take(request: Request):
        """{monitor?} or {window?} - a still of a display or of one window,
        kept as the newest frame; a message sent with `screen: true` carries it."""
        if not cfg.screen.enabled:
            return JSONResponse({"error": "screen share is off (screen.enabled)"}, status_code=409)
        body = await _json(request)
        body = body if isinstance(body, dict) else {}
        monitor, window = body.get("monitor", 1), body.get("window")
        if isinstance(monitor, bool) or not isinstance(monitor, int):
            return JSONResponse({"error": "monitor is a number"}, status_code=400)
        if window is not None and (isinstance(window, bool) or not isinstance(window, int)):
            return JSONResponse({"error": "window is a number"}, status_code=400)
        try:
            # A grab and a JPEG encode: off the loop, so a stream in flight keeps flowing.
            frame = await asyncio.to_thread(screen.capture, monitor, cfg.screen.max_width,
                                            cfg.screen.jpeg_quality, window)
        except RuntimeError as ex:
            return JSONResponse({"error": str(ex)}, status_code=409)
        request.app.state.screen = frame
        return frame.info()

    @app.get("/api/screen/latest")
    async def screen_latest(request: Request):
        frame = request.app.state.screen
        if frame is None:
            return JSONResponse({"error": "no frame yet"}, status_code=404)
        return Response(frame.jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    # ── ears: push-to-talk (seren_symposium.ears) ─────────────────────────
    # The microphone is recorded here; the clip is kept beside the chat and
    # its words come from whisper through Lodestar (POST /api/v1/hear), the
    # one place Symposium talks to.

    def _ears_off() -> Optional[JSONResponse]:
        if not cfg.ears.enabled:
            return JSONResponse({"error": "ears are off (ears.enabled)"}, status_code=409)
        if not ears.available():
            return JSONResponse({"error": "no sounddevice on this machine",
                                 "hint": "pip install \"seren-symposium[ears]\""}, status_code=409)
        return None

    @app.get("/api/ears")
    async def ears_state(request: Request):
        """What there is to listen with, and whether it is listening now."""
        listing = await asyncio.to_thread(ears.devices) if cfg.ears.enabled else []
        return {"enabled": cfg.ears.enabled, "available": ears.available(), "device": cfg.ears.device,
                "devices": listing, **request.app.state.ears.state()}

    @app.post("/api/ears/start")
    async def ears_start(request: Request):
        """{device?: index} - open the input (the yaml's, else the default)."""
        if (off := _ears_off()) is not None:
            return off
        body = await _json(request)
        body = body if isinstance(body, dict) else {}
        device = body.get("device")
        if device is not None and (isinstance(device, bool) or not isinstance(device, int)):
            return JSONResponse({"error": "device is a number"}, status_code=400)
        if device is None:
            device = await asyncio.to_thread(ears.pick, cfg.ears.device)
        try:
            return await asyncio.to_thread(request.app.state.ears.start, device)
        except RuntimeError as ex:
            return JSONResponse({"error": str(ex)}, status_code=409)

    @app.post("/api/ears/stop")
    async def ears_stop(request: Request):
        """{session, keep?: true, transcribe?: true} - close the input. The
        clip is kept in the chat as a file marked `ears` (when `keep`), and
        its words come back as `text`; a Lodestar that could not hear it
        still leaves the clip, with `error` saying why."""
        rec = request.app.state.ears
        if not rec.recording:
            return JSONResponse({"error": "not listening"}, status_code=409)
        body = await _json(request)
        body = body if isinstance(body, dict) else {}
        clip = await asyncio.to_thread(rec.stop)
        out: dict[str, Any] = {"seconds": clip.seconds, "device": clip.device, "clipped": clip.clipped, "text": ""}
        if body.get("keep", True):
            store: SessionStore = request.app.state.sessions
            sid = body.get("session")
            entry = store.add_file(sid, time.strftime("ears-%Y%m%d-%H%M%S.wav", time.localtime(clip.at)),
                                   clip.wav, "audio/wav") if isinstance(sid, str) else None
            if entry is None:
                return JSONResponse({"error": "no such session", "hint": "the clip needs a chat to be kept in",
                                     **out}, status_code=404)
            entry.update(ears=True, seconds=clip.seconds, device=clip.device)
            out["file"] = entry
        if not body.get("transcribe", True):
            return out
        if clip.seconds < 0.3:
            out["error"] = "too short to hear"
            return out
        try:
            r = await request.app.state.client.post(
                "/api/v1/hear", files={"file": ("clip.wav", clip.wav, "audio/wav")},
                data={"language": cfg.ears.language, "holder": "symposium"}, timeout=cfg.ears.timeout_seconds)
            try:
                heard = r.json()
            except ValueError:
                heard = {}
            if r.status_code == 200 and isinstance(heard, dict) and heard.get("ok"):
                out["text"] = str(heard.get("text") or "").strip()
                out["node"] = heard.get("node")
            else:
                said = heard if isinstance(heard, dict) else {}
                out["error"] = said.get("error") or f"lodestar answered {r.status_code}"
                if r.status_code == 401:
                    out.update(_annotate_401({}))
        except httpx.HTTPError as ex:
            out["error"] = f"lodestar unreachable: {type(ex).__name__}"
        return out

    @app.post("/api/ears/cancel")
    async def ears_cancel(request: Request):
        """Close the input and keep nothing."""
        await asyncio.to_thread(request.app.state.ears.cancel)
        return {"ok": True, "recording": False}

    # ── speech: an answer said aloud ───────────────────────────────────────
    # The words of one answer go to Lodestar's speaker (`POST /api/v1/speak`:
    # the chosen voice on a node, woken and let go like whisper) and the clip
    # comes back to sit with that answer as a file marked `said`, playable in
    # the chat. Symposium never talks to the node itself.

    @app.post("/api/say")
    async def say(request: Request):
        """{session, index} - say that answer aloud. Answers with the file
        the clip became (marked `said`, with the voice and how long) and
        `cut` when the answer was longer than speech.max_chars."""
        if not cfg.speech.enabled:
            return JSONResponse({"error": "speech is off (speech.enabled)"}, status_code=409)
        body = await _json(request)
        body = body if isinstance(body, dict) else {}
        store: SessionStore = request.app.state.sessions
        sid, index = body.get("session"), body.get("index")
        s = store.get(sid) if isinstance(sid, str) else None
        if s is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(s["messages"]):
            return JSONResponse({"error": f"no message {index!r}"}, status_code=404)
        m = s["messages"][index]
        if m.get("role") != "assistant":
            return JSONResponse({"error": "only an answer is said aloud"}, status_code=400)
        text, cut = spoken_text(m.get("content") or "", cfg.speech.max_chars)
        if not text:
            return JSONResponse({"error": "nothing to say in that answer"}, status_code=400)
        try:
            r = await request.app.state.client.post(
                "/api/v1/speak", json={"text": text, "voice": cfg.speech.voice or "wren", "holder": "symposium"},
                timeout=cfg.speech.timeout_seconds)
        except httpx.HTTPError as ex:
            return JSONResponse({"error": f"lodestar unreachable: {type(ex).__name__}"}, status_code=502)
        if r.status_code != 200 or not r.content.startswith(b"RIFF"):
            try:
                said = r.json()
            except ValueError:
                said = {}
            said = said if isinstance(said, dict) else {}
            out = {"error": said.get("error") or f"lodestar answered {r.status_code}"}
            if said.get("detail"):
                out["detail"] = said["detail"]
            if r.status_code == 401:
                out.update(_annotate_401({}))
            return JSONResponse(out, status_code=502 if r.status_code not in (503, 401) else r.status_code)
        voice = r.headers.get("x-seren-voice") or cfg.speech.voice or "wren"
        try:
            seconds = float(r.headers.get("x-seren-clip-seconds") or 0)
        except ValueError:
            seconds = 0.0
        entry = store.add_file(sid, time.strftime("said-%Y%m%d-%H%M%S.wav"), r.content, "audio/wav")
        if entry is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        entry.update(said=True, voice=voice, seconds=seconds)
        store.attach_file(sid, index, entry)
        return {"file": entry, "cut": cut, "node": r.headers.get("x-seren-node"), "voice": voice, "seconds": seconds}

    return app


def keep_always(prefs: Prefs, cfg: SymposiumConfig, store: SessionStore, desk: PermitDesk,
                rule: str, scope: str) -> None:
    """An Always, kept where it was asked to be: the chat's own list, its
    project's, or every chat's (Settings > Claude)."""
    if scope == "chat":
        s = store.get(desk.session)
        tools = list((s or {}).get("settings", {}).get("allowed_tools") or [])
        if s is not None and rule not in tools:
            store.update(desk.session, {"settings": {"allowed_tools": tools + [rule]}})
    elif scope == "project" and desk.folder:
        preset = cfg.folder(desk.folder)
        tools = list(preset.allowed_tools) if preset else []
        if rule not in tools:
            prefs.change_project(desk.folder, {"allowed_tools": tools + [rule]})
    else:
        prefs.allow_tool(rule)
