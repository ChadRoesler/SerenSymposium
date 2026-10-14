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
    POST   /api/sessions/{id}/files         attach a file (JSON, base64) - GET .../files/{name} reads it
    PATCH  /api/prefs             your name, the claude voice's settings, confirm - kept beside the chats
    GET    /api/claude            the claude voice's settings and the carabiner clipped on this box
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
from . import attachments, carabiner, claude_voice, screen
from .config import SymposiumConfig, load_config
from .prefs import Invalid as BadPref
from .prefs import Prefs
from .sessions import Invalid, SessionStore, file_entry, summary

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


def _trim_history(history: Any, max_chars: int) -> list[dict[str, Any]]:
    """Keep the newest whole messages that fit in max_chars, oldest dropped first.

    Whole messages, never partial ones: half a turn reads as the model having
    been told something it was not. Dropping from the oldest end is what makes
    a long session degrade into a short-memory one instead of failing outright.
    Only role and content go on: a session's timestamps and receipts are ours.
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
        kept.append({"role": message.get("role"), "content": message.get("content", "")})
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
    if system_prompt := (body.get("system_prompt") or (preset.system_prompt if preset else "")):
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
    told = [{**m, "content": attachments.with_files(m.get("content", ""),
                                                    attachments.for_claude(m.get("files") or [], folder))}
            for m in missed]
    model = layered.get("model_override") or (
        preset.model if preset and preset.voice == "claude" else "")
    system = layered.get("system_prompt") or (preset.system_prompt if preset else "")
    if cfg.claude.framing:
        system = "\n\n".join(p for p in (system, cfg.claude.framing.replace("{files}", str(folder))) if p)
    return {
        "prompt": claude_voice.handover(_trim_history(told, cfg.claude.handover_max_chars),
                                        attachments.with_files(prompt, attachments.for_claude(files or [], folder)),
                                        resumed=bool(resume)),
        "resume": resume, "model": model, "system_prompt": system,
        "add_dirs": [str(folder)],
        "allow": [claude_voice.folder_rule(str(folder))] if cfg.claude.save_files else [],
    }


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
               **{k: v for k, v in body.items() if k not in ("session", "retry", "history", "files", "screen")},
               "prompt": prompt, "folder": s["folder"], "history": messages}
    preset = cfg.folder(s["folder"])
    voice = layered.get("voice") or (preset.voice if preset else "") or "lodestar"
    if voice == "claude":
        if not claude_voice.available(cfg.claude):
            return JSONResponse({"error": "the claude voice is not available here",
                                 "hint": "claude.enabled is off, or claude.command is not "
                                         "on this machine's PATH"}, status_code=409)
        return Turn({}, sid, prompt, keep, files=files,
                    claude=plan_claude(cfg, store, s, layered, messages, prompt, files))
    layered["prompt"] = attachments.with_files(
        prompt, attachments.for_local(files, store, sid, cfg.files.inline_max_chars))
    return Turn(to_lodestar_payload(cfg, layered), sid, prompt, keep, files=files)


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


class PermitDesk:
    """One claude turn's may-I questions: the CLI's permission tool asks
    (POST /api/permit), the ask goes into the turn's stream as a
    `permission` event, and waits here for your answer (/api/permit/answer)."""

    def __init__(self, session: str):
        self.session = session
        self.ticket = secrets.token_urlsafe(24)
        self.events: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, tuple[asyncio.Future, str]] = {}
        self.asked: dict[str, dict[str, Any]] = {}      # id -> the ask, for the turn's steps
        self.always: set[str] = set()
        self.turn: Optional[claude_voice.ClaudeTurn] = None

    def close(self, why: str) -> None:
        for fut, _ in self.pending.values():
            if not fut.done():
                fut.set_result({"behavior": "deny", "message": why})
        self.pending.clear()


def _permit_url(cfg: SymposiumConfig) -> str:
    host = cfg.ui.host if cfg.ui.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    return f"http://{f'[{host}]' if ':' in host else host}:{cfg.ui.port}/api/permit"


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
    desk = PermitDesk(turn.session) if permits is not None and cfg.claude.ask else None
    if desk is not None:
        permits[desk.ticket] = desk
    try:
        ask = turn.claude
        heard = claude_voice.ClaudeTurn(ask["resume"] or str(uuid.uuid4()))
        if desk is not None:
            desk.turn = heard
        # Made before the CLI is pointed at it: --add-dir wants a folder that is there.
        store.files_dir(turn.session).mkdir(parents=True, exist_ok=True)
        before = store.listing(turn.session)
        spoken = claude_voice.events(cfg.claude, prompt=ask["prompt"], resume=ask["resume"],
                                     model=ask["model"], system_prompt=ask["system_prompt"],
                                     add_dirs=ask.get("add_dirs"), allow=ask.get("allow"), turn=heard,
                                     permit={"url": _permit_url(cfg), "ticket": desk.ticket} if desk else None)
        async for event in (_with_asks(spoken, desk) if desk else spoken):
            if event["type"] == "done":
                event["response"], found = await bring_back(cfg, store, turn, event.get("response") or "", before)
            yield event
            if event["type"] == "done":
                saved = store.record(turn.session, turn.prompt, event["response"],
                                     turn.receipt(event, heard.tools, heard.kept_steps()), keep=turn.keep,
                                     claude_session=heard.session_id, files=turn.files,
                                     answer_files=found)
                if saved is not None:
                    yield {"type": "saved", "session": summary(saved)}
    finally:
        busy.discard(turn.session)
        if desk is not None:
            permits.pop(desk.ticket, None)
            desk.close("the turn ended before anyone answered")


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
        desk.close("the window stopped listening")


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
        app.state.answering = set()     # sessions with a claude turn running
        app.state.screen = None         # the newest screen-share frame (screen.Frame)
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

        yield
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
            "claude": {"name": cfg.claude.name, "available": claude_voice.available(cfg.claude)},
            "you": cfg.ui.you,
            "prefs_file": str(prefs.path),
            "files": {"max_mb": cfg.files.max_mb, "fetch_images": cfg.files.fetch_images},
            "screen": {"enabled": cfg.screen.enabled, "available": screen.available(),
                       "monitors": screen.monitors() if cfg.screen.enabled else []},
            "confirm_destructive": cfg.ui.confirm_destructive,
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
        if turn.claude is not None:
            out: dict[str, Any] = {}
            async for event in claude_answer(cfg, store, turn, request.app.state.answering):
                if event["type"] in ("done", "error"):
                    out = event
            if out.get("type") != "done":
                return JSONResponse({k: v for k, v in out.items() if k != "type"},
                                    status_code=409 if "still answering" in out.get("error", "") else 502)
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
                         turn.receipt(payload), keep=turn.keep, files=turn.files, answer_files=found)
            payload["session"] = turn.session
        return JSONResponse(payload)

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request):
        """Lodestar's /chat/stream, line for line: `chunk` as the words come,
        `tool_status` before a tool runs, then `done` (the whole answer) or
        `error`. Nothing is renamed. The one line that is ours comes last:
        `{"type": "saved", "session": {...}}` once a session has the turn.
        A claude turn speaks the same lines (claude_voice translates)."""
        store: SessionStore = request.app.state.sessions
        turn = plan_turn(cfg, store, await _json(request), request.app.state.screen)
        if isinstance(turn, JSONResponse):
            return turn
        if turn.claude is not None:
            async def spoken():
                async for event in claude_answer(cfg, store, turn, request.app.state.answering,
                                                 request.app.state.permits):
                    yield json.dumps(event) + "\n"
            return StreamingResponse(spoken(), media_type="application/x-ndjson",
                                     headers={"Cache-Control": "no-cache"})
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

        async def lines():
            tools: list[str] = []
            try:
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    yield line + "\n"
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "tool_status" and event.get("tool"):
                        tools.append(event["tool"])
                    elif event.get("type") == "done" and turn.session:
                        kept, found = await bring_back(cfg, store, turn, event.get("response") or "")
                        saved = store.record(turn.session, turn.prompt, kept,
                                             turn.receipt(event, tools), keep=turn.keep,
                                             files=turn.files, answer_files=found)
                        if saved is not None:
                            yield json.dumps({"type": "saved", "session": summary(saved)}) + "\n"
            except httpx.HTTPError as ex:
                yield json.dumps({"type": "error", "error": "lodestar stopped mid-answer",
                                  "detail": f"{type(ex).__name__}: {ex}"}) + "\n"
            finally:
                await r.aclose()

        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache"})

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
        return {"sessions": request.app.state.sessions.list()}

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
        return s if s is not None else JSONResponse({"error": "no such session"}, status_code=404)

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

    @app.delete("/api/sessions/{sid}")
    async def sessions_delete(request: Request, sid: str):
        if sid in request.app.state.answering:
            return _busy()
        if not request.app.state.sessions.delete(sid):
            return JSONResponse({"error": "no such session"}, status_code=404)
        return {"ok": True}

    # ── What the window sets: the app's settings, the projects ──────────
    # Kept in the prefs file, never the yaml: a reinstall writes the yaml.

    def _answering_in(request: Request, folder: str) -> bool:
        return any(s["folder"] == folder and s["id"] in request.app.state.answering
                   for s in request.app.state.sessions.list())

    @app.patch("/api/prefs")
    async def prefs_change(request: Request):
        """{you?, claude_name?, confirm_destructive?}"""
        try:
            prefs.set_app(await _json(request))
        except BadPref as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return {"you": cfg.ui.you, "claude_name": cfg.claude.name,
                "confirm_destructive": cfg.ui.confirm_destructive}

    @app.get("/api/claude")
    async def claude_settings(request: Request):
        """The claude voice's settings as they stand, and the carabiner on
        this box (never its tokens)."""
        c = cfg.claude
        return {"available": claude_voice.available(c), "name": c.name, "cwd": c.cwd,
                "mode": c.permission_mode, "ask": c.ask, "tools": c.allowed_tools, "dirs": c.add_dirs,
                "carabiner": await asyncio.to_thread(carabiner.describe, c)}

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
        rule = claude_voice.suggest_rule(tool, tool_input)
        if rule in desk.always or tool in desk.always:
            return {"behavior": "allow"}
        pid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        desk.pending[pid] = (fut, rule)
        desk.asked[pid] = {"tool": tool, "summary": claude_voice.summarize(tool, tool_input), "rule": rule}
        await desk.events.put({"type": "permission", "id": pid, **desk.asked[pid]})
        try:
            return await asyncio.wait_for(fut, timeout=cfg.claude.timeout_seconds)
        except asyncio.TimeoutError:
            return {"behavior": "deny", "message": "Nobody answered in the window."}
        finally:
            desk.pending.pop(pid, None)

    @app.post("/api/permit/answer")
    async def permit_answer(request: Request):
        """{id, answer: once | always | deny}"""
        body = await _json(request)
        if not isinstance(body, dict) or body.get("answer") not in ("once", "always", "deny"):
            return JSONResponse({"error": "send {id, answer: once | always | deny}"}, status_code=400)
        for desk in list(request.app.state.permits.values()):
            fut, rule = desk.pending.get(body.get("id"), (None, ""))
            if fut is None or fut.done():
                continue
            if desk.turn is not None:
                desk.turn.step({"kind": "asked", **desk.asked.get(body["id"], {}), "answer": body["answer"]})
            if body["answer"] == "deny":
                fut.set_result({"behavior": "deny", "message": f"{cfg.ui.you} said no, in the window."})
            else:
                if body["answer"] == "always":
                    desk.always.add(rule)
                    try:
                        prefs.allow_tool(rule)
                    except BadPref as ex:
                        log.warning("could not keep %r as always-allowed: %s", rule, ex)
                fut.set_result({"behavior": "allow"})
            return {"ok": True, "answer": body["answer"], "rule": rule}
        return JSONResponse({"error": "nothing is waiting on that"}, status_code=404)

    @app.post("/api/projects")
    async def projects_add(request: Request):
        """{name, voice?, system_prompt?, model?, temperature?, max_tokens?, repeat_penalty?}"""
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
    async def files_get(request: Request, sid: str, name: str):
        path = request.app.state.sessions.file_path(sid, name)
        if path is None:
            return JSONResponse({"error": "no such file"}, status_code=404)
        media, headers = attachments.serve_headers(attachments.guess_type(name, attachments.head(path)))
        return FileResponse(path, media_type=media, headers=headers)

    # ── Rewind, fork, take one out ──────────────────────────────────────
    # Not while Claude is answering: the turn would land on a thread that
    # moved under it.

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

    return app
