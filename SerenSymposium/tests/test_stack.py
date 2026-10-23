"""What a chat's model is given, layer by layer (22 Oct 2026): the prompts
add up, project then chat; the claude voice's stack names what the CLI said
it loaded, grouped by server; a local model's is asked of Lodestar itself."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from seren_symposium import claude_voice, stack
from seren_symposium.config import FolderPreset, SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def claude_cfg(tmp_path, **folder) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.command = [sys.executable, FAKE]
    cfg.claude.carabiner = str(tmp_path / "no-carabiner-here")
    cfg.folders = [FolderPreset(name="den", voice="claude", system_prompt="the project's", **folder),
                   FolderPreset(name="lab", system_prompt="the lab's")]
    return cfg


def layer(st, key):
    return next(x for x in st["layers"] if x["key"] == key)


def run(tc, sid, prompt="hi"):
    r = tc.post("/api/chat/stream", json={"session": sid, "prompt": prompt})
    assert r.status_code == 200, r.text


def test_the_prompts_add_up_project_then_chat_unless_the_chat_says_not(make_client, tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    tc = make_client(claude_cfg(tmp_path))
    sid = tc.post("/api/sessions", json={"folder": "den", "settings": {"system_prompt": "the chat's"}}).json()["id"]
    run(tc, sid)
    argv = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["argv"]
    told = argv[argv.index("--append-system-prompt") + 1]
    assert told.startswith("the project's\n\nthe chat's"), told[:60]

    assert tc.patch(f"/api/sessions/{sid}", json={"settings": {"system_inherit": False}}).status_code == 200
    run(tc, sid, "again")
    argv = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["argv"]
    assert argv[argv.index("--append-system-prompt") + 1].startswith("the chat's"), "its project's left out"
    assert tc.post("/api/sessions", json={"folder": "den", "settings": {"system_inherit": "no"}}).status_code == 400


def test_the_cli_init_is_kept_for_the_stack_never_its_ids(make_client, tmp_path):
    digest = claude_voice.digest_init({"type": "system", "subtype": "init", "session_id": "s", "uuid": "u",
                                       "apiKeySource": "x", "messaging_socket_path": "p", "tools": ["Read"],
                                       "plugins": [{"name": "kit", "path": "/x"}]})
    assert "session_id" not in json.dumps(digest) and "apiKeySource" not in digest and digest["plugins"] == ["kit"]

    tc = make_client(claude_cfg(tmp_path))
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    assert layer(tc.get("/api/stack", params={"session": sid}).json(), "tools")["state"] == "unknown"
    run(tc, sid)
    assert "claude_init" not in tc.get(f"/api/sessions/{sid}").json(), "the stack's, not the chat's"

    st = tc.get("/api/stack", params={"session": sid}).json()
    assert st["voice"] == "claude"
    tools = layer(st, "tools")
    assert tools["count"] == 7 and tools["builtin"] == ["Task", "Bash", "Read", "ToolSearch"]
    servers = {s["name"]: s for s in tools["servers"]}
    assert servers["workbench"]["tools"] == ["get_time", "recall"] and servers["workbench"]["status"] == "connected"
    assert servers["claude.ai Gmail"]["tools"] == ["search"] and servers["claude.ai Gmail"]["status"] == "needs-auth"
    assert layer(st, "skills")["items"] == ["dataviz"] and layer(st, "slash_commands")["count"] == 2
    assert "this chat" in tools["where"]
    assert [x["key"] for x in st["layers"] if x["group"] == "system"] == ["cli", "project", "chat", "framing"]
    assert layer(st, "project")["text"] == "the project's" and layer(st, "chat")["state"] == "empty"
    assert layer(st, "history")["resumes"] is True

    draft = tc.get("/api/stack", params={"folder": "den", "settings": json.dumps({"system_prompt": "mine"})}).json()
    assert "another chat" in layer(draft, "tools")["where"], "before its first answer, another chat's"
    assert layer(draft, "chat")["text"] == "mine"
    assert draft["chars"]["system"] == len("the project's") + len("mine") + len(layer(draft, "framing")["text"])


def test_the_files_the_cli_reads_itself_are_shown(make_client, tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / ".claude").mkdir(parents=True)
    (work / "CLAUDE.md").write_text("house rules", encoding="utf-8")
    (work / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "secret-thing --token abc"}]}]}}), encoding="utf-8")
    auto = tmp_path / "auto"
    auto.mkdir()
    (auto / "MEMORY.md").write_text("\n".join(f"line {i}" for i in range(300)), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MEMORY", str(auto))
    cfg = claude_cfg(tmp_path)
    cfg.claude.cwd = str(work)
    tc = make_client(cfg)
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    run(tc, sid)

    st = tc.get("/api/stack", params={"session": sid}).json()
    files = {Path(f["path"]).name: f for f in layer(st, "memory")["files"]}
    assert files["CLAUDE.md"]["text"] == "house rules" and files["CLAUDE.md"]["kind"] == "project"
    assert files["MEMORY.md"]["text"].count("\n") == stack.MEMORY_INDEX_LINES - 1, "the index's first lines only"
    hooks = layer(st, "hooks")["items"]
    assert any(str(work) in h for h in hooks) and "secret-thing" not in json.dumps(st), "where, never the command"


def test_a_preface_replaces_the_clis_own_prompt_in_the_stack(make_client, tmp_path):
    page = tmp_path / "preface.md"
    page.write_text("A page of its own.", encoding="utf-8")
    cfg = claude_cfg(tmp_path)
    cfg.claude.preface.file = str(page)
    st = make_client(cfg).get("/api/stack", params={"folder": "den"}).json()
    assert layer(st, "cli")["state"] == "replaced" and layer(st, "preface")["text"] == "A page of its own."
    assert st["chars"]["system"] >= len("A page of its own.") + len("the project's")


def test_a_local_models_stack_is_what_lodestar_says_it_would_send(make_client, tmp_path):
    tc, seen = make_client(claude_cfg(tmp_path)), []
    block = "# Tools\nget_time: what time it is"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.url.path, body))
        if request.url.path == "/api/v1/chat/inspect":
            system = block + ("\n\n" + body["system_prompt"] if body.get("system_prompt") else "")
            return httpx.Response(200, json={"dialect": "qwen", "tools_count": 1, "tool_names": ["get_time"],
                                             "has_tools_block": True, "tools_block_chars": len(block),
                                             "messages": [{"role": "system", "content": system},
                                                          {"role": "user", "content": ""}]})
        return httpx.Response(200, json={"response": "pong", "model": "qwen", "node": "nano", "tool_rounds": 0})

    tc.app.state.client = httpx.AsyncClient(base_url="http://lodestar.test", transport=httpx.MockTransport(handler))
    sid = tc.post("/api/sessions", json={"folder": "lab", "settings": {"system_prompt": "the chat's",
                                                                       "temperature": 0.3}}).json()["id"]
    tc.post("/api/chat", json={"session": sid, "prompt": "ping"})

    st = tc.get("/api/stack", params={"session": sid}).json()
    assert st["voice"] == "lodestar"
    asked = next(b for p, b in seen if p == "/api/v1/chat/inspect")
    assert asked == {"system_prompt": "the lab's\n\nthe chat's", "prompt": ""}, "no history goes to be looked at"
    tools = layer(st, "tools")
    assert tools["text"] == block and tools["items"] == ["get_time"] and tools["count"] == 1
    assert [x["key"] for x in st["layers"] if x["group"] == "system"] == ["tools", "project", "chat"]
    assert layer(st, "history")["messages"] == 2 and layer(st, "sampling")["values"]["temperature"] == 0.3

    tc.app.state.client = httpx.AsyncClient(base_url="http://lodestar.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"detail": "Not Found"})))
    st = tc.get("/api/stack", params={"session": sid}).json()
    assert layer(st, "tools")["state"] == "unknown" and "no /chat/inspect" in layer(st, "tools")["note"]
    assert layer(st, "chat")["text"] == "the chat's", "the rest still shows"
