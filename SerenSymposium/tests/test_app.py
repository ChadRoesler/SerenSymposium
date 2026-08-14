"""The shell's own surface, and the contract that keeps Lodestar the cluster head."""
from __future__ import annotations

from seren_symposium.config import FolderPreset, SymposiumConfig

EXPECTED_UPDATE_KEYS = {"status", "distribution", "installed", "latest",
                        "update_available", "detail", "checked_at"}


def test_health_reports_our_state_and_lodestars_separately(client):
    """Ours is trivially ok. Lodestar's is the useful half, and with nothing
    running it must report unreachable rather than failing the request."""
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["lodestar"]["reachable"] is False
    assert body["lodestar"]["detail"], "an unreachable head must say why"


def test_api_config_never_leaks_the_bearer(make_client):
    """The token stays in Python. That is the entire reason this loopback shim
    exists instead of letting JavaScript call Lodestar directly."""
    cfg = SymposiumConfig()
    cfg.lodestar.bearer_token = "super-secret-value"
    body = make_client(cfg).get("/api/config").json()
    assert "super-secret-value" not in str(body)


def test_api_config_carries_folders_for_the_shell(make_client):
    cfg = SymposiumConfig(folders=[FolderPreset(name="seren stack", temperature=0.6)])
    body = make_client(cfg).get("/api/config").json()
    assert [f["name"] for f in body["folders"]] == ["seren stack"]


def test_api_config_carries_a_well_formed_update_block(client):
    u = client.get("/api/config").json()["updates"]
    assert set(u) == EXPECTED_UPDATE_KEYS
    assert u["status"] in {"ok", "disabled", "unavailable", "error"}
    assert isinstance(u["update_available"], bool)


def test_blank_viewer_urls_are_dropped_not_shown_dead(make_client):
    """Someone who does not run Probe should not get a dead Probe tab."""
    cfg = SymposiumConfig()
    cfg.viewers.probe = ""
    viewers = make_client(cfg).get("/api/config").json()["viewers"]
    assert "probe" not in viewers
    assert "memory" in viewers


def test_chat_returns_502_when_lodestar_is_down(client):
    """Not a 500. The app is fine; the cluster head is not, and the UI needs to
    be able to tell those apart."""
    r = client.post("/api/chat", json={"messages": []})
    assert r.status_code == 502
    assert r.json()["error"] == "lodestar unreachable"


def test_symposium_exposes_no_route_that_bypasses_lodestar(client):
    """THE single-endpoint contract, asserted rather than documented.

    Symposium talks to the cluster head and nothing else. If a route ever
    appears here that reaches a leaf service directly, Lodestar stops being the
    thing that owns routing - and that erosion is gradual and easy to miss in
    review, so it gets a test.
    """
    paths = {r.path for r in client.app.routes}
    allowed = {"/", "/health", "/api/config", "/api/chat",
               "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    unexpected = paths - allowed
    assert not unexpected, (
        f"new route(s) {unexpected} - if any of these call a leaf service "
        f"directly, the single-endpoint contract is broken")
