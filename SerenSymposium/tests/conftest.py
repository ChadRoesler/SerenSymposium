"""Shared fixtures for SerenSymposium.

Two things are guaranteed here: no test reaches the network, and no test
depends on a real Lodestar being up.
"""
from __future__ import annotations

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
    clients = []

    def _factory(cfg: SymposiumConfig | None = None) -> TestClient:
        tc = TestClient(create_app(cfg or SymposiumConfig()),
                        raise_server_exceptions=False)
        tc.__enter__()
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
