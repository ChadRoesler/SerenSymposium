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
    GET    /health                liveness, and whether Lodestar is reachable
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from seren_meninges import get_version
from seren_meninges.viewer import render_from_dir

from . import __version__ as _fallback_version
from . import claude_voice
from .config import SymposiumConfig, load_config
from .sessions import Invalid, SessionStore, summary

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
                 claude: Optional[dict[str, Any]] = None):
        self.payload, self.session, self.prompt, self.keep = payload, session, prompt, keep
        self.claude = claude
        self.started = time.monotonic()

    def receipt(self, reply: dict[str, Any], tools: Optional[list[str]] = None) -> dict[str, Any]:
        """What sits under an answer: who said it, where, and what it reached for."""
        meta = {k: reply[k] for k in ("model", "node", "tool_rounds") if reply.get(k) is not None}
        if tools:
            meta["tools"] = tools
        meta["seconds"] = round(time.monotonic() - self.started, 1)
        return meta


def plan_claude(cfg: SymposiumConfig, s: dict[str, Any], layered: dict[str, Any],
                messages: list[dict[str, Any]], prompt: str) -> dict[str, Any]:
    """What the claude CLI is asked, for a turn in session `s`.

    The CLI keeps its own copy of the conversation, so no history goes on the
    wire - except the messages this conversation never saw (all of them on
    its first turn, or those another voice answered since), handed over in
    front of the prompt. The folder's model only counts when the folder's
    voice is claude too: a local model's name means nothing to the CLI."""
    preset = cfg.folder(s["folder"])
    known = s.get("claude") or {}
    resume = known.get("session")
    missed = messages[known.get("seen", 0):] if resume else messages
    model = layered.get("model_override") or (
        preset.model if preset and preset.voice == "claude" else "")
    return {
        "prompt": claude_voice.handover(_trim_history(missed, cfg.lodestar.history_max_chars),
                                        prompt, resumed=bool(resume)),
        "resume": resume, "model": model,
        "system_prompt": layered.get("system_prompt") or (preset.system_prompt if preset else ""),
    }


def plan_turn(cfg: SymposiumConfig, store: SessionStore, body: Any) -> "Turn | JSONResponse":
    """The request -> a Turn, or the error to answer with.

    Without `session` it is the old stateless shape: the shell sends its own
    `history`. With one, the thread and the settings come from the session
    file, and the answer is written back to it. `retry` asks the last prompt
    again; the exchange it replaces is only dropped once the new one is in.

    The voice - who answers - layers like any setting: the session's, else
    the folder's, else lodestar. Claude needs a session to keep its
    conversation in.
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
    if body.get("retry"):
        last = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user"), None)
        if last is None:
            return JSONResponse({"error": "nothing to retry"}, status_code=400)
        prompt, keep, messages = messages[last]["content"], last, messages[:last]
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    layered = {**s["settings"],
               **{k: v for k, v in body.items() if k not in ("session", "retry", "history")},
               "prompt": prompt, "folder": s["folder"], "history": messages}
    preset = cfg.folder(s["folder"])
    voice = layered.get("voice") or (preset.voice if preset else "") or "lodestar"
    if voice == "claude":
        if not claude_voice.available(cfg.claude):
            return JSONResponse({"error": "the claude voice is not available here",
                                 "hint": "claude.enabled is off, or claude.command is not "
                                         "on this machine's PATH"}, status_code=409)
        return Turn({}, sid, prompt, keep, claude=plan_claude(cfg, s, layered, messages, prompt))
    return Turn(to_lodestar_payload(cfg, layered), sid, prompt, keep)


async def claude_answer(cfg: SymposiumConfig, store: SessionStore, turn: Turn,
                        busy: set[str]):
    """A claude turn, as stream events, written to the session when it
    comes back - then `saved`, as after a Lodestar turn.

    One turn at a time per session: two at once would both resume the same
    CLI conversation, and which answer it remembers would be a race."""
    if turn.session in busy:
        yield {"type": "error", "error": "this chat is still answering the last message"}
        return
    busy.add(turn.session)
    try:
        ask = turn.claude
        heard = claude_voice.ClaudeTurn(ask["resume"] or str(uuid.uuid4()))
        async for event in claude_voice.events(cfg.claude, prompt=ask["prompt"], resume=ask["resume"],
                                               model=ask["model"], system_prompt=ask["system_prompt"],
                                               turn=heard):
            yield event
            if event["type"] == "done":
                saved = store.record(turn.session, turn.prompt, event.get("response") or "",
                                     turn.receipt(event, heard.tools), keep=turn.keep,
                                     claude_session=heard.session_id)
                if saved is not None:
                    yield {"type": "saved", "session": summary(saved)}
    finally:
        busy.discard(turn.session)


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg
        app.state.sessions = SessionStore(cfg.sessions_dir())
        app.state.answering = set()     # sessions with a claude turn running
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
            return {"ok": True, "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": False, "status": "unreachable",
                "detail": f"{type(ex).__name__}: {ex}"}}

        if r.status_code != 200:
            return {"ok": True, "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": False, "status": "unreachable",
                "detail": f"HTTP {r.status_code}"}}

        try:
            chat_health = await client.get("/api/v1/chat/health")
        except Exception as ex:  # noqa: BLE001
            # The head answered a moment ago, so this is a flap rather than a
            # cold service. Say so instead of claiming it is down.
            return {"ok": True, "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "unreachable",
                "detail": f"chat backend probe failed - {type(ex).__name__}: {ex}"}}

        if chat_health.status_code == 401:
            return {"ok": True, "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "unauthorized",
                "detail": "Lodestar rejected our bearer token - check "
                          "lodestar.bearer_token in seren-symposium.yaml"}}

        if chat_health.status_code != 200:
            return {"ok": True, "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "no_inference",
                "detail": f"chat backend returned HTTP {chat_health.status_code}"}}

        payload = chat_health.json()
        if not payload.get("ok"):
            return {"ok": True, "version": APP_VERSION, "lodestar": {
                "url": url, "reachable": True, "status": "no_inference",
                "detail": payload.get("reason") or "no node is serving llama"}}

        node = payload.get("node")
        return {"ok": True, "version": APP_VERSION, "lodestar": {
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
        turn = plan_turn(cfg, store, await _json(request))
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
            store.record(turn.session, turn.prompt, payload.get("response") or "",
                         turn.receipt(payload), keep=turn.keep)
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
        turn = plan_turn(cfg, store, await _json(request))
        if isinstance(turn, JSONResponse):
            return turn
        if turn.claude is not None:
            async def spoken():
                async for event in claude_answer(cfg, store, turn, request.app.state.answering):
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
                        saved = store.record(turn.session, turn.prompt, event.get("response") or "",
                                             turn.receipt(event, tools), keep=turn.keep)
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
        if not request.app.state.sessions.delete(sid):
            return JSONResponse({"error": "no such session"}, status_code=404)
        return {"ok": True}

    return app
