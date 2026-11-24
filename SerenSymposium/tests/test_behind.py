"""What the chats leave running outlives Symposium (23 Nov 2026): written
down as it is set aside, let go rather than killed when Symposium stops, and
picked up at the next start - told in the chat as it would have been."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from seren_symposium import behind, claude_voice

CONV = "1326de2a-f02c-471b-a6d3-b1ca639305af"


def test_the_ledger_keeps_what_is_put_and_forgets_what_is_dropped(tmp_path):
    led = behind.Ledger(tmp_path / "background.json")
    assert led.entries() == {}
    led.put(CONV, {"chat": "c1", "pid": 4})
    led.put("other", {"chat": "c2"})
    assert set(behind.Ledger(tmp_path / "background.json").entries()) == {CONV, "other"}, "on disk, not in memory"
    led.drop(CONV)
    assert set(led.entries()) == {"other"}
    (tmp_path / "background.json").write_text("{broken", encoding="utf-8")
    assert led.entries() == {}, "a broken file is nothing, not a crash"


def _record(kind, text, at):
    return {"type": kind, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(at)),
            "message": {"content": [{"type": "text", "text": text}]}}


def test_what_it_said_after_each_task_ended_is_read_back(tmp_path):
    """The shape measured on the real CLI: a `user` record holding
    <task-notification>, then its turn - maybe tools, then words."""
    t0 = time.time() - 600
    lines = [_record("assistant", "started", t0),
             _record("user", "<task-notification><task-id>old</task-id></task-notification>", t0 + 10),
             _record("assistant", "already told, before Symposium went", t0 + 11),
             _record("user", "<task-notification><task-id>a</task-id></task-notification>", t0 + 100),
             _record("assistant", "checking", t0 + 101),
             _record("assistant", "The build finished: 42/42.", t0 + 102),
             _record("user", "<task-notification><task-id>b</task-id></task-notification>", t0 + 200),
             _record("assistant", "And the flash went through.", t0 + 201)]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    assert behind.told_after(p, t0 + 50) == ["The build finished: 42/42.", "And the flash went through."]
    assert behind.told_after(p, t0 + 300) == []
    assert behind.told_after(None, 0) == []


def test_a_pid_passes_only_while_it_is_a_running_claude():
    assert not behind.pid_alive(None) and not behind.pid_alive(-1)
    assert not behind.pid_alive(os.getpid()), "alive, but python, not a claude: a reused pid must not pass"
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not behind.pid_alive(p.pid)


def test_a_task_that_exited_says_so(tmp_path):
    tasks = tmp_path / "claude" / "h" / CONV / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "a.output").write_bytes(b"all-done\n\n[exited with code 0]\n")
    (tasks / "b.output").write_bytes(b"still going\n")
    assert behind.task_ended(CONV, "a", [tmp_path / "claude"])["code"] == 0
    assert behind.task_ended(CONV, "b", [tmp_path / "claude"]) is None


def test_pick_up_tells_what_was_said_or_asks_when_nothing_was(tmp_path, monkeypatch):
    led = behind.Ledger(tmp_path / "background.json")
    t0 = time.time() - 600
    led.put("told", {"chat": "c1", "seat": None, "conversation": "told", "pid": 999999, "started": t0,
                     "tasks": [{"id": "a", "description": "build"}]})
    led.put("silent", {"chat": "c2", "seat": "kitchen", "conversation": "silent", "pid": 999998, "started": t0,
                       "tasks": [{"id": "b", "description": "flash"}]})
    tr = tmp_path / "told.jsonl"
    tr.write_text(json.dumps(_record("user", "<task-notification>a</task-notification>", t0 + 5)) + "\n"
                  + json.dumps(_record("assistant", "It built.", t0 + 6)) + "\n", encoding="utf-8")
    told, silent = [], []

    async def on_told(e, said): told.append((e["chat"], said))
    async def on_silent(e): silent.append((e["chat"], e["seat"]))

    let_go: dict = {}
    asyncio.run(behind.pick_up(led, let_go, lambda k: tr if k == "told" else None, on_told, on_silent, every=0.01))
    assert told == [("c1", ["It built."])] and silent == [("c2", "kitchen")]
    assert led.entries() == {} and let_go == {}, "crossed off once told"


def test_a_let_go_conversation_shows_as_running_and_its_stop_crosses_it_off(tmp_path, monkeypatch):
    led = behind.Ledger(tmp_path / "background.json")
    monkeypatch.setattr(claude_voice, "LEDGER", led)
    entry = {"chat": "c1", "conversation": CONV, "pid": 999999, "started": 100.0, "let_go": 200.0,
             "tasks": [{"id": "a", "type": "local_bash", "description": "build", "since": 150.0}]}
    led.put(CONV, entry)
    monkeypatch.setitem(claude_voice.LET_GO, CONV, entry)
    got = claude_voice.parked(CONV)
    assert got["tasks"] == ["build"] and got["let_go"] == 200.0 and got["details"][0]["id"] == "a"
    assert claude_voice.stop_parked(CONV) is True
    assert CONV not in claude_voice.LET_GO and led.entries() == {}
