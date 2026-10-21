"""
Say it: an answer said aloud in the assistant's voice THROUGH Lodestar's speaker,
the clip kept on that answer.

Lodestar is a MockTransport. Pinned:

- /api/say {session, index} sends the answer's words to /api/v1/speak in
  Symposium's name (the bearer attached), and the WAV that comes back is a
  file on that answer, marked `said`, with the voice and how long
- served back, it plays; it rides with the message like any file and
  whoever answers is told what it is
- what is said is the answer minus thinking, code fences and markdown
  marks; a long answer is said up to max_chars, cut at a sentence, and
  says so
- a question (a user message), a message that is not there, a chat that
  is not there: 400 / 404; speech off: 409
- a Lodestar that cannot say it answers with the why and keeps nothing
- the bearer never reaches /api/config
"""
from __future__ import annotations

import re

import httpx

from seren_symposium.app import spoken_text
from seren_symposium.config import SymposiumConfig

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 20 + b"data\0\0\0\0"


def lodestar(tc, status=200):
    """A Lodestar whose /api/v1/speak is scripted; returns what it was sent."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": request.read(), "auth": request.headers.get("authorization", "")})
        if status == 503:
            return httpx.Response(503, json={"error": "no online node with speaker", "node": None})
        if status == 401:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav", "X-Seren-Node": "bigbox",
                                                         "X-Seren-Voice": "wren", "X-Seren-Backend": "qwen3tts",
                                                         "X-Seren-Seconds": "4.1", "X-Seren-Clip-Seconds": "4.9"})
    tc.app.state.client = httpx.AsyncClient(base_url="http://127.0.0.1:6361", transport=httpx.MockTransport(handle),
                                            headers={"Authorization": "Bearer sym-token"})
    return seen


def config(**speech_kw) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.lodestar.bearer_token = "sym-token"
    for k, v in speech_kw.items():
        setattr(cfg.speech, k, v)
    return cfg


def chat_with_an_answer(tc, tmp_path, answer="Hi. **It's me.** Here is `code`.\n\n```py\nprint(1)\n```\n\nDone."):
    from seren_symposium.sessions import SessionStore
    sid = tc.post("/api/sessions", json={"folder": ""}).json()["id"]
    SessionStore(tmp_path / "sessions").record(sid, "hello?", answer, {})
    return sid


def test_say_sends_the_answer_and_the_clip_sits_on_it(make_client, tmp_path):
    tc = make_client(config())
    seen = lodestar(tc)
    sid = chat_with_an_answer(tc, tmp_path)

    assert tc.get("/api/config").json()["speech"] == {"enabled": True, "voice": "", "max_chars": 4000}
    assert "sym-token" not in tc.get("/api/config").text

    out = tc.post("/api/say", json={"session": sid, "index": 1}).json()
    assert out["cut"] is False and out["node"] == "bigbox" and out["voice"] == "wren" and out["seconds"] == 4.9
    f = out["file"]
    assert re.fullmatch(r"said-\d{8}-\d{6}\.wav", f["orig"]) and f["type"] == "audio/wav"
    assert f["said"] is True and f["voice"] == "wren" and f["seconds"] == 4.9

    (sent,) = seen
    assert sent["path"] == "/api/v1/speak" and sent["auth"] == "Bearer sym-token"
    import json
    assert json.loads(sent["body"]) == {"text": "Hi. It's me. Here is code.\nDone.", "voice": "wren", "holder": "symposium"}

    # on the answer, in the chat, served back as something that plays
    s = tc.get(f"/api/sessions/{sid}").json()
    assert s["messages"][1]["files"] == [f] and "files" not in s["messages"][0]
    got = tc.get(f"/api/sessions/{sid}/files/{f['name']}")
    assert got.status_code == 200 and got.content == WAV and got.headers["content-type"].startswith("audio/")
    assert "content-disposition" not in got.headers


def test_the_clip_rides_with_the_message_and_the_voice_is_told_what_it_is(make_client, tmp_path):
    from seren_symposium import attachments
    from seren_symposium.app import _named_files
    from seren_symposium.sessions import SessionStore
    tc = make_client(config())
    lodestar(tc)
    sid = chat_with_an_answer(tc, tmp_path)
    f = tc.post("/api/say", json={"session": sid, "index": 1}).json()["file"]
    store = SessionStore(tmp_path / "sessions")
    (entry,) = _named_files(store, sid, [f])
    assert entry["said"] is True and entry["voice"] == "wren" and entry["seconds"] == 4.9
    assert "this answer said aloud in wren voice, 5s" in attachments.for_claude([entry], tmp_path)
    assert "[attached the answer said aloud:" in attachments.for_local([entry], store, sid, 1000)


def test_what_is_said_is_the_answer_without_the_parts_for_the_eye():
    text, cut = spoken_text("<think>hmm</think>\n# Title\n\n- one\n- two\n\n> quoted\n\nSee [the docs](http://x) and ![pic](a.png). *soft* __loud__ ~ok~", 4000)
    assert text == "Title\none\ntwo\nquoted\nSee the docs and . soft loud ~ok~" and cut is False
    long = "This is a sentence. " * 300
    text, cut = spoken_text(long, 1000)
    assert cut is True and len(text) <= 1000 and text.endswith("sentence.")
    assert spoken_text("```\nonly code\n```", 4000) == ("", False)


def test_a_long_answer_is_said_up_to_max_chars_and_says_so(make_client, tmp_path):
    tc = make_client(config(max_chars=120))
    seen = lodestar(tc)
    sid = chat_with_an_answer(tc, tmp_path, answer="One sentence here. " * 20)
    out = tc.post("/api/say", json={"session": sid, "index": 1}).json()
    assert out["cut"] is True and out["file"]["said"] is True
    import json
    said = json.loads(seen[0]["body"])["text"]
    assert len(said) <= 120 and said.endswith("here.")


def test_only_an_answer_that_is_there_is_said(make_client, tmp_path):
    tc = make_client(config())
    seen = lodestar(tc)
    sid = chat_with_an_answer(tc, tmp_path)
    assert tc.post("/api/say", json={"session": sid, "index": 0}).status_code == 400, "a question is not said"
    assert tc.post("/api/say", json={"session": sid, "index": 7}).status_code == 404
    assert tc.post("/api/say", json={"session": sid, "index": True}).status_code == 404
    assert tc.post("/api/say", json={"session": "nope", "index": 1}).status_code == 404
    assert tc.post("/api/say", json={}).status_code == 404
    assert not seen
    tc2 = make_client(config(enabled=False))
    lodestar(tc2)
    sid2 = chat_with_an_answer(tc2, tmp_path)
    r = tc2.post("/api/say", json={"session": sid2, "index": 1})
    assert r.status_code == 409 and "speech.enabled" in r.json()["error"]
    assert "speech" in tc2.get("/api/config").json() and tc2.get("/api/config").json()["speech"]["enabled"] is False


def test_a_lodestar_that_cannot_say_it_says_why_and_keeps_nothing(make_client, tmp_path):
    tc = make_client(config())
    lodestar(tc, status=503)
    sid = chat_with_an_answer(tc, tmp_path)
    r = tc.post("/api/say", json={"session": sid, "index": 1})
    assert r.status_code == 503 and r.json()["error"] == "no online node with speaker"
    assert "files" not in tc.get(f"/api/sessions/{sid}").json()["messages"][1]
    assert not list((tmp_path / "sessions").glob("*.files/*")), "nothing kept"
    lodestar(tc, status=401)
    r = tc.post("/api/say", json={"session": sid, "index": 1})
    assert r.status_code == 401 and "bearer" in r.json()["error"], "a 401 is explained the way the chat explains it"
