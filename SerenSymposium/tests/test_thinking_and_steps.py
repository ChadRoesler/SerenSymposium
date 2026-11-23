"""How hard Claude thinks and what it shows of it, in the permissions' layers
(chat, project, every chat, carabiner, the CLI); and a tool call opened in
full from the CLI's transcript, where a turn keeps only a preview. Chad, 22
Nov 2026: a cut-off tool result is no help when the call went wrong."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from seren_symposium import permissions, sessions
from seren_symposium.config import FolderPreset, SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))

CLIP = """carabiner: claude
session:
  model: claude-haiku-5-5
  effort: low
  thinking_display: summarized
"""


def cfg_with(tmp_path, carabiner: str = "", **claude) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.command = [sys.executable, FAKE]
    cfg.claude.carabiner = str(tmp_path / "no-carabiner-here")
    cfg.claude.projects_dir = str(tmp_path / "projects")
    if carabiner:
        (tmp_path / "claude.yaml").write_text(carabiner, encoding="utf-8")
        cfg.claude.carabiner = str(tmp_path / "claude.yaml")
    for k, v in claude.items():
        setattr(cfg.claude, k, v)
    cfg.folders = [FolderPreset(name="den", voice="claude", effort="high"),
                   FolderPreset(name="plain", voice="claude"),
                   FolderPreset(name="lab", effort="max")]          # a local model's project: no say
    return cfg


def test_the_most_particular_layer_with_an_opinion_wins(tmp_path):
    cfg = cfg_with(tmp_path, carabiner=CLIP)
    t = permissions.thinking(cfg, cfg.folder("plain"), {})
    assert (t["effort"], t["effort_from"]) == ("low", "carabiner")
    assert (t["thinking_display"], t["thinking_from"]) == ("summarized", "carabiner")
    cfg.claude.effort = "medium"
    assert permissions.thinking(cfg, cfg.folder("plain"), {})["effort_from"] == "symposium"
    assert permissions.thinking(cfg, cfg.folder("den"), {})["effort"] == "high"
    assert permissions.thinking(cfg, cfg.folder("lab"), {})["effort"] == "medium", "only a claude project counts"
    chat = permissions.thinking(cfg, cfg.folder("den"), {"effort": "xhigh", "thinking_display": "omitted"})
    assert (chat["effort"], chat["effort_from"], chat["thinking_display"]) == ("xhigh", "chat", "omitted")
    bare = cfg_with(tmp_path / "b")
    assert permissions.thinking(bare, None, {}) == {"effort": "", "effort_from": "cli",
                                                    "thinking_display": "", "thinking_from": "cli"}


def test_the_carabiner_says_only_what_the_cli_takes(tmp_path):
    cfg = cfg_with(tmp_path, carabiner="session:\n  effort: x&calc.exe\n  thinking_display: raw\n  model: a b\n")
    k = permissions.carabiner_session(cfg)
    assert (k["effort"], k["thinking_display"], k["model"]) == ("", "", "")


FAKE_KBH = """import json, sys
assert sys.argv[1:5] == ["claude", "config", "show", "--json"], sys.argv
print(json.dumps({"fields": [{"key": "session.model", "choices": [
    {"value": "", "label": "Claude Code's own default"},
    {"value": "claude-fable-9", "label": "Claude Fable 9"},
    {"value": "claude-opus-5-5", "label": "Claude Opus 5.5"}]}]}))
"""


def test_the_model_list_is_the_carabiners(make_client, tmp_path):
    from seren_symposium import carabiner
    carabiner._models_cache.clear()
    cfg = cfg_with(tmp_path, carabiner=CLIP + f"python: {sys.executable}\n")
    (tmp_path / "claude.kbh").write_text(FAKE_KBH, encoding="utf-8")
    tc = make_client(cfg)
    got = tc.get("/api/claude/models").json()
    assert got == {"source": "carabiner", "models": [{"value": "claude-fable-9", "label": "Claude Fable 9"},
                                                      {"value": "claude-opus-5-5", "label": "Claude Opus 5.5"}]}

    (tmp_path / "claude.kbh").write_text("print('not json')\n", encoding="utf-8")
    assert tc.get("/api/claude/models").json()["source"] == "carabiner", "kept a while"
    got = tc.get("/api/claude/models", params={"fresh": 1}).json()
    assert got["source"] == "symposium" and {"value": "opus", "label": "opus"} in got["models"], \
        "a carabiner that does not answer: the yaml's list"
    carabiner._models_cache.clear()
    bare = make_client(cfg_with(tmp_path / "b"))
    assert bare.get("/api/claude/models").json()["source"] == "symposium", "nothing clipped"


def test_a_bad_value_is_refused_at_the_door():
    assert sessions.clean_settings({"effort": "high", "thinking_display": "summarized"}) == \
        {"effort": "high", "thinking_display": "summarized"}
    for bad in ({"effort": "eleven"}, {"thinking_display": "everything"}):
        with pytest.raises(sessions.Invalid):
            sessions.clean_settings(bad)


def _turn(tc, log, folder, settings=None):
    sid = tc.post("/api/sessions", json={"folder": folder, "settings": settings or {}}).json()["id"]
    r = tc.post("/api/chat/stream", json={"session": sid, "prompt": "hi"})
    assert r.status_code == 200, r.text
    return sid, json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["argv"]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def test_a_turn_carries_the_layers_to_the_cli(make_client, tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    tc = make_client(cfg_with(tmp_path, carabiner=CLIP))
    _, argv = _turn(tc, log, "plain")
    assert (flag(argv, "--effort"), flag(argv, "--thinking-display"), flag(argv, "--model")) == \
        ("low", "summarized", "claude-haiku-5-5"), "the carabiner's, nothing more particular"
    _, argv = _turn(tc, log, "den", {"thinking_display": "omitted", "model_override": "opus"})
    assert (flag(argv, "--effort"), flag(argv, "--thinking-display"), flag(argv, "--model")) == ("high", "omitted", "opus")

    bare_log = tmp_path / "bare.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(bare_log))
    _, argv = _turn(make_client(cfg_with(tmp_path / "b")), bare_log, "plain")
    assert "--effort" not in argv and "--thinking-display" not in argv, "no opinion anywhere: the CLI's own"


def test_a_tool_call_opens_in_full_from_the_transcript(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "runs.jsonl"))
    tc = make_client(cfg_with(tmp_path))
    sid = tc.post("/api/sessions", json={"folder": "plain"}).json()["id"]
    assert tc.post("/api/chat/stream", json={"session": sid, "prompt": "hi"}).status_code == 200
    s = tc.get(f"/api/sessions/{sid}").json()
    step = next(x for x in s["messages"][1]["meta"]["steps"] if x["kind"] == "tool")
    assert step["sid"] == s["claude"]["session"]

    long = "x" * 5000 + " the end"
    folder = tmp_path / "projects" / "D--somewhere"
    folder.mkdir(parents=True)
    lines = [{"type": "assistant", "message": {"content": [
                 {"type": "tool_use", "id": step["id"], "name": "mcp__workbench__get_time", "input": {"zone": "UTC"}}]}},
             {"type": "user", "message": {"content": [
                 {"type": "tool_result", "tool_use_id": step["id"], "content": [{"type": "text", "text": long}]}]}}]
    (folder / f"{step['sid']}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    got = tc.get(f"/api/sessions/{sid}/steps/{step['id']}").json()
    assert got == {"tool": "mcp__workbench__get_time", "input": {"zone": "UTC"}, "ok": True, "result": long, "cut": False}
    assert tc.get(f"/api/sessions/{sid}/steps/toolu_nobody").status_code == 404
    assert tc.get(f"/api/sessions/{sid}/steps/..%2F..%2Fetc").status_code == 404
    assert tc.get(f"/api/sessions/nope/steps/{step['id']}").status_code == 404
