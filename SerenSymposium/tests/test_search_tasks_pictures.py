"""23 Nov 2026, Chad's run-through: every match in one chat behind the
sidebar's "and N more"; your own pictures folded on their own setting; and a
task left running, looked into - what it ran, for how long, its output."""
from __future__ import annotations

import json
import time

import pytest

from seren_symposium import claude_voice, sessions, terminal
from seren_symposium.config import SymposiumConfig


def cfg_at(tmp_path) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.projects_dir = str(tmp_path / "projects")
    return cfg


def test_and_n_more_opens_every_match_in_the_chat(make_client, tmp_path):
    tc = make_client(cfg_at(tmp_path))
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"line {i} about the lantern"} for i in range(12)]
    sid = tc.post("/api/sessions", json={"folder": "", "title": "t", "messages": msgs}).json()["id"]
    few = tc.get("/api/sessions/search", params={"q": "lantern"}).json()["sessions"][0]
    assert few["hits"] == 12 and len(few["matches"]) == 3
    every = tc.get("/api/sessions/search", params={"q": "lantern", "chat": sid}).json()["sessions"]
    assert len(every) == 1 and [m["index"] for m in every[0]["matches"]] == list(range(11, -1, -1)), "all, newest first"
    assert tc.get("/api/sessions/search", params={"q": "nowhere", "chat": sid}).json()["sessions"] == []
    assert tc.get("/api/sessions/search", params={"q": "lantern", "chat": "nope"}).json()["sessions"] == []


def test_your_pictures_have_their_own_setting(make_client, tmp_path):
    assert sessions.clean_settings({"my_pictures": "folded"}) == {"my_pictures": "folded"}
    with pytest.raises(sessions.Invalid):
        sessions.clean_settings({"my_pictures": "sideways"})
    tc = make_client(cfg_at(tmp_path))
    assert tc.get("/api/config").json()["my_pictures"] == "", "unset: yours go as everyone's"
    assert tc.patch("/api/prefs", json={"my_pictures": "folded"}).status_code < 300
    assert tc.get("/api/config").json()["my_pictures"] == "folded"
    assert tc.patch("/api/prefs", json={"my_pictures": "sideways"}).status_code == 400
    assert tc.patch("/api/prefs", json={"my_pictures": ""}).status_code < 300


CONV = "1326de2a-f02c-471b-a6d3-b1ca639305af"


def test_a_running_task_is_looked_into(tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"message": {"content": [{"type": "tool_use", "id": "toolu_a", "name": "Bash",
                                  "input": {"command": "idf.py build", "run_in_background": True}}]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_a",
                                  "content": "Command running in background with ID: b2v5pp0i9"}]}},
        {"message": {"content": [{"type": "tool_use", "id": "toolu_b", "name": "BashOutput", "input": {"id": "b2v5pp0i9"}}]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_b", "content": "b2v5pp0i9: still going"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    tasks = tmp_path / "tmp" / "claude" / "abc123" / CONV / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "b2v5pp0i9.output").write_bytes(("x" * 30000 + "\n[42/42] Linking firmware.elf\n").encode("utf-8"))

    got = terminal.task_info(transcript, CONV, "b2v5pp0i9", dirs=[tmp_path / "tmp" / "claude"])
    assert got["started_by"] == {"tool": "Bash", "input": {"command": "idf.py build", "run_in_background": True}, "id": "toolu_a"}, \
        "the call that started it, not a later one reading its output"
    assert got["output"]["tail"].endswith("Linking firmware.elf\n") and got["output"]["cut"]
    assert got["output"]["size"] > terminal.TASK_TAIL

    assert terminal.task_info(transcript, CONV, "../../etc", dirs=[tmp_path / "tmp" / "claude"]) == \
        {"started_by": None, "output": None}
    assert terminal.task_info(None, CONV, "nothere", dirs=[tmp_path / "tmp" / "claude"]) == {"started_by": None, "output": None}


def test_the_window_is_told_each_tasks_id_and_since(monkeypatch):
    class FakeCli:
        started = 100.0
        tasks = [{"task_id": "b2v5pp0i9", "task_type": "local_bash", "description": "build the firmware"}]
        task_seen = {"b2v5pp0i9": 150.0}
        def alive(self): return True
    monkeypatch.setitem(claude_voice.PARKED, CONV, FakeCli())
    got = claude_voice.parked(CONV)
    assert got["tasks"] == ["build the firmware"]
    assert got["details"] == [{"id": "b2v5pp0i9", "type": "local_bash", "description": "build the firmware", "since": 150.0}]


def test_a_task_not_running_here_is_a_404(make_client, tmp_path):
    tc = make_client(cfg_at(tmp_path))
    sid = tc.post("/api/sessions", json={"folder": ""}).json()["id"]
    assert tc.get(f"/api/sessions/{sid}/background/b2v5pp0i9").status_code == 404
    assert tc.get("/api/sessions/nope/background/b2v5pp0i9").status_code == 404
