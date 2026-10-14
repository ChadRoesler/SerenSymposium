"""Sessions that outlive the window, streaming that shows its work, and a
shim no other web page can drive."""
from __future__ import annotations

import json
import sys

import httpx
import pytest

from seren_symposium.__main__ import LOG_MAX_BYTES, came_up, log_without_console, webview_profile
from seren_symposium.app import refuse_cross_site
from seren_symposium.config import FolderPreset, SymposiumConfig, load_config
from seren_symposium.sessions import SessionStore


# ── Nobody else's page gets to drive it ───────────────────────────────────
#
# Loopback keeps the LAN out, not the browser: any page open on this machine
# can POST to 127.0.0.1:7426, and /api/chat runs Lodestar's tools with our
# bearer. These are the requests such a page can make.

def test_a_cross_site_form_post_is_refused(client):
    """The simple POST a hostile page can send without a preflight."""
    r = client.post("/api/chat", content="prompt=hi",
                    headers={"content-type": "application/x-www-form-urlencoded",
                             "origin": "https://evil.example"})
    assert r.status_code == 403


def test_text_plain_without_an_origin_is_still_refused(client):
    """No Origin (an old browser, a privacy extension) still cannot send a
    body JSON would have needed a preflight for."""
    r = client.post("/api/chat", content='{"prompt": "hi"}', headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_a_rebound_hostname_is_refused_even_for_reads(client):
    """DNS rebinding: evil.example resolves to 127.0.0.1, so the page is
    same-origin with itself and could read sessions. Its Host gives it away."""
    r = client.get("/api/sessions", headers={"host": "evil.example:7426"})
    assert r.status_code == 403


def test_the_shell_itself_gets_in(client):
    r = client.post("/api/sessions", json={"folder": "f"},
                    headers={"origin": "http://127.0.0.1:7426"})
    assert r.status_code == 201


def test_open_lan_drops_the_host_check_but_not_the_origin_one():
    cfg = SymposiumConfig()
    cfg.ui.allow_open_lan = True
    lan = {"host": "192.0.2.10:7426", "content-type": "application/json"}
    assert refuse_cross_site(cfg, "POST", lan) is None
    assert refuse_cross_site(cfg, "POST", {**lan, "origin": "https://evil.example"})[0] == 403
    assert refuse_cross_site(cfg, "POST", {**lan, "origin": "http://192.0.2.10:7426"}) is None


def test_ipv6_loopback_is_loopback():
    assert refuse_cross_site(SymposiumConfig(), "GET", {"host": "[::1]:7426"}) is None


# ── Sessions ──────────────────────────────────────────────────────────────

def test_a_session_round_trips_through_its_file(client, tmp_path):
    made = client.post("/api/sessions", json={"folder": "seren stack", "title": "upgrades"}).json()
    assert (tmp_path / "sessions" / f"{made['id']}.json").is_file()
    listed = client.get("/api/sessions").json()["sessions"]
    assert [(s["id"], s["folder"], s["count"]) for s in listed] == [(made["id"], "seren stack", 0)]
    assert client.get(f"/api/sessions/{made['id']}").json()["title"] == "upgrades"


def test_rename_move_and_settings(client):
    sid = client.post("/api/sessions", json={"folder": "a"}).json()["id"]
    s = client.patch(f"/api/sessions/{sid}", json={
        "title": "new name", "folder": "b",
        "settings": {"temperature": 0.0, "max_tokens": 512}}).json()
    assert (s["title"], s["folder"]) == ("new name", "b")
    assert s["settings"] == {"temperature": 0.0, "max_tokens": 512}
    # null drops a setting back to the folder's
    s = client.patch(f"/api/sessions/{sid}", json={"settings": {"temperature": None}}).json()
    assert s["settings"] == {"max_tokens": 512}


def test_bad_settings_and_unknown_fields_are_400(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.patch(f"/api/sessions/{sid}", json={"settings": {"temperature": "hot"}}).status_code == 400
    assert client.patch(f"/api/sessions/{sid}", json={"settings": {"bearer": "x"}}).status_code == 400
    assert client.patch(f"/api/sessions/{sid}", json={"messages": []}).status_code == 400
    assert client.post("/api/sessions", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 400


def test_an_id_is_never_a_path(client, tmp_path):
    (tmp_path / "secret.json").write_text('{"id": "secret"}', encoding="utf-8")
    assert client.get("/api/sessions/..%2Fsecret").status_code == 404
    assert SessionStore(tmp_path / "sessions").get("../secret") is None


def test_export_is_the_session_and_import_is_a_new_one(client):
    sid = client.post("/api/sessions", json={"folder": "f", "title": "t", "messages": [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]}).json()["id"]
    exported = client.get(f"/api/sessions/{sid}").json()
    again = client.post("/api/sessions", json=exported).json()
    assert again["id"] != sid
    assert [m["content"] for m in again["messages"]] == ["hi", "hello"]
    assert (again["folder"], again["title"]) == ("f", "t")


def test_delete(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True}
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_sessions_live_next_to_the_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("SEREN_SYMPOSIUM_SESSIONS_DIR", raising=False)
    yml = tmp_path / "seren-symposium.yaml"
    yml.write_text("ui:\n  port: 7427\n", encoding="utf-8")
    assert load_config(str(yml)).sessions_dir() == tmp_path.resolve() / "sessions"
    yml.write_text("sessions:\n  dir: " + str(tmp_path / "elsewhere").replace("\\", "/") + "\n",
                   encoding="utf-8")
    assert load_config(str(yml)).sessions_dir() == tmp_path / "elsewhere"


# ── A turn in a session ───────────────────────────────────────────────────

@pytest.fixture
def lodestar(make_client):
    """A Symposium wired to a scripted Lodestar that remembers what it got."""
    def _factory(cfg=None, reply=None, stream=None, status=200):
        tc = make_client(cfg)
        seen: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            seen.append((request.url.path, body))
            if status != 200:
                return httpx.Response(status, json={"detail": "nope"})
            if request.url.path == "/api/v1/chat/stream":
                lines = stream if stream is not None else [
                    {"type": "chunk", "content": "po"}, {"type": "chunk", "content": "ng"},
                    {"type": "done", "response": "pong", "model": "qwen", "node": "nano", "tool_rounds": 0}]
                return httpx.Response(200, content="".join(json.dumps(x) + "\n" for x in lines),
                                      headers={"content-type": "application/x-ndjson"})
            if request.url.path == "/api/v1/service/llama/models":
                return httpx.Response(200, json={"node": "nano", "models": {
                    "models": [{"name": "qwen.gguf", "size": 1}, {"name": "gemma.gguf"}], "models_dir": "/m"}})
            return httpx.Response(200, json=reply or {
                "response": "pong", "model": "qwen", "node": "nano", "tool_rounds": 0})

        tc.app.state.client = httpx.AsyncClient(base_url="http://lodestar.test",
                                                transport=httpx.MockTransport(handler))
        return tc, seen
    return _factory


def _events(r):
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_a_session_turn_layers_folder_then_session_and_is_kept(lodestar):
    cfg = SymposiumConfig(folders=[FolderPreset(name="f", system_prompt="from folder",
                                                temperature=0.6, repeat_penalty=1.1)])
    tc, seen = lodestar(cfg)
    sid = tc.post("/api/sessions", json={"folder": "f", "settings": {"temperature": 0.2}}).json()["id"]

    assert tc.post("/api/chat", json={"session": sid, "prompt": "ping"}).json()["session"] == sid
    tc.post("/api/chat", json={"session": sid, "prompt": "again"})

    path, sent = seen[-1]
    assert path == "/api/v1/chat"
    assert sent == {"prompt": "again", "system_prompt": "from folder", "temperature": 0.2,
                    "repeat_penalty": 1.1,
                    "history": [{"role": "user", "content": "ping"}, {"role": "assistant", "content": "pong"}]}
    s = tc.get(f"/api/sessions/{sid}").json()
    assert [m["content"] for m in s["messages"]] == ["ping", "pong", "again", "pong"]
    assert s["title"] == "ping", "a session with no name is named after its first prompt"
    assert s["messages"][1]["meta"]["node"] == "nano"


def test_a_failed_turn_is_not_kept(lodestar):
    tc, _ = lodestar(status=500)
    sid = tc.post("/api/sessions", json={}).json()["id"]
    assert tc.post("/api/chat", json={"session": sid, "prompt": "ping"}).status_code == 500
    assert tc.get(f"/api/sessions/{sid}").json()["messages"] == []


def test_retry_asks_the_last_prompt_again_and_replaces_its_answer(lodestar):
    tc, seen = lodestar(reply={"response": "better", "model": "qwen"})
    sid = tc.post("/api/sessions", json={"messages": [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "1"},
        {"role": "user", "content": "two"}, {"role": "assistant", "content": "meh"}]}).json()["id"]

    tc.post("/api/chat", json={"session": sid, "retry": True})

    _, sent = seen[-1]
    assert sent["prompt"] == "two"
    assert [m["content"] for m in sent["history"]] == ["one", "1"]
    assert [m["content"] for m in tc.get(f"/api/sessions/{sid}").json()["messages"]] == ["one", "1", "two", "better"]


def test_retry_with_nothing_to_retry_and_unknown_sessions(lodestar):
    tc, _ = lodestar()
    sid = tc.post("/api/sessions", json={}).json()["id"]
    assert tc.post("/api/chat", json={"session": sid, "retry": True}).status_code == 400
    assert tc.post("/api/chat", json={"session": "0" * 32, "prompt": "hi"}).status_code == 404


def test_the_stream_is_lodestars_line_for_line_then_saved(lodestar):
    tc, seen = lodestar(stream=[
        {"type": "chunk", "content": "let me look"},
        {"type": "tool_status", "tool": "recall"},
        {"type": "chunk", "content": "found it"},
        {"type": "done", "response": "found it", "model": "qwen", "node": "nano", "tool_rounds": 1}])
    sid = tc.post("/api/sessions", json={"folder": "f"}).json()["id"]

    r = tc.post("/api/chat/stream", json={"session": sid, "prompt": "what did we say?"})

    assert r.headers["content-type"].startswith("application/x-ndjson")
    events = _events(r)
    assert [e["type"] for e in events] == ["chunk", "tool_status", "chunk", "done", "saved"]
    assert events[-1]["session"]["count"] == 2
    assert seen[-1][0] == "/api/v1/chat/stream"
    answer = tc.get(f"/api/sessions/{sid}").json()["messages"][-1]
    assert answer["content"] == "found it"
    assert answer["meta"]["tools"] == ["recall"]
    assert answer["meta"]["tool_rounds"] == 1


def test_a_stream_that_ends_in_error_keeps_nothing(lodestar):
    tc, _ = lodestar(stream=[{"type": "chunk", "content": "hal"},
                             {"type": "error", "error": "llama-server timed out"}])
    sid = tc.post("/api/sessions", json={}).json()["id"]
    events = _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "hi"}))
    assert events[-1]["type"] == "error"
    assert tc.get(f"/api/sessions/{sid}").json()["messages"] == []


def test_a_stream_401_is_annotated_like_chats(lodestar):
    tc, _ = lodestar(status=401)
    r = tc.post("/api/chat/stream", json={"prompt": "hi"})
    assert r.status_code == 401
    assert "seren-symposium.yaml" in r.json()["hint"]


def test_a_stream_with_lodestar_down_is_502(client):
    r = client.post("/api/chat/stream", json={"prompt": "hi"})
    assert r.status_code == 502
    assert r.json()["error"] == "lodestar unreachable"


def test_models_are_flattened_to_names(lodestar):
    tc, seen = lodestar()
    assert tc.get("/api/models").json() == {"node": "nano", "models": ["qwen.gguf", "gemma.gguf"]}
    assert seen[-1][0] == "/api/v1/service/llama/models"


def test_receipts_and_timestamps_stay_home(lodestar):
    """Lodestar gets role and content; the rest of a message is ours."""
    tc, seen = lodestar()
    sid = tc.post("/api/sessions", json={"messages": [
        {"role": "user", "content": "a", "at": 1},
        {"role": "assistant", "content": "b", "meta": {"node": "nano"}}]}).json()["id"]
    tc.post("/api/chat", json={"session": sid, "prompt": "c"})
    assert seen[-1][1]["history"] == [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]


# ── A window onto nothing ─────────────────────────────────────────────────

class _Server:
    started = False


class _Thread:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


def test_no_window_when_the_server_died_on_the_port():
    """uvicorn on a taken port logs it and ends its thread. That must not be
    followed by a native window pointing at nothing."""
    assert came_up(_Server(), _Thread(alive=False), seconds=5) is False


def test_no_window_when_the_server_never_came_up():
    assert came_up(_Server(), _Thread(alive=True), seconds=0.1, step=0.01) is False


def test_a_window_when_it_did():
    s = _Server()
    s.started = True
    assert came_up(s, _Thread(alive=True), seconds=0.1) is True


# ── Started with no console (pythonw, a Start menu shortcut) ─────────────

def test_with_a_console_nothing_is_redirected(tmp_path):
    assert log_without_console(str(tmp_path / "seren-symposium.yaml")) is None


def test_without_one_output_goes_beside_the_config(tmp_path, monkeypatch):
    """pythonw has no stdout or stderr; uvicorn's log setup asked None
    whether it was a terminal and the window never opened."""
    cfg = tmp_path / "seren-symposium.yaml"
    cfg.write_text("ui:\n  port: 7426\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    path = log_without_console(str(cfg))
    assert path == tmp_path / "symposium.log"
    print("[symposium] hello", file=sys.stderr)
    sys.stderr.flush()
    assert "[symposium] hello" in path.read_text(encoding="utf-8")
    assert sys.stdout is sys.stderr
    sys.stderr.close()


def test_the_window_keeps_its_profile_beside_the_sessions(tmp_path, monkeypatch):
    """pywebview starts private unless told otherwise, and private empties
    localStorage on close: a project with no chat was gone on every launch."""
    monkeypatch.delenv("SEREN_SYMPOSIUM_SESSIONS_DIR", raising=False)
    yml = tmp_path / "seren-symposium.yaml"
    yml.write_text("sessions:\n  dir: " + str(tmp_path / "stores" / "sessions").replace("\\", "/") + "\n",
                   encoding="utf-8")
    path = webview_profile(load_config(str(yml)))
    assert path == tmp_path / "stores" / "webview"
    assert path.is_dir()


def test_a_log_grown_too_big_starts_over(tmp_path, monkeypatch):
    cfg = tmp_path / "seren-symposium.yaml"
    cfg.write_text("", encoding="utf-8")
    (tmp_path / "symposium.log").write_text("x" * (LOG_MAX_BYTES + 1), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    path = log_without_console(str(cfg))
    sys.stderr.close()
    assert path.stat().st_size == 0
