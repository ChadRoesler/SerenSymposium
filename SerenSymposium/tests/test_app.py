"""The shell's own surface, and the contract that keeps Lodestar the cluster head."""
from __future__ import annotations

import json

import httpx
import pytest

from seren_symposium.app import to_lodestar_payload
from seren_symposium.config import FolderPreset, SymposiumConfig

EXPECTED_UPDATE_KEYS = {"status", "distribution", "installed", "latest",
                        "update_available", "detail", "checked_at"}


def test_health_reports_our_state_and_lodestars_separately(client):
    """Ours is trivially ok. Lodestar's is the useful half, and with nothing
    running it must report unreachable rather than failing the request."""
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["lodestar"]["reachable"] is False
    assert body["lodestar"]["status"] == "unreachable"
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
    r = client.post("/api/chat", json={"prompt": "hi"})
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


# ── The Lodestar request shape ────────────────────────────────────────────
#
# Lodestar's POST /api/v1/chat rejects a body without `prompt` outright:
#
#     if not body or not body.get("prompt"):
#         return JSONResponse({"error": "prompt is required"}, status_code=400)
#
# The shell used to send OpenAI-style {"messages": [...]}, so every message got
# a 400 and nothing ever reached a model. These tests pin the real field names.

def test_payload_speaks_lodestars_dialect_not_openais():
    """`prompt`, never `messages` - the bug that made chat 400 on every send."""
    out = to_lodestar_payload(SymposiumConfig(), {"prompt": "hello"})
    assert out["prompt"] == "hello"
    assert "messages" not in out


def test_folder_preset_is_resolved_into_the_payload():
    """Folders are a Symposium concept; Lodestar only ever sees the flattened
    result. This is what made folder presets real rather than decorative."""
    cfg = SymposiumConfig(folders=[FolderPreset(
        name="seren stack", system_prompt="prefer real bytes",
        model="qwen", temperature=0.6)])
    out = to_lodestar_payload(cfg, {"prompt": "hi", "folder": "seren stack"})
    assert out["system_prompt"] == "prefer real bytes"
    assert out["model_override"] == "qwen"
    assert out["temperature"] == 0.6
    assert "folder" not in out, "Lodestar has never heard of a folder"


def test_per_session_values_override_the_folder_preset():
    """The documented layering: folder default, then the session chip on top."""
    cfg = SymposiumConfig(folders=[FolderPreset(
        name="f", system_prompt="from folder", model="a", temperature=0.2)])
    out = to_lodestar_payload(cfg, {
        "prompt": "hi", "folder": "f",
        "system_prompt": "from session", "model_override": "b", "temperature": 0.9})
    assert out["system_prompt"] == "from session"
    assert out["model_override"] == "b"
    assert out["temperature"] == 0.9


def test_temperature_zero_survives_the_override_check():
    """0.0 is a legitimate temperature and a falsy float. A truthiness test
    here would silently swap it for the folder default."""
    cfg = SymposiumConfig(folders=[FolderPreset(name="f", temperature=0.7)])
    out = to_lodestar_payload(cfg, {"prompt": "hi", "folder": "f", "temperature": 0.0})
    assert out["temperature"] == 0.0


def test_empty_preset_fields_are_omitted_rather_than_sent_blank():
    cfg = SymposiumConfig(folders=[FolderPreset(name="general")])
    out = to_lodestar_payload(cfg, {"prompt": "hi", "folder": "general"})
    assert set(out) == {"prompt"}


def test_unknown_folder_does_not_explode():
    """A folder renamed in the yaml while a session is open must not 500."""
    out = to_lodestar_payload(SymposiumConfig(), {"prompt": "hi", "folder": "gone"})
    assert out == {"prompt": "hi"}


def test_history_carries_multi_turn_context():
    prior = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    out = to_lodestar_payload(SymposiumConfig(), {"prompt": "three", "history": prior})
    assert out["history"] == prior


# ── Bounding what goes on the wire ────────────────────────────────────────
#
# Lodestar's _enforce_token_budget only rewrites messages containing
# <tool_response>; ordinary conversation is never trimmed. Measured against the
# real function, a 40-turn thread came to 18,400 tokens against a 6,000-token
# budget and passed through untouched. Nothing downstream will save us, so the
# client bounds its own history.

def test_long_history_is_trimmed_to_the_configured_budget():
    cfg = SymposiumConfig()
    cfg.lodestar.history_max_chars = 100
    history = [{"role": "user", "content": "x" * 30} for _ in range(20)]
    out = to_lodestar_payload(cfg, {"prompt": "hi", "history": history})
    assert sum(len(m["content"]) for m in out["history"]) <= 100


def test_trimming_drops_the_oldest_and_keeps_the_newest():
    """A long session should degrade into a short memory, not a wrong one."""
    cfg = SymposiumConfig()
    cfg.lodestar.history_max_chars = 12   # fits "middle" + "newest", not "old"
    history = [{"role": "user", "content": "old"},
               {"role": "assistant", "content": "middle"},
               {"role": "user", "content": "newest"}]
    out = to_lodestar_payload(cfg, {"prompt": "hi", "history": history})
    assert [m["content"] for m in out["history"]] == ["middle", "newest"]


def test_a_single_oversized_message_does_not_wedge_the_request():
    """One giant paste must not be sent whole, nor stop the turn happening."""
    cfg = SymposiumConfig()
    cfg.lodestar.history_max_chars = 50
    out = to_lodestar_payload(cfg, {
        "prompt": "hi", "history": [{"role": "user", "content": "x" * 5000}]})
    assert "history" not in out


def test_default_history_budget_leaves_room_under_lodestars_context():
    """3 chars/token is Lodestar's own estimate; 6000 tokens is its default
    budget. History must not eat the whole thing - the system prompt, the MCP
    tools block and the answer all have to fit alongside it."""
    assert SymposiumConfig().lodestar.history_max_chars < 6000 * 3


def test_our_timeout_outlives_lodestars_so_its_504_wins_the_race():
    """Lodestar's llama call times out at 120s and answers with a 504 naming the
    node. A shorter client timeout would replace that with a generic 502."""
    assert SymposiumConfig().lodestar.timeout_seconds > 120.0


@pytest.fixture
def wired_to_a_fake_lodestar(make_client):
    """Swap the Lodestar leg for a MockTransport so the request that actually
    goes out can be inspected - no cluster head, no network."""
    def _factory(cfg=None, reply=None):
        tc = make_client(cfg)
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["json"] = json.loads(request.content)
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, json=reply or {
                "response": "pong", "model": "seren", "node": "nano", "tool_rounds": 0})

        tc.app.state.client = httpx.AsyncClient(
            base_url="http://lodestar.test",
            transport=httpx.MockTransport(handler),
            headers=tc.app.state.client.headers)
        return tc, seen
    return _factory


def test_chat_puts_lodestars_shape_on_the_wire(wired_to_a_fake_lodestar):
    cfg = SymposiumConfig(folders=[FolderPreset(name="f", system_prompt="sp", temperature=0.3)])
    cfg.lodestar.bearer_token = "super-secret-value"
    tc, seen = wired_to_a_fake_lodestar(cfg)

    r = tc.post("/api/chat", json={"prompt": "hi", "folder": "f",
                                   "history": [{"role": "user", "content": "prior"}]})

    assert r.status_code == 200
    assert seen["path"] == "/api/v1/chat"
    assert seen["json"] == {
        "prompt": "hi",
        "history": [{"role": "user", "content": "prior"}],
        "system_prompt": "sp",
        "temperature": 0.3,
    }
    assert seen["authorization"] == "Bearer super-secret-value", \
        "the bearer is attached on the way OUT even though it never reaches the UI"


def test_chat_passes_lodestars_response_through_unrenamed(wired_to_a_fake_lodestar):
    """The shell reads `response` and `tool_rounds`. Renaming them in the proxy
    would just create a second contract to keep in sync."""
    tc, _ = wired_to_a_fake_lodestar(
        reply={"response": "the answer", "model": "seren", "tool_rounds": 2})
    body = tc.post("/api/chat", json={"prompt": "hi"}).json()
    assert body["response"] == "the answer"
    assert body["tool_rounds"] == 2


# ── Telling "up" apart from "usable" ──────────────────────────────────────
#
# Lodestar's /health is PUBLIC (seren_meninges.auth.DEFAULT_PUBLIC_PATHS), so it
# answers 200 even when our bearer is wrong. Probing only that produced a green
# dot on an app where every message came back 401 - verified against a real
# Lodestar, not theorised. The second probe hits /api/v1/chat/health, which is
# behind the token AND knows whether a node is serving llama.

@pytest.fixture
def lodestar_answering(make_client):
    """Drive Symposium's health probes with scripted Lodestar responses."""
    def _factory(health=None, chat_health=None, cfg=None):
        tc = make_client(cfg)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return health or httpx.Response(200, json={"ok": True})
            if request.url.path == "/api/v1/chat/health":
                if chat_health is None:
                    return httpx.Response(200, json={"ok": True, "node": "xavier"})
                return chat_health
            return httpx.Response(404)

        tc.app.state.client = httpx.AsyncClient(
            base_url="http://lodestar.test", transport=httpx.MockTransport(handler))
        return tc
    return _factory


def test_health_is_ok_only_when_inference_is_actually_reachable(lodestar_answering):
    body = lodestar_answering().get("/health").json()["lodestar"]
    assert body["status"] == "ok"
    assert "xavier" in body["detail"], "name the node so the dot's tooltip is worth reading"


def test_a_rejected_token_is_not_reported_as_healthy(lodestar_answering):
    """THE bug this split exists for: reachable, and completely unusable."""
    tc = lodestar_answering(chat_health=httpx.Response(401, json={"detail": "unauthorized"}))
    body = tc.get("/health").json()["lodestar"]
    assert body["reachable"] is True, "the process really is up - say so"
    assert body["status"] == "unauthorized"
    assert "bearer_token" in body["detail"], "must name the setting to go fix"


def test_authenticated_but_no_llama_node_is_its_own_state(lodestar_answering):
    """Not an auth problem and not a dead head - a cluster with no model up."""
    tc = lodestar_answering(chat_health=httpx.Response(
        200, json={"ok": False, "reason": "no online node serving llama"}))
    body = tc.get("/health").json()["lodestar"]
    assert body["status"] == "no_inference"
    assert body["detail"] == "no online node serving llama"


def test_a_dead_head_short_circuits_before_the_authed_probe(lodestar_answering):
    tc = lodestar_answering(health=httpx.Response(503))
    body = tc.get("/health").json()["lodestar"]
    assert body["status"] == "unreachable"
    assert body["reachable"] is False


def test_chat_401_says_which_file_to_edit(wired_to_a_fake_lodestar):
    """Lodestar's own {"detail": "unauthorized"} is correct and useless to a
    desktop user - it cannot know the token came from our yaml."""
    tc, _ = wired_to_a_fake_lodestar()

    def unauthorized(request):
        return httpx.Response(401, json={"detail": "unauthorized"})

    tc.app.state.client = httpx.AsyncClient(
        base_url="http://lodestar.test", transport=httpx.MockTransport(unauthorized))

    r = tc.post("/api/chat", json={"prompt": "hi"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "lodestar rejected our bearer token"
    assert "seren-symposium.yaml" in body["hint"]
    assert body["detail"] == "unauthorized", "Lodestar's own words are kept too"
