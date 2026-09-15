"""
seren_symposium.app
════════════════════════════════════════════════════════════════════════

A LOOPBACK SHIM. Symposium serves its own UI on 127.0.0.1 and proxies chat to
Lodestar with the bearer attached.

WHY A LOCAL SERVER AND NOT JUST JS -> LODESTAR:
    Two reasons, both practical rather than architectural. The bearer stays in
    Python instead of being handed to a webview's JavaScript, and Lodestar
    never needs CORS opened for a desktop app. The cost is one loopback port.

WHAT IT DOES NOT DO:
    No inbound auth. It binds 127.0.0.1 only - anything reaching it is already
    on the machine. If you ever widen ui.host beyond loopback you have built a
    different, unauthenticated service; don't.

Serves:
    GET  /              - the shell (chat + tabs pointing at sibling viewers)
    GET  /api/config    - what the UI needs to render: viewers, folders, version
    POST /api/chat      - proxied to Lodestar, bearer injected
    GET  /health        - liveness, and whether Lodestar is reachable
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from seren_meninges import get_version

from . import __version__ as _fallback_version
from .config import SymposiumConfig, load_config

APP_VERSION = get_version("seren-symposium", fallback=_fallback_version)
ACCENT = "#8E7CC3"          # symposium: muted violet, distinct from siblings
log = logging.getLogger("seren_symposium")

UI_DIR = Path(__file__).resolve().parent / "ui"


def _trim_history(history: Any, max_chars: int) -> list[dict[str, Any]]:
    """Keep the newest whole messages that fit in max_chars, oldest dropped first.

    Whole messages, never partial ones: half a turn reads as the model having
    been told something it was not. Dropping from the oldest end is what makes
    a long session degrade into a short-memory one instead of failing outright.
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
        kept.append(message)
    kept.reverse()
    return kept


def to_lodestar_payload(cfg: SymposiumConfig, body: dict[str, Any]) -> dict[str, Any]:
    """Symposium's request shape -> Lodestar's.

    Lodestar wants a flat {prompt, system_prompt, history, model_override,
    temperature}; it has never heard of a folder. Resolving the preset here
    rather than in the shell's JavaScript is what makes the layer precedence -
    folder default, then explicit per-session override - assertable in pytest.

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

    temperature = body.get("temperature")
    if temperature is None and preset is not None:
        temperature = preset.temperature
    if temperature is not None:
        out["temperature"] = temperature

    return out


def create_app(config: Optional[SymposiumConfig] = None) -> FastAPI:
    cfg = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg
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
        from urllib.parse import urlparse
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

    @app.get("/", response_class=HTMLResponse)
    async def index():
        shell = UI_DIR / "index.html"
        if not shell.is_file():
            return HTMLResponse(f"<h1>SerenSymposium {APP_VERSION}</h1>"
                                f"<p>ui/index.html not found at {shell}</p>", status_code=500)
        return HTMLResponse(shell.read_text(encoding="utf-8"))

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
            "viewers": {k: v for k, v in cfg.viewers.model_dump().items() if v},
            "folders": [f.model_dump() for f in cfg.folders],
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
        body: dict[str, Any] = await request.json()
        try:
            r = await request.app.state.client.post(
                "/api/v1/chat", json=to_lodestar_payload(cfg, body))
        except Exception as ex:  # noqa: BLE001
            return JSONResponse(
                {"error": "lodestar unreachable", "detail": f"{type(ex).__name__}: {ex}",
                 "url": cfg.lodestar.url}, status_code=502)

        payload = (r.json() if r.headers.get("content-type", "").startswith("application/json")
                   else {"text": r.text})

        # The one error we ANNOTATE rather than relay. Lodestar answers a bad
        # bearer with {"detail": "unauthorized"}, which is correct and useless
        # here - it cannot know the token it rejected came from a yaml file on
        # this machine. Saying which file to edit is knowledge only the client
        # has, and without it the UI renders a flat "401 unauthorized" that
        # sends people looking at Lodestar, where nothing is wrong.
        if r.status_code == 401:
            payload = dict(payload)
            payload["error"] = "lodestar rejected our bearer token"
            payload["hint"] = ("check lodestar.bearer_token in seren-symposium.yaml "
                               "against the token Lodestar is configured with")

        return JSONResponse(payload, status_code=r.status_code)

    return app
