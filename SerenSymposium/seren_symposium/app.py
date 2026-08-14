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
        """Ours is trivial; the useful half is whether the cluster head answers.
        Silence is signal - the UI shows this as a dot, not a paragraph."""
        reachable, detail = False, ""
        try:
            r = await request.app.state.client.get("/health")
            reachable = r.status_code == 200
            detail = f"HTTP {r.status_code}"
        except Exception as ex:  # noqa: BLE001
            detail = f"{type(ex).__name__}: {ex}"
        return {"ok": True, "version": APP_VERSION,
                "lodestar": {"url": cfg.lodestar.url, "reachable": reachable, "detail": detail}}

    @app.post("/api/chat")
    async def chat(request: Request):
        """Proxy to Lodestar with the bearer attached.

        Deliberately thin: Symposium does not know what a tool is, does not
        route, does not touch MCP. Lodestar owns all of that. If logic starts
        accumulating here, it belongs in the cluster head instead.
        """
        body: dict[str, Any] = await request.json()
        try:
            r = await request.app.state.client.post("/api/v1/chat", json=body)
        except Exception as ex:  # noqa: BLE001
            return JSONResponse(
                {"error": "lodestar unreachable", "detail": f"{type(ex).__name__}: {ex}",
                 "url": cfg.lodestar.url}, status_code=502)
        return JSONResponse(r.json() if r.headers.get("content-type", "").startswith(
            "application/json") else {"text": r.text}, status_code=r.status_code)

    return app
