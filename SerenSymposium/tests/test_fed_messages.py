"""A message fed into a chat by something other than the person (17 Oct
2026): marked with its source on the server, drawn as that source's by the
shell, never as the person's. The feeder itself (a game watcher) lives
outside this tree; what stays here is the mark and the rule that no request
body can set it."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from seren_symposium.app import LiveTurn, claude_answer, plan_turn
from seren_symposium.config import FolderPreset, SymposiumConfig
from seren_symposium.sessions import clean_messages

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def test_import_keeps_who_fed_a_message():
    kept = clean_messages([{"role": "user", "content": "x", "source": "table", "by": "Wren's eyes"},
                           {"role": "user", "content": "y", "by": "nope"}])
    assert kept[0]["source"] == "table" and kept[0]["by"] == "Wren's eyes"
    assert "by" not in kept[1] and "source" not in kept[1]


def test_a_fed_turn_is_marked_and_a_request_body_cannot_fake_it(make_client):
    cfg = SymposiumConfig()
    cfg.claude.command = [sys.executable, FAKE]
    cfg.folders = [FolderPreset(name="den", voice="claude")]
    client = make_client(cfg)
    app = client.app
    sid = client.post("/api/sessions", json={"folder": "den", "title": "fed"}).json()["id"]

    def wait_for(n):
        for _ in range(200):
            s = client.get(f"/api/sessions/{sid}").json()
            if len(s["messages"]) >= n:
                return s
            time.sleep(0.05)
        raise AssertionError(f"the chat never reached {n} messages")

    # Something on the server side speaks into the chat: it marks the turn.
    async def fed():
        store = app.state.sessions
        turn = plan_turn(cfg, store, {"session": sid, "prompt": "a fight begins"}, None)
        turn.source, turn.by = "table", "Wren's eyes"
        LiveTurn.start(app.state, sid, claude_answer(cfg, store, turn, set(), app.state.permits))

    client.portal.call(fed)
    s = wait_for(2)
    asked, said = s["messages"][0], s["messages"][1]
    assert asked["role"] == "user" and asked["source"] == "table" and asked["by"] == "Wren's eyes"
    assert said["role"] == "assistant" and "source" not in said
    mine = next(x for x in client.get("/api/sessions").json()["sessions"] if x["id"] == sid)
    assert mine["sources"] == ["table"]

    # A body that claims a source is just a body: the mark is the server's alone.
    r = client.post("/api/chat/stream", json={"session": sid, "prompt": "hi", "source": "table", "by": "x"})
    assert r.status_code == 200
    s = wait_for(4)
    assert s["messages"][2]["content"] == "hi"
    assert "source" not in s["messages"][2] and "by" not in s["messages"][2]