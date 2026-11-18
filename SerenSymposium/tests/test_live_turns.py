"""A turn outlives the window that asked for it (LiveTurn, 15 Oct 2026).

Before: the response generator WAS the turn. Close the window, or click
another chat while the answer was coming, and the generator closed, the CLI
was killed mid-thought and the answer - reasoning, tool calls and all - was
gone. Now the server runs the turn on its own; a window only follows it,
from the first event if it arrives late (GET /api/sessions/{id}/live), and
only the chat being answered refuses a new message.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import httpx

from seren_symposium.app import LiveTurn
from seren_symposium.config import FolderPreset, SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def config() -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.claude.command = [sys.executable, FAKE]
    cfg.folders = [FolderPreset(name="den", voice="claude"), FolderPreset(name="lab", model="qwen-local")]
    return cfg


def _events(text: str) -> list[dict]:
    return [json.loads(l) for l in text.splitlines() if l.strip()]


# ── The object ────────────────────────────────────────────────────────────

def test_a_follower_that_leaves_does_not_stop_the_turn():
    """Two followers: one reads two events and stops, the other reads to the
    end - and gets everything, including what was said before it arrived."""
    async def go():
        gate = asyncio.Event()
        said: list[int] = []

        async def source():
            for i in range(5):
                if i == 3:
                    await gate.wait()
                said.append(i)
                yield {"type": "chunk", "content": str(i)}
            yield {"type": "done", "response": "01234"}

        answering: set[str] = set()
        live: dict[str, LiveTurn] = {}

        class FakeApp:
            class state:
                pass
        FakeApp.state.answering, FakeApp.state.live = answering, live

        class Req:
            app = FakeApp
        turn = LiveTurn.begin(Req(), "s1", source())
        assert "s1" in answering and live["s1"] is turn

        early = []
        async for ev in turn.follow():
            early.append(ev)
            if len(early) == 2:
                break                                 # the window went away
        await asyncio.sleep(0.05)
        assert said == [0, 1, 2] and not turn.finished.is_set(), "the turn is still going without a follower"

        gate.set()
        late = [ev async for ev in turn.follow()]      # a window that came back
        assert [e["content"] for e in late if e["type"] == "chunk"] == ["0", "1", "2", "3", "4"]
        assert late[-1]["type"] == "done"
        assert "s1" not in answering and "s1" not in live
    asyncio.run(go())


def test_a_source_that_blows_up_ends_the_turn_with_an_error_event():
    async def go():
        async def source():
            yield {"type": "chunk", "content": "a"}
            raise RuntimeError("the voice fell over")

        class FakeApp:
            class state:
                answering, live = set(), {}

        class Req:
            app = FakeApp
        turn = LiveTurn.begin(Req(), "s2", source())
        got = [ev async for ev in turn.follow()]
        assert got[-1]["type"] == "error" and "fell over" in got[-1]["detail"]
        assert not FakeApp.state.answering
    asyncio.run(go())


# ── Over HTTP, with the fake CLI ──────────────────────────────────────────

# The test client runs a whole app call before it hands back the first byte,
# so a request cannot be dropped mid-stream from here; the "follower leaves"
# half is proven on the object above. What HTTP adds: the turn is a thing
# other requests can see and join while it runs. A thread holds the stream
# open (the CLI waits at a gate), the main thread looks in from outside.

class _InThread(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn, self.result = fn, None

    def run(self):
        self.result = self.fn()


def _until(check, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_a_running_turn_can_be_stopped(make_client, tmp_path, monkeypatch):
    """17 Nov 2026: a room's round ran four seats long with no way to say
    stop. And, the same evening, the first stop in a real chat: the window
    showed the stopped answer, a reload showed neither it nor the message it
    answered, and the CLI's conversation remembered both. A stopped turn is
    KEPT as far as it got - the message, the words said, the steps taken,
    marked stopped - and the next turn carries on in the same conversation.
    That conversation saw the message; whether it kept its own cut-short
    answer is not something a killed CLI can be trusted on (the second stop,
    the same night: the chat showed a whole answer the conversation could
    not recall), so the answer is handed back to it, said to be its own."""
    gate, log = tmp_path / "let-it-finish", tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    monkeypatch.setenv("FAKE_CLAUDE_SAY_FIRST", "Running the tests now.")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    assert tc.post(f"/api/sessions/{sid}/stop", json={}).status_code == 404, "nothing to stop"

    asker = _InThread(lambda: tc.post("/api/chat/stream", json={"session": sid, "prompt": "think about it"}))
    asker.start()
    live = tc.app.state.live
    assert _until(lambda: sid in live and any(e["type"] == "thinking" for e in live[sid].events)), \
        "it has said something and used a tool, and is still at work"
    assert tc.post(f"/api/sessions/{sid}/stop", json={}).json() == {"ok": True}
    asker.join(15)
    assert not asker.is_alive(), "the stream ends when the turn is stopped"

    kinds = [e["type"] for e in _events(asker.result.text)]
    assert kinds[-2:] == ["stopped", "saved"] and "error" not in kinds and "done" not in kinds, kinds
    s = tc.get(f"/api/sessions/{sid}").json()
    assert s["answering"] is False
    asked, said = s["messages"]
    assert asked["content"] == "think about it"
    assert said["content"] == "Running the tests now." and said["meta"]["stopped"] is True
    assert [x["kind"] for x in said["meta"]["steps"]][:2] == ["said", "tool"]      # what it did, in order
    first = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    conversation = first["argv"][first["argv"].index("--session-id") + 1]
    assert s["claude"] == {"session": conversation, "seen": 1}, "it saw the message; its own answer goes back to it"

    gate.write_text("go")
    assert tc.post("/api/chat/stream", json={"session": sid, "prompt": "and now?"}).status_code == 200
    again = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert again["argv"][again["argv"].index("--resume") + 1] == conversation
    told = again["prompt"]
    assert told.startswith("Your last answer in this chat was stopped by the person before it finished.")
    assert "Running the tests now." in told and "Tools you had started before the stop" in told
    assert "think about it" not in told, "the message itself it had already seen"
    assert told.endswith("Now:\n\nand now?")
    s = tc.get(f"/api/sessions/{sid}").json()
    assert len(s["messages"]) == 4 and s["claude"]["seen"] == 4


def test_a_running_turn_is_seen_joined_and_kept_whoever_is_watching(make_client, tmp_path, monkeypatch):
    gate = tmp_path / "let-it-finish"
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]

    asker = _InThread(lambda: tc.post("/api/chat/stream", json={"session": sid, "prompt": "think about it"}))
    asker.start()
    assert _until(lambda: tc.get(f"/api/sessions/{sid}").json()["answering"]), "the chat says it is answering"
    assert any(s["id"] == sid and s["answering"] for s in tc.get("/api/sessions").json()["sessions"])
    assert tc.post("/api/chat/stream", json={"session": sid, "prompt": "and this?"}).status_code == 409, \
        "the chat being answered refuses a second message"
    other = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    assert tc.get(f"/api/sessions/{other}").json()["answering"] is False, "another chat is free"
    assert tc.get(f"/api/sessions/{sid}").json()["messages"] == [], "nothing is kept until the answer comes"

    # a second window opens the chat mid-answer and follows it
    watcher = _InThread(lambda: tc.get(f"/api/sessions/{sid}/live"))
    watcher.start()
    time.sleep(0.2)
    gate.write_text("go")
    asker.join(15); watcher.join(15)
    assert not asker.is_alive() and not watcher.is_alive()

    asked, watched = _events(asker.result.text), _events(watcher.result.text)
    kinds = [e["type"] for e in asked]
    assert kinds[0] == "tool_status" and kinds[-2:] == ["done", "saved"], kinds
    assert watched == asked, "the late window gets the whole turn, from its first event"
    assert {"type": "thinking", "content": "hmm"} in watched, "what it reasoned before the window opened is there"

    s = tc.get(f"/api/sessions/{sid}").json()
    assert [m["content"] for m in s["messages"]] == ["think about it", "Hello there"]
    assert s["messages"][1]["meta"]["steps"][-1] == {"kind": "thinking", "text": "hmm"}
    assert s["answering"] is False
    assert tc.get(f"/api/sessions/{sid}/live").status_code == 404


def test_a_lodestar_turn_is_a_live_turn_too(make_client):
    tc = make_client()
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        async def body():
            yield json.dumps({"type": "chunk", "content": "po"}).encode() + b"\n"
            await gate.wait()
            yield json.dumps({"type": "chunk", "content": "ng"}).encode() + b"\n"
            yield json.dumps({"type": "done", "response": "pong", "model": "qwen", "node": "node-a"}).encode() + b"\n"
        return httpx.Response(200, stream=_Async(body()), headers={"content-type": "application/x-ndjson"})
    tc.app.state.client = httpx.AsyncClient(base_url="http://lodestar.test", transport=httpx.MockTransport(slow))
    sid = tc.post("/api/sessions", json={"folder": "lab"}).json()["id"]

    asker = _InThread(lambda: tc.post("/api/chat/stream", json={"session": sid, "prompt": "ping"}))
    asker.start()
    assert _until(lambda: tc.get(f"/api/sessions/{sid}").json()["answering"])
    assert tc.post("/api/chat/stream", json={"session": sid, "prompt": "again"}).status_code == 409
    watcher = _InThread(lambda: tc.get(f"/api/sessions/{sid}/live"))
    watcher.start()
    time.sleep(0.2)
    tc.portal.call(gate.set)
    asker.join(10); watcher.join(10)

    assert [e["type"] for e in _events(asker.result.text)] == ["chunk", "chunk", "done", "saved"]
    assert _events(watcher.result.text) == _events(asker.result.text)
    assert [m["content"] for m in tc.get(f"/api/sessions/{sid}").json()["messages"]] == ["ping", "pong"]


class _Async(httpx.AsyncByteStream):
    def __init__(self, gen):
        self._gen = gen

    async def __aiter__(self):
        async for chunk in self._gen:
            yield chunk

    async def aclose(self) -> None:
        await self._gen.aclose()


def test_a_sessionless_stream_is_not_kept_and_not_live(make_client):
    tc = make_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"type": "done", "response": "x"}) + "\n",
                              headers={"content-type": "application/x-ndjson"})
    tc.app.state.client = httpx.AsyncClient(base_url="http://lodestar.test", transport=httpx.MockTransport(handler))
    events = _events(tc.post("/api/chat/stream", json={"prompt": "hi"}).text)
    assert [e["type"] for e in events] == ["done"]
    assert not tc.app.state.live and not tc.app.state.answering


def test_the_shell_follows_a_live_turn_and_only_locks_that_chat():
    """The page's half of the contract, read from the script: an answer
    being written is followed by chat id, a chat that is answering is the
    only one whose composer is locked, and opening a chat picks a running
    answer back up."""
    ui = Path(__file__).resolve().parents[1] / "seren_symposium" / "ui"
    script = (ui / "scripts.js").read_text(encoding="utf-8")
    assert "function followLive(sid)" in script and "/live`" in script
    assert "function stopFollowing()" in script and "AbortController" in script
    assert "ANSWERING.has(current.id)" in script, "locked per chat, not one switch for the window"
    assert "if (current.answering) followLive(id);" in script
    assert "let busy" not in script and "busy = true" not in script, "the one global lock is gone"


def test_a_chat_opened_mid_answer_says_what_it_is_answering(make_client, tmp_path, monkeypatch):
    """The session file has the question only once the answer is in; until
    then GET /api/sessions/{id} carries it as `pending`, so a window that opens
    the chat mid-answer can draw it (22 Oct 2026)."""
    gate = tmp_path / "let-it-finish"
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    assert tc.get(f"/api/sessions/{sid}").json()["pending"] is None, "nothing coming, nothing pending"

    asker = _InThread(lambda: tc.post("/api/chat/stream", json={"session": sid, "prompt": "think about it"}))
    asker.start()
    assert _until(lambda: tc.get(f"/api/sessions/{sid}").json()["answering"])
    pending = tc.get(f"/api/sessions/{sid}").json()["pending"]
    assert pending["prompt"] == "think about it" and pending["retry"] is False and pending["source"] is None
    gate.write_text("go")
    asker.join(15)
    s = tc.get(f"/api/sessions/{sid}").json()
    assert s["pending"] is None and [m["content"] for m in s["messages"]] == ["think about it", "Hello there"]

    # a retry asks no new question: it says so, and the window takes the old answer off
    gate.unlink()
    asker = _InThread(lambda: tc.post("/api/chat/stream", json={"session": sid, "retry": True}))
    asker.start()
    assert _until(lambda: tc.get(f"/api/sessions/{sid}").json()["answering"])
    pending = tc.get(f"/api/sessions/{sid}").json()["pending"]
    assert pending["retry"] is True and pending["prompt"] == "think about it"
    gate.write_text("go")
    asker.join(15)
    assert len(tc.get(f"/api/sessions/{sid}").json()["messages"]) == 2, "the retry replaced the answer"
