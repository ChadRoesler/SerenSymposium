"""What of the house a chat or a project leaves out (26 Nov 2026). Chad: an
NPC on a small Haiku "would get the preface, the framing ... we'd want to be
able to uncheck passing some of those things so they don't get passed in
specific sessions". Each of Seren's layers - Claude Code's own prompt, the
preface, the framing, the memory files, the session-start hooks (the
bookmark), the house's tools - can be left out, by the project (the campaign)
or the chat, the chat's choice over the project's."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from seren_symposium import stack
from seren_symposium.config import FolderPreset, SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def cfg_at(tmp_path) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.command = [sys.executable, FAKE]
    cfg.claude.projects_dir = str(tmp_path / "projects")
    cfg.claude.carabiner = str(tmp_path / "no-carabiner")
    cfg.claude.framing = "You are answering in Seren Symposium. FRAMING-MARK"
    cfg.claude.preface.file = str(tmp_path / "preface.md")
    (tmp_path / "preface.md").write_text("You are Wren. PREFACE-MARK", encoding="utf-8")
    cfg.claude.mcp_servers = ["wren-workbench"]
    cfg.folders = [FolderPreset(name="campaign", voice="claude", system_prompt="The world of Vellmarch.",
                                leave_out=["preface", "framing", "hooks", "tools", "memory", "cli"]),
                   FolderPreset(name="home", voice="claude")]
    return cfg


@pytest.fixture
def runs(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return lambda: [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def turn(tc, folder, settings=None):
    sid = tc.post("/api/sessions", json={"folder": folder, "settings": settings or {}}).json()["id"]
    r = tc.post("/api/chat/stream", json={"session": sid, "prompt": "hi"})
    assert r.status_code == 200, r.text
    return sid


def test_a_campaign_leaves_all_of_seren_out(make_client, tmp_path, runs):
    tc = make_client(cfg_at(tmp_path))
    turn(tc, "campaign")
    run = runs()[-1]
    system = flag(run["argv"], "--system-prompt")
    assert system == "The world of Vellmarch.", "the CLI's prompt replaced by the chat's own words alone"
    assert "--append-system-prompt" not in run["argv"]
    assert json.loads(flag(run["argv"], "--settings")) == {"disableAllHooks": True}, "no bookmark"
    assert "--strict-mcp-config" in run["argv"] and "wren-workbench" not in json.dumps(run["argv"]), "no house tools"
    assert run["env"].get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1" and run["env"].get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_home_passes_everything_as_before(make_client, tmp_path, runs):
    tc = make_client(cfg_at(tmp_path))
    turn(tc, "home")
    run = runs()[-1]
    system = flag(run["argv"], "--system-prompt")
    assert "PREFACE-MARK" in system and "FRAMING-MARK" in system
    assert "--settings" not in run["argv"] and "CLAUDE_CODE_DISABLE_CLAUDE_MDS" not in run["env"]


def test_the_chat_says_over_its_project(make_client, tmp_path, runs):
    tc = make_client(cfg_at(tmp_path))
    turn(tc, "campaign", {"leave_out": []})                 # this one table wants the whole house
    run = runs()[-1]
    assert "PREFACE-MARK" in flag(run["argv"], "--system-prompt") and "--settings" not in run["argv"]
    turn(tc, "home", {"leave_out": ["framing", "hooks"]})   # and a chat at home can shed a little
    run = runs()[-1]
    system = flag(run["argv"], "--system-prompt")
    assert "PREFACE-MARK" in system and "FRAMING-MARK" not in system
    assert json.loads(flag(run["argv"], "--settings")) == {"disableAllHooks": True}
    assert tc.post("/api/sessions", json={"folder": "home", "settings": {"leave_out": ["the-moon"]}}).status_code == 400


def test_left_out_with_nothing_said_still_says_something(make_client, tmp_path, runs):
    """The CLI's prompt left out and no words of the chat's own: a line, not nothing."""
    tc = make_client(cfg_at(tmp_path))
    turn(tc, "home", {"leave_out": ["cli", "preface", "framing"]})
    assert flag(runs()[-1]["argv"], "--system-prompt") == "Answer in this conversation."


def test_the_stack_shows_what_is_passed_and_whose_choice(make_client, tmp_path, runs):
    tc = make_client(cfg_at(tmp_path))
    st = tc.get("/api/stack", params={"folder": "campaign"}).json()
    assert st["leave_out_from"] == "project" and set(st["leave_out"]) == set(stack.LEAVABLE)
    by = {l["key"]: l for l in st["layers"]}
    assert by["preface"]["state"] == "off" and by["framing"]["state"] == "off" and by["hooks"]["state"] == "off"
    assert by["cli"]["state"] == "replaced" and by["cli"]["left_out"] is True and by["tools"]["leavable"] is True
    sid = turn(tc, "campaign", {"leave_out": ["hooks"]})
    st = tc.get("/api/stack", params={"session": sid}).json()
    assert st["leave_out"] == ["hooks"] and st["leave_out_from"] == "chat"
    by = {l["key"]: l for l in st["layers"]}
    assert by["framing"]["state"] == "on" and by["hooks"]["left_out"] is True


def test_a_project_takes_it_from_the_window(make_client, tmp_path, runs):
    tc = make_client(cfg_at(tmp_path))
    r = tc.patch("/api/projects", json={"name": "home", "set": {"leave_out": ["tools", "memory"]}})
    assert r.status_code < 300, r.text
    assert set(tc.get("/api/stack", params={"folder": "home"}).json()["leave_out"]) == {"tools", "memory"}
    assert tc.patch("/api/projects", json={"name": "home", "set": {"leave_out": ["nope"]}}).status_code == 400
