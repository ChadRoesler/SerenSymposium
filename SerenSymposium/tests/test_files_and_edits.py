"""Files in a chat (both ways), rewinding, forking and taking a message out -
and the Claude conversation staying honest about what it has seen - plus
screen share. Claude is tests/fake_claude.py; Lodestar is scripted."""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from seren_symposium import attachments, claude_voice, screen
from seren_symposium.config import FolderPreset, SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
STORED = re.compile(r"^[0-9a-f]{8}-")


@pytest.fixture
def runs(tmp_path, monkeypatch):
    log = tmp_path / "claude-runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return lambda: [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def config() -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.claude.command = [sys.executable, FAKE]
    cfg.folders = [FolderPreset(name="den", voice="claude"), FolderPreset(name="lab")]
    return cfg


def lines(r):
    assert r.status_code == 200, r.text
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def allowed(argv):
    """--allowedTools' values: up to the next flag."""
    out = []
    for a in argv[argv.index("--allowedTools") + 1:]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def upload(tc, sid, name, data: bytes):
    return tc.post(f"/api/sessions/{sid}/files", json={"name": name, "data": base64.b64encode(data).decode()})


@pytest.fixture
def lodestar(make_client):
    """A scripted Lodestar that remembers what it was sent."""
    def _factory(cfg=None, response="pong"):
        tc = make_client(cfg or config())
        seen: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content) if request.content else None)
            return httpx.Response(200, json={"response": response, "model": "qwen", "node": "nano"})
        tc.app.state.client = httpx.AsyncClient(base_url="http://lodestar.test",
                                                transport=httpx.MockTransport(handler))
        return tc, seen
    return _factory


# ── Files: in, kept, served back safely ───────────────────────────────────

def test_a_file_goes_in_and_comes_back(make_client, tmp_path):
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    r = upload(tc, sid, "../../My Meme!.png", PNG)
    assert r.status_code == 201
    entry = r.json()
    assert STORED.match(entry["name"]) and entry["name"].endswith("-My_Meme_.png"), "never a path"
    assert (entry["orig"], entry["type"], entry["size"]) == ("../../My Meme!.png", "image/png", len(PNG))
    assert (tmp_path / "sessions" / f"{sid}.files" / entry["name"]).read_bytes() == PNG

    got = tc.get(f"/api/sessions/{sid}/files/{entry['name']}")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-security-policy"].startswith("sandbox;")


def test_nothing_comes_back_as_a_page_that_could_drive_us(make_client):
    """Files are served from the origin that can call the API: an uploaded
    page must never run there."""
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={}).json()["id"]
    page = upload(tc, sid, "x.html", b"<script>fetch('/api/sessions')</script>").json()
    svg = upload(tc, sid, "x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>").json()
    r = tc.get(f"/api/sessions/{sid}/files/{page['name']}")
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-security-policy"].startswith("sandbox;")
    # A drawing is a picture - in an <img> its script never runs, and opened
    # on its own the sandbox stops it.
    r = tc.get(f"/api/sessions/{sid}/files/{svg['name']}")
    assert r.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in r.headers["content-security-policy"] and "default-src 'none'" in r.headers["content-security-policy"]
    exe = upload(tc, sid, "x.exe", bytes([0x4D, 0x5A, 0x90, 0x00])).json()
    r = tc.get(f"/api/sessions/{sid}/files/{exe['name']}")
    assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-disposition"] == "attachment"


def test_file_refusals(make_client):
    cfg = config()
    cfg.files.max_mb = 0.001
    tc = make_client(cfg)
    sid = tc.post("/api/sessions", json={}).json()["id"]
    assert upload(tc, "0" * 32, "a.txt", b"hi").status_code == 404
    assert tc.post(f"/api/sessions/{sid}/files", json={"name": "a", "data": "not base64!"}).status_code == 400
    assert upload(tc, sid, "big.bin", b"x" * 4096).status_code == 413
    for name in ("%2E%2E", "a.txt", "deadbeef-..%2Fx", "deadbeef-ok.txt"):
        assert tc.get(f"/api/sessions/{sid}/files/{name}").status_code == 404
    r = tc.post("/api/chat/stream", json={"session": sid, "prompt": "hi", "files": ["deadbeef-nope.png"]})
    assert r.status_code == 400 and "no file" in r.json()["error"]


# ── Files reach whoever answers ───────────────────────────────────────────

def test_claude_is_given_the_path_and_may_read_the_folder(make_client, runs):
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    meme = upload(tc, sid, "meme.png", PNG).json()
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "look", "files": [meme]}))

    (run,) = runs()
    folder = flag(run["argv"], "--add-dir")
    assert run["prompt"].startswith("look\n\nAttached to this message:\n")
    assert str(Path(folder) / meme["name"]) in run["prompt"] and "meme.png" in run["prompt"]
    asked = tc.get(f"/api/sessions/{sid}").json()["messages"][0]
    assert asked["content"] == "look", "the note is for Claude; the chat keeps what you wrote"
    assert [f["name"] for f in asked["files"]] == [meme["name"]]


def test_claude_may_write_in_the_chats_folder_and_only_there(make_client, runs):
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "draw"}))
    argv = runs()[-1]["argv"]
    tools = allowed(argv)
    rule = claude_voice.folder_rule(flag(argv, "--add-dir"))
    assert tools == [rule] and rule.startswith("Edit(//") and rule.endswith(f"{sid}.files/**)")

    cfg = config()
    cfg.claude.save_files = False
    cfg.claude.allowed_tools = ["mcp__workbench__*"]
    tc = make_client(cfg)
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "draw"}))
    argv = runs()[-1]["argv"]
    assert allowed(argv) == ["mcp__workbench__*"]


@pytest.mark.parametrize("path, rule", [
    ("C:\\Users\\alice\\chats\\ab.files", "Edit(//c/Users/alice/chats/ab.files/**)"),
    ("D:/work/project/ab.files/", "Edit(//d/work/project/ab.files/**)"),
    ("/home/alice/chats/ab.files", "Edit(//home/alice/chats/ab.files/**)"),
])
def test_the_folder_rule_is_spelled_the_way_the_cli_matches(path, rule):
    assert claude_voice.folder_rule(path) == rule


def test_a_file_alone_is_a_message(make_client, runs):
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    meme = upload(tc, sid, "meme.png", PNG).json()
    assert lines(tc.post("/api/chat/stream", json={"session": sid, "files": [meme]}))[-1]["type"] == "saved"


def test_a_local_model_gets_the_text_and_is_told_about_the_rest(lodestar):
    tc, seen = lodestar()
    sid = tc.post("/api/sessions", json={"folder": "lab"}).json()["id"]
    notes = upload(tc, sid, "notes.md", b"# plan\nship it").json()
    meme = upload(tc, sid, "meme.png", PNG).json()
    tc.post("/api/chat", json={"session": sid, "prompt": "read these", "files": [notes, meme]})
    prompt = seen[-1]["prompt"]
    assert prompt.startswith("read these\n\n--- attached: notes.md ---\n# plan\nship it\n--- end of notes.md ---")
    assert "[attached an image: meme.png - this model is not shown its contents]" in prompt


def test_a_long_text_file_is_cut_and_says_so(lodestar):
    cfg = config()
    cfg.files.inline_max_chars = 10
    tc, seen = lodestar(cfg)
    sid = tc.post("/api/sessions", json={"folder": "lab"}).json()["id"]
    log = upload(tc, sid, "big.log", b"0123456789abcdef").json()
    tc.post("/api/chat", json={"session": sid, "prompt": "?", "files": [log]})
    assert "0123456789\n--- end of big.log (cut short: too long to send whole) ---" in seen[-1]["prompt"]
    assert "abcdef" not in seen[-1]["prompt"]


# ── Images come back ──────────────────────────────────────────────────────

def test_an_image_from_the_web_is_saved_into_the_chat(make_client, runs, monkeypatch):
    asked = []

    def web(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.path == "/cat.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(200, content=b"<html>not a picture</html>", headers={"content-type": "image/png"})
    monkeypatch.setattr(attachments, "FETCH_TRANSPORT", httpx.MockTransport(web))
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "look ![a cat](https://img.example/cat.png) and "
                                            "![liar](https://img.example/fake.png)")
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "cat pls"}))

    said = tc.get(f"/api/sessions/{sid}").json()["messages"][1]
    (cat,) = said["files"]
    assert said["content"] == f"look ![a cat](files/{cat['name']}) and ![liar](https://img.example/fake.png)"
    assert (cat["orig"], cat["type"]) == ("cat.png", "image/png"), "the bytes decide, not the header"
    assert tc.get(f"/api/sessions/{sid}/files/{cat['name']}").content == PNG
    assert sorted(asked) == ["https://img.example/cat.png", "https://img.example/fake.png"]


def test_with_fetching_off_an_image_stays_a_link(make_client, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "![a cat](https://img.example/cat.png)")
    cfg = config()
    cfg.files.fetch_images = False
    tc = make_client(cfg)
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "cat"}))
    said = tc.get(f"/api/sessions/{sid}").json()["messages"][1]
    assert said["content"] == "![a cat](https://img.example/cat.png)" and "files" not in said


def test_a_file_claude_saved_into_the_chat_is_adopted(make_client, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SAVE", "drawn.png")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "made you this ![it](files/drawn.png)")
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "draw"}))
    said = tc.get(f"/api/sessions/{sid}").json()["messages"][1]
    (drawn,) = said["files"]
    assert STORED.match(drawn["name"]) and drawn["name"].endswith("-drawn.png")
    assert said["content"] == f"made you this ![it](files/{drawn['name']})"
    assert tc.get(f"/api/sessions/{sid}/files/{drawn['name']}").content.startswith(b"\x89PNG")


# ── Rewind, fork, take one out ────────────────────────────────────────────

def two_turns(tc):
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    for p in ("one", "two"):
        lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": p}))
    return sid


def test_rewinding_what_claude_saw_starts_a_new_conversation(make_client, runs):
    """The CLI cannot unsay anything. Cut into what it saw, and the next turn
    is a new conversation that is handed the chat as it now stands."""
    tc = make_client(config())
    sid = two_turns(tc)
    first = tc.get(f"/api/sessions/{sid}").json()["claude"]["session"]

    s = tc.post(f"/api/sessions/{sid}/rewind", json={"to": 2}).json()
    assert [m["content"] for m in s["messages"]] == ["one", "Hello there"] and "claude" not in s

    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "three"}))
    run = runs()[-1]
    assert "--resume" not in run["argv"] and flag(run["argv"], "--session-id") != first
    assert run["prompt"] == ("Earlier in this chat, before you were answering in it:\n\n"
                             "[user] one\n[assistant] Hello there\n\nNow:\n\nthree")


def test_rewinding_only_what_claude_never_saw_keeps_its_conversation(lodestar, runs):
    tc, _ = lodestar()
    sid = two_turns(tc)
    tc.patch(f"/api/sessions/{sid}", json={"settings": {"voice": "lodestar"}})
    tc.post("/api/chat", json={"session": sid, "prompt": "local"})
    s = tc.post(f"/api/sessions/{sid}/rewind", json={"to": 4}).json()
    assert s["claude"]["seen"] == 4, "nothing it saw changed"


def test_retry_in_a_claude_chat_does_not_answer_with_the_old_answer_in_mind(make_client, runs):
    tc = make_client(config())
    sid = two_turns(tc)
    lines(tc.post("/api/chat/stream", json={"session": sid, "retry": True}))
    run = runs()[-1]
    assert "--resume" not in run["argv"]
    assert run["prompt"].endswith("[user] one\n[assistant] Hello there\n\nNow:\n\ntwo")
    s = tc.get(f"/api/sessions/{sid}").json()
    assert len(s["messages"]) == 4 and s["claude"]["seen"] == 4


def test_a_message_can_be_taken_out(make_client, runs):
    tc = make_client(config())
    sid = two_turns(tc)
    s = tc.delete(f"/api/sessions/{sid}/messages/1").json()
    assert [m["content"] for m in s["messages"]] == ["one", "two", "Hello there"] and "claude" not in s
    assert tc.delete(f"/api/sessions/{sid}/messages/9").status_code == 400


def test_a_fork_is_a_new_chat_with_the_thread_and_its_files(make_client, runs):
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den", "title": "memes"}).json()["id"]
    meme = upload(tc, sid, "meme.png", PNG).json()
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "look", "files": [meme]}))
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "more"}))

    r = tc.post(f"/api/sessions/{sid}/fork", json={"at": 2})
    assert r.status_code == 201
    fork = r.json()
    assert (fork["title"], fork["folder"]) == ("memes (fork)", "den") and "claude" not in fork
    assert [m["content"] for m in fork["messages"]] == ["look", "Hello there"]
    assert tc.get(f"/api/sessions/{fork['id']}/files/{meme['name']}").content == PNG
    assert len(tc.get(f"/api/sessions/{sid}").json()["messages"]) == 4, "the original is untouched"
    assert tc.post(f"/api/sessions/{sid}/fork", json={"at": 0}).status_code == 400


def test_nothing_moves_under_an_answer(make_client):
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={}).json()["id"]
    tc.app.state.answering.add(sid)
    assert tc.post(f"/api/sessions/{sid}/rewind", json={"to": 0}).status_code == 409
    assert tc.delete(f"/api/sessions/{sid}").status_code == 409


def test_deleting_a_chat_takes_its_files(make_client, tmp_path):
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={}).json()["id"]
    upload(tc, sid, "a.txt", b"hi")
    tc.delete(f"/api/sessions/{sid}")
    assert not (tmp_path / "sessions" / f"{sid}.files").exists()


# ── Screen share ──────────────────────────────────────────────────────────

def test_a_screen_frame_rides_with_a_message(make_client, runs, monkeypatch):
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    monkeypatch.setattr(screen, "capture", lambda monitor, w, q, window=None: screen.Frame(jpeg, 1600, 900, monitor))
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    r = tc.post("/api/chat/stream", json={"session": sid, "prompt": "see?", "screen": True})
    assert r.status_code == 409, "no frame taken yet"

    info = tc.post("/api/screen", json={"monitor": 2}).json()
    assert (info["width"], info["monitor"]) == (1600, 2)
    assert tc.get("/api/screen/latest").content == jpeg
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "see?", "screen": True}))
    (shot,) = tc.get(f"/api/sessions/{sid}").json()["messages"][0]["files"]
    assert shot["orig"].startswith("screen-") and shot["type"] == "image/jpeg"
    assert shot["name"] in runs()[-1]["prompt"]
    # marked, so the shell keeps it out of the conversation and out of Files
    assert shot["screen"] is True and shot["source"] == "display 2"
    assert "a screen share of display 2" in runs()[-1]["prompt"]


def test_a_window_can_be_shared_instead_of_a_display(make_client, monkeypatch):
    seen = {}

    def grab(monitor, w, q, window=None):
        seen["window"] = window
        return screen.Frame(b"\xff\xd8\xff", 800, 600, 0, "notes.txt - Notepad")
    monkeypatch.setattr(screen, "capture", grab)
    tc = make_client(config())
    info = tc.post("/api/screen", json={"window": 4242}).json()
    assert seen["window"] == 4242 and info["window"] == "notes.txt - Notepad"
    assert tc.post("/api/screen", json={"window": "4242"}).status_code == 400
    assert tc.post("/api/screen", json={"window": True}).status_code == 400


def test_the_sources_name_the_main_display(make_client, monkeypatch):
    monkeypatch.setattr(screen, "monitors", lambda: [{"index": 1, "width": 1080, "height": 1920, "primary": False},
                                                     {"index": 2, "width": 1920, "height": 1080, "primary": True}])
    monkeypatch.setattr(screen, "windows", lambda: [{"id": 7, "title": "a window"}])
    tc = make_client(config())
    got = tc.get("/api/screen/sources").json()
    assert got["primary"] == 2 and got["windows"] == [{"id": 7, "title": "a window"}]
    cfg = config()
    cfg.screen.enabled = False
    assert make_client(cfg).get("/api/screen/sources").json() == {"monitors": [], "windows": [], "primary": 1}


def test_a_screen_frame_sent_again_stays_one(make_client, runs, monkeypatch):
    """An edit puts a message's files back and sends them again: the frame
    must come back marked, or it would land in the conversation."""
    monkeypatch.setattr(screen, "capture", lambda monitor, w, q, window=None: screen.Frame(b"\xff\xd8\xff", 10, 10, 1))
    tc = make_client(config())
    sid = tc.post("/api/sessions", json={"folder": "den"}).json()["id"]
    tc.post("/api/screen", json={})
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "see?", "screen": True}))
    (shot,) = tc.get(f"/api/sessions/{sid}").json()["messages"][0]["files"]
    tc.post(f"/api/sessions/{sid}/rewind", json={"to": 0})
    lines(tc.post("/api/chat/stream", json={"session": sid, "prompt": "again", "files": [shot]}))
    (again,) = tc.get(f"/api/sessions/{sid}").json()["messages"][0]["files"]
    assert again["screen"] is True and again["source"] == "display 1"


def test_screen_share_says_why_when_it_cannot(make_client, monkeypatch):
    def broken(*_):
        raise RuntimeError("screen share needs mss and Pillow")
    monkeypatch.setattr(screen, "capture", broken)
    tc = make_client(config())
    r = tc.post("/api/screen", json={})
    assert r.status_code == 409 and "mss" in r.json()["error"]
    cfg = config()
    cfg.screen.enabled = False
    assert make_client(cfg).post("/api/screen", json={}).status_code == 409


def test_the_shell_is_told_what_it_can_offer(make_client):
    cfg = config()
    cfg.ui.you = "alice"
    c = make_client(cfg).get("/api/config").json()
    assert c["you"] == "alice"
    assert c["files"] == {"max_mb": 25.0, "fetch_images": True}
    assert set(c["screen"]) == {"enabled", "available", "monitors"}


def test_the_renderer_is_shipped_not_fetched(client):
    for f in ("marked.min.js", "purify.min.js", "highlight.min.js", "github-dark.min.css"):
        assert client.get(f"/vendor/{f}").status_code == 200
