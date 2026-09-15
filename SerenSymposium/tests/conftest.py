"""Shared fixtures for SerenSymposium.

Two things are guaranteed here, by construction rather than by luck: no test
reaches the network, and no test cares whether a real Lodestar is running on
this machine - in EITHER direction.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from seren_symposium.app import create_app
from seren_symposium.config import SymposiumConfig


@pytest.fixture(autouse=True)
def offline_update_checks(monkeypatch):
    """No test may talk to pypi.org.

    GET /api/config carries the update status and checking is on by default,
    so without this every test touching it makes a real network call - slow,
    flaky offline, and rude to someone else's server. Patching the CLASS method
    rather than an env var is deliberate: tests build a SymposiumConfig
    directly, so an env override would not reach them.
    """
    try:
        from seren_meninges.updates import UpdateChecker
    except ImportError:
        return

    async def _no_network(self, distribution):
        raise ConnectionError("network disabled in tests")

    monkeypatch.setattr(UpdateChecker, "_fetch_from_index", _no_network)


@pytest.fixture
def make_client():
    """A Symposium whose Lodestar leg CANNOT reach anything.

    The default config points at 127.0.0.1:6361, so "Lodestar is down" used to
    mean "nobody happened to be serving that port". Anyone with a real Lodestar
    running - the normal state while developing the thing Symposium talks to -
    got two failures that had nothing to do with their change. Refusing the
    connection in a transport makes the down-ness a property of the fixture
    instead of a property of the developer's machine.

    Tests that want a Lodestar replace app.state.client with their own
    MockTransport.
    """
    clients = []

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no Lodestar in tests", request=request)

    def _factory(cfg: SymposiumConfig | None = None) -> TestClient:
        cfg = cfg or SymposiumConfig()
        app = create_app(cfg)
        tc = TestClient(app, raise_server_exceptions=False)
        tc.__enter__()
        # After __enter__, so this replaces the client lifespan just built.
        bearer = cfg.lodestar.resolve_bearer()
        app.state.client = httpx.AsyncClient(
            base_url=cfg.lodestar.url.rstrip("/"),
            transport=httpx.MockTransport(_refuse),
            headers=({"Authorization": f"Bearer {bearer}"} if bearer else {}))
        clients.append(tc)
        return tc

    yield _factory
    for tc in clients:
        try:
            tc.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def client(make_client):
    return make_client()
