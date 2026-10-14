"""The claude voice: a chat where Claude answers through the claude CLI on
this machine, played here by tests/fake_claude.py."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from seren_symposium import claude_voice
from seren_symposium.app import Turn, claude_answer
from seren_symposium.config import ClaudeConfig, FolderPreset, SymposiumConfig
from seren_symposium.sessions import SessionStore

FAKE = str(Path(__file__).with_name("fake_claude.py"))


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """What the fake was asked, one dict per run: {argv, prompt}."""
    log = tmp_path / "claude-runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def read():
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return read


def config(**folder) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.claude.command = [sys.executable, FAKE]
    cfg.folders = [FolderPreset(name="den", voice="claude", **folder),
                   FolderPreset(name="lab", model="qwen-local")]
    return cfg


def stream(client, body):
    r = client.post("/api/chat/stream", json=body)
    assert r.status_code == 200, r.text
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


# ── A turn ────────────────────────────────────────────────────────────────

def test_a_claude_turn_streams_in_lodestars_shape_and_is_kept(make_client, runs):
    client = make_client(config(system_prompt="be brief", model="opus"))
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]

    events = stream(client, {"session": sid, "prompt": "hi"})

    assert [e["type"] for e in events] == ["tool_status", "tool_call", "tool_result", "thinking",
                                           "chunk", "chunk", "done", "saved"]
    assert events[0]["tool"] == "mcp__workbench__get_time"          # ToolSearch is bookkeeping
    assert events[1]["summary"] == '{"zone": "UTC"}' and events[2] == {
        "type": "tool_result", "id": events[1]["id"], "ok": True, "preview": "12:00 UTC"}
    assert "".join(e["content"] for e in events if e["type"] == "chunk") == "Hello there"
    assert events[6]["response"] == "Hello there"                    # no subagent, no thinking

    s = client.get(f"/api/sessions/{sid}").json()
    assert [m["content"] for m in s["messages"]] == ["hi", "Hello there"]
    meta = s["messages"][1]["meta"]
    assert meta["model"] == "fake-model" and meta["node"] == "claude"
    assert meta["tools"] == ["mcp__workbench__get_time"] and meta["tool_rounds"] == 2
    # how it got there, folded under the answer
    assert meta["steps"] == [
        {"kind": "tool", "id": events[1]["id"], "tool": "mcp__workbench__get_time",
         "summary": '{"zone": "UTC"}', "ok": True, "result": "12:00 UTC"},
        {"kind": "thinking", "text": "hmm"}]

    (run,) = runs()
    assert run["prompt"] == "hi"
    assert flag(run["argv"], "--session-id") == s["claude"]["session"]
    assert flag(run["argv"], "--model") == "opus"
    system = flag(run["argv"], "--append-system-prompt")
    folder = flag(run["argv"], "--add-dir")
    assert system.startswith("be brief\n\nYou are answering in Seren Symposium")   # the chat's, then ours
    assert folder.endswith(f"{sid}.files") and folder in system                     # files: where, and readable
    assert s["claude"]["seen"] == 2


def test_the_next_turn_resumes_the_same_conversation_and_sends_no_history(make_client, runs):
    client = make_client(config())
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    stream(client, {"session": sid, "prompt": "one"})
    stream(client, {"session": sid, "prompt": "two"})

    first, second = runs()
    assert flag(second["argv"], "--resume") == flag(first["argv"], "--session-id")
    assert "--session-id" not in second["argv"]
    assert second["prompt"] == "two"


def test_a_chat_that_started_elsewhere_is_handed_over_once(make_client, runs):
    client = make_client(config())
    sid = client.post("/api/sessions", json={
        "folder": "lab", "settings": {"voice": "claude"},
        "messages": [{"role": "user", "content": "what is a rig"},
                     {"role": "assistant", "content": "bones for a mesh"}]}).json()["id"]

    stream(client, {"session": sid, "prompt": "and a skin?"})
    stream(client, {"session": sid, "prompt": "thanks"})

    first, second = runs()
    assert first["prompt"].startswith("Earlier in this chat")
    assert "bones for a mesh" in first["prompt"] and first["prompt"].endswith("and a skin?")
    assert second["prompt"] == "thanks"


def test_turns_another_voice_answered_are_handed_over_on_return(make_client, runs, tmp_path):
    client = make_client(config())
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    stream(client, {"session": sid, "prompt": "one"})
    # A local model answers a turn in between (as /api/chat would record it).
    SessionStore(tmp_path / "sessions").record(sid, "lodestar q", "lodestar a", {"node": "nano"})
    stream(client, {"session": sid, "prompt": "two"})

    second = runs()[1]
    assert second["prompt"].startswith("In this chat, while another voice was answering")
    assert "lodestar a" in second["prompt"] and "Hello there" not in second["prompt"]


def test_a_local_models_name_is_not_handed_to_claude(make_client, runs):
    """The lab folder's model is a llama file; a chat there switched to
    claude must not pass it to --model."""
    client = make_client(config())
    sid = client.post("/api/sessions", json={"folder": "lab", "settings": {"voice": "claude"}}).json()["id"]
    stream(client, {"session": sid, "prompt": "hi"})
    assert "--model" not in runs()[0]["argv"]


def test_the_non_streaming_route_answers_too(make_client, runs):
    client = make_client(config())
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    r = client.post("/api/chat", json={"session": sid, "prompt": "hi"})
    assert r.status_code == 200
    assert r.json()["response"] == "Hello there" and r.json()["session"] == sid


# ── When it goes wrong ────────────────────────────────────────────────────

def test_a_failed_turn_says_why_and_leaves_nothing_behind(make_client, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "fail")
    client = make_client(config())
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]

    events = stream(client, {"session": sid, "prompt": "hi"})

    assert [e["type"] for e in events] == ["error"]
    assert "No conversation found" in events[0]["detail"]
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["messages"] == [] and "claude" not in s     # the next turn starts fresh


def test_a_cli_that_dies_is_an_error_with_its_stderr(make_client, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "die")
    client = make_client(config())
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    (event,) = stream(client, {"session": sid, "prompt": "hi"})
    assert event["error"] == "claude stopped before answering"
    assert "the fake fell over" in event["detail"]


def test_a_missing_cli_is_refused_before_anything_runs(make_client):
    cfg = config()
    cfg.claude.command = ["no-such-claude-here"]
    client = make_client(cfg)
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    assert client.post("/api/chat/stream", json={"session": sid, "prompt": "hi"}).status_code == 409


def test_claude_needs_a_session(make_client):
    r = make_client(config()).post("/api/chat", json={"prompt": "hi", "voice": "claude"})
    assert r.status_code == 400


def test_one_turn_at_a_time_per_chat(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create(folder="den")["id"]
    turn = Turn({}, sid, "hi", claude={"prompt": "hi", "resume": None, "model": "", "system_prompt": ""})

    async def collect():
        return [e async for e in claude_answer(config(), store, turn, {sid})]

    (event,) = asyncio.run(collect())
    assert "still answering" in event["error"]


# ── Settings and config ──────────────────────────────────────────────────

def test_voice_is_a_setting_with_two_values(client):
    sid = client.post("/api/sessions", json={"folder": "f"}).json()["id"]
    assert client.patch(f"/api/sessions/{sid}", json={"settings": {"voice": "claude"}}).status_code == 200
    assert client.patch(f"/api/sessions/{sid}", json={"settings": {"voice": "hal"}}).status_code == 400


def test_the_shell_is_told_whether_claude_is_here(make_client):
    on = make_client(config()).get("/api/config").json()["claude"]
    assert on == {"name": "claude", "available": True}
    cfg = config()
    cfg.claude.enabled = False
    assert make_client(cfg).get("/api/config").json()["claude"]["available"] is False


def test_the_command_is_the_configs_and_the_prompt_never_an_argument():
    cfg = ClaudeConfig(command=["claude", "--fixed"], permission_mode="default",
                       allowed_tools=["mcp__workbench__*"], model="sonnet")
    argv = claude_voice.argv(cfg, resume=None, new_id="abc", system_prompt="sp")
    assert argv[1] == "--fixed" and "-p" in argv
    assert flag(argv, "--session-id") == "abc" and flag(argv, "--model") == "sonnet"
    assert flag(argv, "--permission-mode") == "default"
    assert flag(argv, "--allowedTools") == "mcp__workbench__*"
    assert flag(claude_voice.argv(cfg, resume="r1", new_id="x", model="opus"), "--model") == "opus"
