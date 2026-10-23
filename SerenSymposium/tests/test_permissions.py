"""The claude voice's permissions in layers (22 Oct 2026): the CLI's own,
then the carabiner's, then every chat's, then a project's, then a chat's.
The mode is the most particular one set; the allow list gathers from the
chat upward until a layer says not to inherit; Always is kept in the scope
it was asked for, in the width it was asked for."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from seren_symposium import claude_voice, permissions
from seren_symposium.config import FolderPreset, SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def cfg_with(tmp_path, *, carabiner: str = "", **claude) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.command = [sys.executable, FAKE]
    cfg.claude.carabiner = str(tmp_path / "no-carabiner-here")
    if carabiner:
        (tmp_path / "claude.yaml").write_text(carabiner, encoding="utf-8")
        cfg.claude.carabiner = str(tmp_path / "claude.yaml")
    for k, v in claude.items():
        setattr(cfg.claude, k, v)
    cfg.folders = [FolderPreset(name="redteam", voice="claude", permission_mode="manual",
                                allowed_tools=["Read"], tools_inherit=False),
                   FolderPreset(name="den", voice="claude", allowed_tools=["mcp__wren-workbench__*"])]
    return cfg


CLIP = """carabiner: claude
session:
  permission_mode: acceptEdits
  allowed_tools:
    - mcp__wren-workbench__recall
"""


def test_the_mode_is_the_most_particular_one_set_and_the_list_gathers_upward(tmp_path):
    cfg = cfg_with(tmp_path, carabiner=CLIP, permission_mode="plan", allowed_tools=["Bash(git:*)"])
    den = permissions.for_chat(cfg, "den", {})
    assert (den["mode"], den["mode_from"]) == ("plan", "symposium"), "every chat's beats the carabiner's"
    assert den["allow"] == ["mcp__wren-workbench__*", "Bash(git:*)", "mcp__wren-workbench__recall"]
    assert den["from"]["mcp__wren-workbench__recall"] == "carabiner"

    chat = permissions.for_chat(cfg, "den", {"permission_mode": "auto", "allowed_tools": ["WebFetch"]})
    assert (chat["mode"], chat["mode_from"]) == ("auto", "chat") and chat["allow"][0] == "WebFetch"

    red = permissions.for_chat(cfg, "redteam", {})
    assert red["mode"] == "manual" and red["allow"] == ["Read"] and red["stopped_at"] == "project", \
        "a project that does not inherit asks about everything else"
    alone = permissions.for_chat(cfg, "den", {"tools_inherit": False, "allowed_tools": ["Read"]})
    assert alone["allow"] == ["Read"], "so does a chat"

    bare = cfg_with(tmp_path)
    assert permissions.for_chat(bare, "", {}) == {**permissions.for_chat(bare, "", {}), "mode": "", "mode_from": "cli"}


def test_the_carabiners_session_block_is_read_and_a_broken_one_is_nothing(tmp_path):
    cfg = cfg_with(tmp_path, carabiner=CLIP)
    got = permissions.carabiner_session(cfg)
    assert got["permission_mode"] == "acceptEdits" and got["allowed_tools"] == ["mcp__wren-workbench__recall"]
    (tmp_path / "claude.yaml").write_text("session:\n  allowed_tools: Read, Bash(git:*)\n", encoding="utf-8")
    assert permissions.carabiner_session(cfg)["allowed_tools"] == ["Read", "Bash(git:*)"], "the stdlib reader's commas"
    (tmp_path / "claude.yaml").write_text("session: [not, a, block", encoding="utf-8")
    assert permissions.carabiner_session(cfg) == {}
    (tmp_path / "claude.yaml").write_text("session:\n  permission_mode: yolo\n", encoding="utf-8")
    assert permissions.carabiner_session(cfg)["permission_mode"] == "", "a mode the CLI does not know is dropped"


@pytest.mark.parametrize("rule, tool, given, yes", [
    ("mcp__wren-workbench__*", "mcp__wren-workbench__recall", {}, True),
    ("mcp__wren-workbench", "mcp__wren-workbench__recall", {}, True),
    ("mcp__wren-workbench__*", "mcp__other__recall", {}, False),
    ("Read", "Grep", {"pattern": "x"}, True),
    ("Read", "Edit", {"file_path": "C:/a/b.txt"}, False),
    ("Read(//c/work/project/**)", "Read", {"file_path": "C:\\work\\project\\notes.txt"}, True),
    ("Read(//c/work/project/**)", "Read", {"file_path": "C:/work/projectile/notes.txt"}, False),
    ("Edit(//c/work/**)", "Write", {"file_path": "C:/work/a/b.py"}, True),
    ("Bash(git:*)", "Bash", {"command": "git status"}, True),
    ("Bash(git:*)", "Bash", {"command": "gitk"}, False),
    ("WebFetch(domain:example.org)", "WebFetch", {"url": "https://docs.example.org/x"}, True),
    ("WebFetch(domain:example.org)", "WebFetch", {"url": "https://example.org.evil.net/"}, False),
    ("Bash(rm -rf /)", "Bash", {"command": "rm -rf /"}, False),
])
def test_a_rule_covers_only_what_it_plainly_says(rule, tool, given, yes):
    assert permissions.covers(rule, tool, given) is yes


def test_always_is_offered_in_widths_narrowest_first():
    assert claude_voice.suggest_rules("mcp__wren-workbench__recall", {}) == \
        ["mcp__wren-workbench__recall", "mcp__wren-workbench__*"]
    assert claude_voice.suggest_rules("Read", {"file_path": "C:/work/a.txt"}) == ["Read(//c/work/**)", "Read"]
    assert claude_voice.suggest_rules("Bash", {"command": "git log"}) == ["Bash(git:*)", "Bash"]
    assert claude_voice.suggest_rules("TodoWrite", {}) == ["TodoWrite"]


def test_a_turn_runs_with_its_layers(make_client, tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    cfg = cfg_with(tmp_path, carabiner=CLIP, allowed_tools=["Bash(git:*)"])
    tc = make_client(cfg)

    def run(folder, settings=None):
        sid = tc.post("/api/sessions", json={"folder": folder, "settings": settings or {}}).json()["id"]
        r = tc.post("/api/chat/stream", json={"session": sid, "prompt": "hi"})
        assert r.status_code == 200, r.text
        argv = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["argv"]
        tools = argv[argv.index("--allowedTools") + 1:]
        tools = tools[:next((i for i, a in enumerate(tools) if a.startswith("--")), len(tools))]
        return argv, tools, sid

    argv, tools, sid = run("den")
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits", "the carabiner's mode, nothing more particular"
    assert {"mcp__wren-workbench__*", "Bash(git:*)", "mcp__wren-workbench__recall"} <= set(tools)
    assert any(t.startswith("Edit(") for t in tools), "the chat's own folder stays writable"

    argv, tools, _ = run("redteam")
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert "Read" in tools and "Bash(git:*)" not in tools and "mcp__wren-workbench__recall" not in tools

    eff = tc.get("/api/claude", params={"session": sid}).json()["effective"]
    assert eff["mode_from"] == "carabiner" and eff["counts"]["project"] == 1
    draft = tc.get("/api/claude", params={"folder": "den", "settings": json.dumps({"permission_mode": "plan"})}).json()
    assert draft["effective"]["mode"] == "plan" and draft["carabiner_session"]["permission_mode"] == "acceptEdits"


def test_the_settings_take_the_new_keys_and_refuse_bad_ones(make_client, tmp_path):
    tc = make_client(cfg_with(tmp_path))
    ok = tc.post("/api/sessions", json={"folder": "den", "settings": {
        "permission_mode": "auto", "allowed_tools": [" Read ", "Read", "Bash(git:*)"], "tools_inherit": False}})
    assert ok.status_code == 201, ok.text
    assert ok.json()["settings"] == {"permission_mode": "auto", "allowed_tools": ["Read", "Bash(git:*)"],
                                     "tools_inherit": False}
    for bad in ({"permission_mode": "bypassPermissions"}, {"allowed_tools": "Read"}, {"tools_inherit": "no"}):
        assert tc.post("/api/sessions", json={"folder": "den", "settings": bad}).status_code == 400, bad
    r = tc.patch("/api/projects", json={"name": "den", "set": {"permission_mode": "plan", "allowed_tools": ["Read"],
                                                                "tools_inherit": False}})
    assert r.status_code == 200 and r.json()["tools_inherit"] is False and r.json()["allowed_tools"] == ["Read"]
    assert tc.patch("/api/projects", json={"name": "den", "set": {"permission_mode": "yolo"}}).status_code == 400
