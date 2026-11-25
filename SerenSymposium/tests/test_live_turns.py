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
    assert {k: v for k, v in s["claude"].items() if k != "marks"} == {"session": conversation, "seen": 1}, "it saw the message; its own answer goes back to it"

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
    assert "if (current.answering && !SENDING.has(id) && FOLLOWER?.sid !== id) followLive(id);" in script
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


def test_what_you_attach_while_an_answer_is_coming_is_yours_not_the_answers(make_client, tmp_path, monkeypatch):
    """A message sent mid-answer waits its turn, and its files are uploaded at
    once so the slip holds what the chat already has. The answer under way
    takes the files that turned up in the chat's folder during its turn for
    ones it saved - and took yours. 18 Nov 2026, the first tangle on 5.7.0:
    two screenshots Chad pasted mid-round were kept on the seat's answer -
    "an interruption with an image gives it to the next answerer"."""
    import base64
    from fake_claude import PNG
    gate = tmp_path / "let-it-finish"
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    monkeypatch.setenv("FAKE_CLAUDE_SAVE", "drawn.png")            # and the answer saves one of its own
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    asker = _InThread(lambda: tc.post("/api/chat/stream", json={"session": sid, "prompt": "think about it"}))
    asker.start()
    assert _until(lambda: tc.get(f"/api/sessions/{sid}").json()["answering"])
    time.sleep(0.3)                                                  # the turn has taken its look at the folder
    r = tc.post(f"/api/sessions/{sid}/files", json={"name": "my-screenshot.png", "data": base64.b64encode(PNG).decode()})
    assert r.status_code == 201
    mine = r.json()
    gate.write_text("go")
    asker.join(15)
    assert not asker.is_alive()

    asked, said = tc.get(f"/api/sessions/{sid}").json()["messages"]
    assert [f["orig"] for f in said["files"]] == ["drawn.png"], "its own picture, and not yours"
    # yours is still there, and goes with the message it was waiting on
    lines = _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "look at this", "files": [mine]}).text)
    assert lines[-1]["type"] == "saved"
    later = tc.get(f"/api/sessions/{sid}").json()["messages"][2]
    assert later["content"] == "look at this" and [f["name"] for f in later["files"]] == [mine["name"]]


# ── A task set aside: the answer comes, the task goes on, and it comes back to say so ──

def _said(tc, sid):
    return [(m["role"], m.get("seat"), m["content"]) for m in tc.get(f"/api/sessions/{sid}").json()["messages"]]


def test_a_task_left_running_outlives_the_answer_and_comes_back_to_say_so(make_client, tmp_path, monkeypatch):
    """Chad, 19 Nov 2026, after seats froze a room waiting on firmware builds:
    "a task is kicked off, and set to the side, similar to what is done in the
    harness, it runs in the background, and then alerts when done. because a
    frozen seat not being able to answer a question would suck". The answer
    is kept and the chat is free; the process is set aside with its task; the
    next message goes to THAT process, not to a second one beside it; and
    when the task ends, what it says is kept as its own, marked as such."""
    from seren_symposium import claude_voice
    gate, log = tmp_path / "the-build-is-done", tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND", "30")
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND_GATE", str(gate))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]

    events = _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "build the firmware"}).text)
    assert {"type": "background", "tasks": ["Build the firmware"]} in events and events[-1]["type"] == "saved"
    s = tc.get(f"/api/sessions/{sid}").json()
    assert s["answering"] is False, "the chat is not held by what is running behind it"
    conversation = s["claude"]["session"]
    assert [(b["seat"], b["conversation"], b["tasks"]) for b in s["background"]] == [(None, conversation, ["Build the firmware"])]
    assert _said(tc, sid) == [("user", None, "build the firmware"), ("assistant", None, "Hello there")]

    # asked something while it runs: the same process answers, with the task still going
    events = _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "how long will it take?"}).text)
    assert events[-1]["type"] == "saved"
    assert _said(tc, sid)[-1] == ("assistant", None, "meanwhile: how long will it take?")
    runs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert len(runs) == 2 and runs[1].get("meanwhile") is True, "one process, handed the second message"
    assert "--input-format" in runs[0]["argv"] and runs[0]["prompt"] == "build the firmware"
    assert tc.get(f"/api/sessions/{sid}").json()["background"], "still set aside, still running"

    # the task ends: it comes back on its own
    gate.write_text("go")
    assert _until(lambda: len(_said(tc, sid)) == 5), "what it said when the build ended is in the chat"
    s = tc.get(f"/api/sessions/{sid}").json()
    woke = s["messages"][-1]
    assert (woke["role"], woke["content"]) == ("assistant", "The build finished: exit 0.")
    assert woke["meta"]["woke"] == ['Background command "Build the firmware" completed (exit code 0)']
    assert {k: v for k, v in s["claude"].items() if k != "marks"} == {"session": conversation, "seen": 5}, "it has seen its own words, and all before them"
    assert _until(lambda: not tc.get(f"/api/sessions/{sid}").json()["background"]), "nothing left running"
    assert _until(lambda: conversation not in claude_voice.PARKED)


def test_a_restart_lets_the_task_run_on_and_the_next_start_tells_the_chat(make_client, tmp_path, monkeypatch):
    """Chad, 23 Nov 2026: "what happens if symposium crashes or goes down,
    while a long running background task is ongoing?" Symposium stops with a
    build running: the CLI is let go, not killed, and written down; the next
    Symposium shows it as running from before the restart, and when it ends,
    what it said - in its transcript, nobody reading - is put in the chat."""
    from seren_symposium import behind, claude_voice
    gate, projects = tmp_path / "the-build-is-done", tmp_path / "projects"
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND", "60")
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND_GATE", str(gate))
    monkeypatch.setenv("FAKE_CLAUDE_PROJECTS", str(projects))
    monkeypatch.setattr(behind, "PROCESS_NAMES", ("claude", "python"))     # the fake CLI is a python
    monkeypatch.setattr(behind, "PICKUP_EVERY", 0.2)
    cfg = config()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.projects_dir = str(projects)
    tc = make_client(cfg)
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "build the firmware"}).text)
    conversation = tc.get(f"/api/sessions/{sid}").json()["claude"]["session"]
    ledger = behind.Ledger(tmp_path / behind.FILE_NAME)
    entry = ledger.entries()[conversation]
    assert entry["chat"] == sid and entry["tasks"][0]["description"] == "Build the firmware", "written down"

    tc.__exit__(None, None, None)                                   # Symposium stops: an upgrade, say
    assert behind.pid_alive(entry["pid"]), "let go, not killed"
    assert ledger.entries()[conversation].get("let_go"), "and stamped for the next start"

    tc2 = make_client(cfg)                                          # the next Symposium
    running = tc2.get(f"/api/sessions/{sid}").json()["background"]
    assert running and running[0]["let_go"] and running[0]["tasks"] == ["Build the firmware"]

    gate.write_text("go")                                           # the build ends, nobody reading
    assert _until(lambda: _said(tc2, sid)[-1] == ("assistant", None, "The build finished: exit 0."), seconds=20)
    woke = tc2.get(f"/api/sessions/{sid}").json()["messages"][-1]
    assert woke["meta"]["picked_up"] is True and "while Symposium was away" in woke["meta"]["woke"][0]
    assert _until(lambda: not tc2.get(f"/api/sessions/{sid}").json()["background"])
    assert ledger.entries() == {} and conversation not in claude_voice.LET_GO


def test_a_task_from_before_a_rewind_still_reports_and_says_so(make_client, tmp_path, monkeypatch):
    """The chat is rewound past where a build was started: the voice talks in
    a branch now, but the build is real and still going. It stays on the chip,
    and when it ends its word comes in marked as from before the rewind -
    not as if the branch had said it (25 Nov 2026)."""
    gate = tmp_path / "the-build-is-done"
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND", "30")
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND_GATE", str(gate))
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    monkeypatch.delenv("FAKE_CLAUDE_BACKGROUND")
    _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "hello"}).text)
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND", "30")
    _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "build the firmware"}).text)
    assert tc.get(f"/api/sessions/{sid}").json()["background"]

    tc.post(f"/api/sessions/{sid}/rewind", json={"to": 2})            # back to before the build was asked for
    running = tc.get(f"/api/sessions/{sid}").json()["background"]
    assert running and running[0].get("rewound") is True, "still shown, said to be from before"

    gate.write_text("go")
    assert _until(lambda: len(_said(tc, sid)) == 3, seconds=15)
    woke = tc.get(f"/api/sessions/{sid}").json()["messages"][-1]
    assert woke["meta"].get("rewound") is True and woke["meta"]["woke"][0].startswith("from before a rewind")
    assert _until(lambda: not tc.get(f"/api/sessions/{sid}").json()["background"])


def test_a_cli_that_died_before_it_could_report_is_asked(make_client, tmp_path, monkeypatch):
    """The box went down with the build: the process is gone and never said
    anything. The next start resumes that conversation with a note marked as
    Symposium's (never the person's), and what it answers goes in the chat."""
    from seren_symposium import behind
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setattr(behind, "PICKUP_EVERY", 0.2)
    cfg = config()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.projects_dir = str(tmp_path / "projects")
    tc = make_client(cfg)
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "build the firmware"}).text)
    conversation = tc.get(f"/api/sessions/{sid}").json()["claude"]["session"]
    tc.__exit__(None, None, None)
    behind.Ledger(tmp_path / behind.FILE_NAME).put(conversation, {
        "chat": sid, "seat": None, "conversation": conversation, "pid": 999999, "started": time.time() - 100,
        "let_go": time.time() - 50, "tasks": [{"id": "bg1", "type": "local_bash", "description": "Build the firmware"}]})

    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "It got to 30/42, then the box went down.")
    tc2 = make_client(cfg)
    assert _until(lambda: _said(tc2, sid)[-1][2] == "It got to 30/42, then the box went down.", seconds=20)
    assert len(_said(tc2, sid)) == 3, "its answer, and no message of the person's for the note"
    asked = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert flag(asked["argv"], "--resume") == conversation
    assert "[Symposium, not the person]" in asked["prompt"] and "Build the firmware" in asked["prompt"]
    assert behind.Ledger(tmp_path / behind.FILE_NAME).entries() == {}


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def test_a_seat_with_a_task_running_does_not_hold_the_round(make_client, tmp_path, monkeypatch):
    """The room: the seat who started the build has answered, so the next
    seat is asked; neither waits on the build. When it ends, each says so
    under her own name - and has NOT thereby heard what the other said
    meanwhile, which she is still handed next round."""
    gate, log = tmp_path / "the-build-is-done", tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND", "30")
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND_GATE", str(gate))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    cfg = config()
    cfg.ui.you = "Chad"
    tc = make_client(cfg)
    sid = tc.post("/api/sessions", json={"settings": {"voice": "claude"}}).json()["id"]
    for name in ("soma", "psycheion"):
        assert tc.post(f"/api/sessions/{sid}/seats", json={"name": name, "cwd": str(tmp_path)}).status_code == 201
    started = time.time()
    events = _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "flash the board"}).text)
    assert time.time() - started < 15, "the round did not wait thirty seconds for anyone's build"
    assert [e["seat"] for e in events if e["type"] == "seat"] == ["soma", "psycheion"]
    assert _said(tc, sid) == [("user", None, "flash the board"), ("assistant", "soma", "Hello there"),
                              ("assistant", "psycheion", "Hello there")]
    s = tc.get(f"/api/sessions/{sid}").json()
    assert sorted(b["seat"] for b in s["background"]) == ["psycheion", "soma"] and s["answering"] is False

    gate.write_text("go")
    assert _until(lambda: len(_said(tc, sid)) == 5)
    late = tc.get(f"/api/sessions/{sid}").json()
    assert sorted((m["seat"], m["content"]) for m in late["messages"][3:]) == [
        ("psycheion", "The build finished: exit 0."), ("soma", "The build finished: exit 0.")]
    assert all(m["meta"]["woke"] for m in late["messages"][3:])
    soma = next(x for x in late["seats"] if x["name"] == "soma")
    assert soma["claude"]["seen"] == 2, "she spoke before psycheion did: what was said since is still hers to hear"


def test_what_is_running_behind_a_chat_can_be_stopped_and_some_turns_never_leave_anything(make_client, tmp_path, monkeypatch):
    from seren_symposium import claude_voice
    monkeypatch.setenv("FAKE_CLAUDE_BACKGROUND", "30")
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    _events(tc.post("/api/chat/stream", json={"session": sid, "prompt": "build the firmware"}).text)
    conversation = tc.get(f"/api/sessions/{sid}").json()["claude"]["session"]
    held = claude_voice.PARKED[conversation]
    r = tc.post(f"/api/sessions/{sid}/background/stop", json={}).json()
    assert r == {"ok": True, "stopped": [None], "background": []}
    assert _until(lambda: not held.alive()), "the process, and its task with it, is ended"
    time.sleep(0.4)
    assert len(_said(tc, sid)) == 2, "and nobody is woken to report a task that was stopped"
    assert tc.post("/api/sessions/nope/background/stop", json={}).status_code == 404

    # a one-shot (the picture panel's translate) is one answer and nothing after it
    before = set(claude_voice.PARKED)
    out = asyncio.run(claude_voice.one_shot(config().claude, prompt="a bird"))
    assert out["ok"] and set(claude_voice.PARKED) == before

    # and with claude.background off it is the old way: the prompt, then the end of the process
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    cfg = config()
    cfg.claude.background = False
    cfg.sessions.dir = str(tmp_path / "old-way")
    tc2 = make_client(cfg)
    sid2 = tc2.post("/api/sessions", json={"folder": "den"}).json()["id"]
    _events(tc2.post("/api/chat/stream", json={"session": sid2, "prompt": "build the firmware"}).text)
    run = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert "--input-format" not in run["argv"] and run["prompt"] == "build the firmware"
    assert tc2.get(f"/api/sessions/{sid2}").json()["background"] == []
