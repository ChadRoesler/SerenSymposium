"""Avatars: the picture a voice wears in the window, set by whoever wears it
or by the person, from any picture (seren_symposium.avatars). Not the
kitchen's magnets: nothing is looked up, it is put on."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

from seren_symposium import attachments, avatars
from seren_symposium.config import SymposiumConfig

from fake_claude import PNG

FAKE = str(Path(__file__).with_name("fake_claude.py"))
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3"/></svg>'


@pytest.fixture
def client(make_client, tmp_path):
    cfg = SymposiumConfig()
    cfg.claude.command = [sys.executable, FAKE]
    cfg.claude.projects_dir = str(tmp_path / "projects")
    cfg.ui.you = "Chad"
    return make_client(cfg)


def stream(client, body):
    r = client.post("/api/chat/stream", json=body)
    assert r.status_code == 200, r.text
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def upload(client, sid, name, data):
    r = client.post(f"/api/sessions/{sid}/files", json={"name": name, "data": base64.b64encode(data).decode()})
    assert r.status_code == 201, r.text
    return r.json()["name"]


def a_chat(client):
    return client.post("/api/sessions", json={"settings": {"voice": "claude"}}).json()["id"]


# ── The rule, alone ───────────────────────────────────────────────────────

def test_an_answer_names_its_avatar_by_caption():
    files = [{"name": "ab12-a-wren.png", "orig": "a-wren.png"}]
    assert avatars.wanted("look ![avatar](files/ab12-a-wren.png)", []) == ("files/ab12-a-wren.png", "ab12-a-wren.png", False)
    assert avatars.wanted("![My Avatar](files/x.svg)", []) == ("files/x.svg", "x.svg", False)
    # for this chat unless it says everywhere
    assert avatars.wanted("![avatar everywhere](files/x.svg)", []) == ("files/x.svg", "x.svg", True)
    assert avatars.wanted("![My Avatar Everywhere](files/x.svg)", [])[2] is True
    # a picture a tool drew this turn, by the file name (or the whole path) the tool gave
    assert avatars.wanted("![avatar](a-wren.png)", files) == ("a-wren.png", "ab12-a-wren.png", False)
    assert avatars.wanted("![avatar](/mnt/x/gallery/a-wren.png)", files)[1] == "ab12-a-wren.png"
    # the last one wins; a picture that is only a picture is not one
    assert avatars.wanted("![avatar](files/one.png) no: ![avatar](files/two.png)", [])[1] == "two.png"
    assert avatars.wanted("![a wren](files/x.png)", []) is None
    assert avatars.wanted("![avatar](nowhere.png)", files) is None


# ── Through the app ───────────────────────────────────────────────────────

def test_the_person_puts_a_picture_on_a_name_and_takes_it_off(client):
    sid = a_chat(client)
    assert client.get("/api/avatars").json() == {"avatars": {}}
    assert client.get("/api/avatars/kitchen").status_code == 404

    worn = client.post("/api/avatars/kitchen", json={"session": sid, "file": upload(client, sid, "face.png", PNG)}).json()
    assert set(worn["avatars"]) == {"kitchen"} and worn["avatars"]["kitchen"]["type"] == "image/png"
    got = client.get("/api/avatars/kitchen")
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"

    # a drawing in SVG, and one for everybody who has not picked
    client.post("/api/avatars/default", json={"session": sid, "file": upload(client, sid, "possum.svg", SVG)})
    assert client.get("/api/avatars/default").headers["content-type"].startswith("image/svg+xml")
    first = client.get("/api/avatars").json()["avatars"]["kitchen"]["at"]
    client.post("/api/avatars/kitchen", json={"session": sid, "file": upload(client, sid, "possum.svg", SVG)})
    assert client.get("/api/avatars").json()["avatars"]["kitchen"]["at"] >= first       # a new face, not a cached one
    assert client.get("/api/avatars/kitchen").content == SVG

    assert set(client.delete("/api/avatars/kitchen").json()["avatars"]) == {"default"}
    assert client.delete("/api/avatars/kitchen").status_code == 404


def test_what_is_not_a_picture_is_not_worn(client):
    sid = a_chat(client)
    notes = upload(client, sid, "notes.txt", b"not a picture")
    assert client.post("/api/avatars/kitchen", json={"session": sid, "file": notes}).status_code == 400
    assert client.post("/api/avatars/kitchen", json={"session": sid, "file": "nope.png"}).status_code == 400
    assert client.post("/api/avatars/no spaces", json={"session": sid, "file": notes}).status_code == 400
    assert client.post("/api/avatars/kitchen", json={"session": "0" * 32, "file": notes}).status_code == 404
    assert client.post("/api/avatars/kitchen", json={"file": notes}).status_code == 400
    assert client.get("/api/avatars").json() == {"avatars": {}}


def test_a_link_is_fetched_into_the_chat_and_worn(client, monkeypatch):
    def web(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG) if request.url.path.endswith("robot.png") else httpx.Response(404)
    monkeypatch.setattr(attachments, "FETCH_TRANSPORT", httpx.MockTransport(web))
    sid = a_chat(client)
    worn = client.post("/api/avatars/lodestar", json={"session": sid, "url": "https://example.org/robot.png"})
    assert worn.status_code == 200 and "lodestar" in worn.json()["avatars"]
    assert client.get("/api/avatars/lodestar").content == PNG
    bad = client.post("/api/avatars/lodestar", json={"session": sid, "url": "https://example.org/a-page.html"})
    assert bad.status_code == 400 and "did not give a picture" in bad.json()["error"]


def test_a_seat_puts_on_its_own_and_a_lone_claude_the_default(client, tmp_path, monkeypatch):
    """Chad, 17 Nov 2026: "if you wanted to have one thats of you, or of a
    robot, or of like a really cool space ship, ridden by a possum" - the one
    wearing it picks, by showing a picture captioned avatar."""
    monkeypatch.setenv("FAKE_CLAUDE_SAVE", "possum.png")
    monkeypatch.setenv("FAKE_CLAUDE_SEATS", json.dumps({"kitchen": "my new face: ![avatar everywhere](files/possum.png)",
                                                       "lodestar": "nothing to wear"}))
    for name in ("SerenKitchen", "SerenLodestar"):
        (tmp_path / name).mkdir()
    sid = a_chat(client)
    for name in ("kitchen", "lodestar"):
        client.post(f"/api/sessions/{sid}/seats", json={"name": name, "cwd": str(tmp_path / f"Seren{name.title()}")})
    events = stream(client, {"session": sid, "prompt": "pick a face"})
    assert [(e["name"], e["scope"], e["seat"]) for e in events if e["type"] == "avatar"] == [("kitchen", "everywhere", "kitchen")]
    assert set(client.get("/api/avatars").json()["avatars"]) == {"kitchen"}
    assert client.get("/api/avatars/kitchen").content == PNG

    lone = a_chat(client)
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "![avatar everywhere](files/possum.png)")
    events = stream(client, {"session": lone, "prompt": "and you?"})
    assert [e["name"] for e in events if e["type"] == "avatar"] == ["default"]
    assert set(client.get("/api/avatars").json()["avatars"]) == {"kitchen", "default"}


def test_a_chat_can_have_faces_of_its_own(client, tmp_path, monkeypatch):
    """Chad, 17 Nov 2026: "the per chat avatars would be good as well, so that
    if you decide you want a specific avatar for cheese tag, or whatever, you
    have one". An avatar put on in a chat is that chat's; what is worn
    everywhere is untouched and is what every other chat still sees."""
    monkeypatch.setenv("FAKE_CLAUDE_SAVE", "cheese-thief.png")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "for cheese tag only: ![avatar](files/cheese-thief.png)")
    sid = a_chat(client)
    events = stream(client, {"session": sid, "prompt": "tag, you're it"})
    assert [(e["name"], e["scope"]) for e in events if e["type"] == "avatar"] == [("default", "chat")]
    s = client.get(f"/api/sessions/{sid}").json()
    worn = s["settings"]["faces"]["default"]
    assert worn == s["messages"][1]["files"][0]["name"]                       # one of this chat's own files
    assert client.get(f"/api/sessions/{sid}/files/{worn}").content == PNG
    assert client.get("/api/avatars").json() == {"avatars": {}}               # nothing changed everywhere
    assert "faces" not in a_chat_settings(client)                             # nor in another chat

    # the person does the same from the sheet: a file, in this chat only, for anyone
    mine = upload(client, sid, "cid.png", PNG)
    r = client.post("/api/avatars/you", json={"session": sid, "file": mine, "scope": "chat"}).json()
    assert r["faces"] == {"default": worn, "you": mine} and r["avatars"] == {}
    # ... and takes one off, back to what is worn everywhere
    left = client.patch(f"/api/sessions/{sid}", json={"settings": {"faces": {"default": worn}}}).json()
    assert left["settings"]["faces"] == {"default": worn}
    # what is not a picture of this chat's is not worn in it
    notes = upload(client, sid, "notes.txt", b"words")
    for bad in ({"file": notes}, {"file": "nope.png"}):
        assert client.post("/api/avatars/you", json={"session": sid, "scope": "chat", **bad}).status_code == 400
    for bad in ({"you": "../../etc/passwd"}, {"you": ""}, {"you": 3}, ["you"]):
        assert client.patch(f"/api/sessions/{sid}", json={"settings": {"faces": bad}}).status_code == 400, bad


def test_the_settings_tab_sets_a_face_for_every_chat_with_no_chat_open(client):
    import base64
    r = client.post("/api/avatars/default", json={"data": base64.b64encode(PNG).decode(), "filename": "wren.png"})
    assert r.status_code == 200 and r.json()["avatars"]["default"]["type"] == "image/png"
    assert client.get("/api/avatars/default").content == PNG
    assert client.post("/api/avatars/default", json={"data": base64.b64encode(b"not a picture").decode()}).status_code == 400
    assert client.post("/api/avatars/default", json={"data": "%%%"}).status_code == 400


def test_everyone_who_speaks_can_have_a_face_and_a_colour(client):
    """Chad, 17 Nov 2026: "definitely avatars for all, color bubbles for all,
    set up in the per chat settings". A face goes by who it is - `you` the
    person, `default` the chat's Claude, a local model or a plugin by name -
    and a bubble colour is the chat's own setting, a hue per voice."""
    sid = a_chat(client)
    face = upload(client, sid, "me.png", PNG)
    for who in ("you", "default", "qwen3-30b-a3b", "Super-Dude-Bros"):
        assert client.post(f"/api/avatars/{who}", json={"session": sid, "file": face}).status_code == 200
    assert set(client.get("/api/avatars").json()["avatars"]) == {"you", "default", "qwen3-30b-a3b", "Super-Dude-Bros"}

    colors = {"you": 200, "default": 330, "qwen3-30b-a3b": 0}
    s = client.patch(f"/api/sessions/{sid}", json={"settings": {"colors": colors}}).json()
    assert s["settings"]["colors"] == colors and s["settings"]["voice"] == "claude"     # beside the rest, not instead
    assert client.get(f"/api/sessions/{sid}").json()["settings"]["colors"] == colors
    # another chat has its own
    assert "colors" not in a_chat_settings(client)
    # an empty one is none at all; what is not a hue is refused
    assert "colors" not in client.patch(f"/api/sessions/{sid}", json={"settings": {"colors": {}}}).json()["settings"]
    for bad in ({"you": 360}, {"you": -1}, {"you": "red"}, {"you": True}, {"": 10}, ["you"], "blue"):
        assert client.patch(f"/api/sessions/{sid}", json={"settings": {"colors": bad}}).status_code == 400, bad


def a_chat_settings(client):
    return client.get(f"/api/sessions/{a_chat(client)}").json()["settings"]


def test_a_picture_a_tool_drew_is_worn_by_the_name_the_tool_gave(client, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DRAW", "1")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", "drew myself: ![avatar](023502-42-a-wren.png)")
    sid = a_chat(client)
    events = stream(client, {"session": sid, "prompt": "draw yourself"})
    assert [(e["name"], e["scope"]) for e in events if e["type"] == "avatar"] == [("default", "chat")]
    s = client.get(f"/api/sessions/{sid}").json()
    said = s["messages"][1]
    kept = said["files"][0]["name"]
    assert f"![avatar](files/{kept})" in said["content"], "pointed at the chat's copy, so the answer shows it"
    assert s["settings"]["faces"] == {"default": kept}
