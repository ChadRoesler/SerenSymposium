"""The claude voice asking may-I in the window instead of being refused:
the CLI starts seren_symposium.permit (an MCP server over stdio), it asks
Symposium over loopback, the ask shows in the chat's stream, and the answer
given there goes back. Run end to end against a real local server - the
fake CLI starts the real permission tool, exactly as the CLI does."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from seren_symposium import claude_voice, permit
from seren_symposium.app import create_app
from seren_symposium.config import FolderPreset, SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))
SRC = str(Path(__file__).resolve().parents[1])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Symposium serving on a real loopback port, so the permission tool -
    another process - can reach it."""
    monkeypatch.setenv("PYTHONPATH", SRC)          # the permission tool imports seren_symposium
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "runs.jsonl"))
    cfg = SymposiumConfig()
    cfg.ui.port = free_port()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.command = [sys.executable, FAKE]
    cfg.folders = [FolderPreset(name="den", voice="claude")]
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=cfg.ui.port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    base = f"http://127.0.0.1:{cfg.ui.port}"
    yield base, tmp_path
    server.should_exit = True
    t.join(timeout=10)


def turn(base: str, answer: str):
    """One streamed claude turn; when it asks, answer `answer`. Returns the
    events seen and the answer's response."""
    with httpx.Client(base_url=base, timeout=60) as c:
        sid = c.post("/api/sessions", json={"folder": "den"}).json()["id"]
        seen = []
        with c.stream("POST", "/api/chat/stream", json={"session": sid, "prompt": "read my notes"}) as r:
            for line in r.iter_lines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                seen.append(ev)
                if ev["type"] == "permission":
                    got = c.post("/api/permit/answer", json={"id": ev["id"], "answer": answer})
                    assert got.status_code == 200, got.text
        done = next(e for e in seen if e["type"] == "done")
        return seen, done["response"], c


ASK = {"tool_name": "Read", "input": {"file_path": "C:/work/project/notes.txt"}}


def test_an_ask_is_shown_and_once_lets_it_through(live, monkeypatch):
    base, tmp = live
    monkeypatch.setenv("FAKE_CLAUDE_ASK", json.dumps(ASK))
    seen, said, _ = turn(base, "once")
    (ask,) = [e for e in seen if e["type"] == "permission"]
    assert ask["tool"] == "Read" and ask["summary"] == "C:/work/project/notes.txt"
    assert ask["rule"] == "Read(//c/work/project/**)"
    assert said == "asked: allow"
    assert not (tmp / "symposium-prefs.json").exists(), "once is not kept"
    # The ticket rides in the environment, never the command line.
    run = json.loads((tmp / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "--permission-prompt-tool" in run["argv"] and not any("SYMPOSIUM_PERMIT" in a for a in run["argv"])


def test_always_is_kept_for_the_next_turn(live, monkeypatch):
    base, tmp = live
    monkeypatch.setenv("FAKE_CLAUDE_ASK", json.dumps(ASK))
    _, said, _ = turn(base, "always")
    assert said == "asked: allow"
    kept = json.loads((tmp / "symposium-prefs.json").read_text(encoding="utf-8"))
    assert kept["claude_tools"] == ["Read(//c/work/project/**)"]
    with httpx.Client(base_url=base) as c:
        assert c.get("/api/claude").json()["tools"] == ["Read(//c/work/project/**)"]
    monkeypatch.delenv("FAKE_CLAUDE_ASK")
    turn(base, "once")
    run = json.loads((tmp / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "Read(//c/work/project/**)" in run["argv"], "the next turn has it on its list"


def test_deny_says_so(live, monkeypatch):
    base, _ = live
    monkeypatch.setenv("FAKE_CLAUDE_ASK", json.dumps(ASK))
    _, said, _ = turn(base, "deny")
    assert said == "asked: deny"


def test_no_turn_no_permission(live):
    base, _ = live
    with httpx.Client(base_url=base) as c:
        r = c.post("/api/permit", json={"ticket": "made-up", "tool_name": "Bash", "input": {"command": "rm -rf /"}})
        assert r.json()["behavior"] == "deny"
        assert c.post("/api/permit/answer", json={"id": "nope", "answer": "once"}).status_code == 404
        assert c.post("/api/permit/answer", json={"id": "nope", "answer": "sure"}).status_code == 400


def test_asking_can_be_turned_off(tmp_path):
    cfg = SymposiumConfig()
    assert "--permission-prompt-tool" in claude_voice.argv(cfg.claude, resume=None, new_id="x", ask=True)
    assert "--permission-prompt-tool" not in claude_voice.argv(cfg.claude, resume=None, new_id="x")


@pytest.mark.parametrize("tool, given, rule", [
    ("Read", {"file_path": "C:/work/project/notes.txt"}, "Read(//c/work/project/**)"),
    ("Edit", {"file_path": "/home/alice/notes/today.md"}, "Edit(//home/alice/notes/**)"),
    ("Write", {"file_path": "D:\\work\\project\\out.txt"}, "Edit(//d/work/project/**)"),
    ("Grep", {"pattern": "x", "path": "/srv/logs"}, "Read(//srv/logs/**)"),
    ("Bash", {"command": "  git status --short"}, "Bash(git:*)"),
    ("WebFetch", {"url": "https://example.com/a?b=c"}, "WebFetch(domain:example.com)"),
    ("WebSearch", {"query": "krater"}, "WebSearch"),
    ("mcp__workbench__search", {"q": 1}, "mcp__workbench__search"),
])
def test_always_is_no_wider_than_what_was_asked(tool, given, rule):
    assert claude_voice.suggest_rule(tool, given) == rule


def test_the_ask_reads_as_a_line():
    assert claude_voice.summarize("Bash", {"command": "ls -la"}) == "ls -la"
    assert claude_voice.summarize("Thing", {"a": "x" * 500}).endswith("…")


def test_the_tool_without_symposium_refuses(monkeypatch):
    monkeypatch.delenv(permit.URL_ENV, raising=False)
    assert permit.ask("Read", {})["behavior"] == "deny"
    out = permit.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert out["result"]["tools"][0]["name"] == "permit"
    assert permit.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert permit.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"] == -32601


# ── Always, kept where it was asked to be (22 Oct 2026) ───────────────────

MCP_ASK = {"tool_name": "mcp__wren-workbench__recall", "input": {"query": "the eval"}}


def _chat_turn(c, sid, answer=None, scope=None, rule_index=0):
    """One streamed turn in chat `sid`; an ask is answered `answer` (and, for
    Always, kept in `scope` at the width `rule_index`). An ask with no answer
    to give fails the test."""
    seen = []
    with c.stream("POST", "/api/chat/stream", json={"session": sid, "prompt": "go"}) as r:
        for line in r.iter_lines():
            if not line.strip():
                continue
            ev = json.loads(line)
            seen.append(ev)
            if ev["type"] == "permission":
                assert answer, f"asked when it should not have: {ev}"
                body = {"id": ev["id"], "answer": answer}
                if scope:
                    body.update(scope=scope, rule=ev["rules"][rule_index])
                got = c.post("/api/permit/answer", json=body)
                assert got.status_code == 200, got.text
    return seen


def test_always_in_a_chat_or_a_project_is_kept_there_and_answers_the_next_ask(live, monkeypatch):
    base, tmp = live
    monkeypatch.setenv("FAKE_CLAUDE_ASK", json.dumps(MCP_ASK))
    with httpx.Client(base_url=base, timeout=60) as c:
        sid = c.post("/api/sessions", json={"folder": "den"}).json()["id"]
        seen = _chat_turn(c, sid, "always", "chat", rule_index=1)
        ask = next(e for e in seen if e["type"] == "permission")
        assert ask["rules"] == ["mcp__wren-workbench__recall", "mcp__wren-workbench__*"]
        assert ask["scopes"] == ["chat", "project", "all"]
        assert c.get(f"/api/sessions/{sid}").json()["settings"]["allowed_tools"] == ["mcp__wren-workbench__*"]
        assert not (tmp / "symposium-prefs.json").exists(), "a chat's Always is not every chat's"

        # the fake asks every turn; in this chat the rule now answers it, no card
        again = _chat_turn(c, sid)
        assert next(e for e in again if e["type"] == "done")["response"] == "asked: allow"

        # another chat in the project still asks; Always for the project keeps it there
        other = c.post("/api/sessions", json={"folder": "den"}).json()["id"]
        seen = _chat_turn(c, other, "always", "project")
        assert [e for e in seen if e["type"] == "permission"]
        kept = json.loads((tmp / "symposium-prefs.json").read_text(encoding="utf-8"))
        den = next(p for p in kept["projects"] if p["name"] == "den")
        assert den["allowed_tools"] == ["mcp__wren-workbench__recall"] and "claude_tools" not in kept
        third = c.post("/api/sessions", json={"folder": "den"}).json()["id"]
        assert next(e for e in _chat_turn(c, third) if e["type"] == "done")["response"] == "asked: allow", \
            "a new chat in the project is covered by the project's rule"
        assert c.post("/api/permit/answer", json={"id": "x", "answer": "always", "scope": "galaxy"}).status_code == 400
