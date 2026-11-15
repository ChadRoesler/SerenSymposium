"""A room: seats at one chat's table, each its own claude conversation in its
own folder, going round in order and hearing each other (seren_symposium.room).
The seats are played by tests/fake_claude.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from seren_symposium import room
from seren_symposium.config import SymposiumConfig

from test_terminal import rec, write          # a terminal's transcript, as the CLI keeps one

FAKE = str(Path(__file__).with_name("fake_claude.py"))
SEATS = [{"name": "lodestar"}, {"name": "kitchen"}, {"name": "soma"}]


# ── The rules, alone ──────────────────────────────────────────────────────

def test_everyone_answers_unless_someone_is_named():
    assert room.who_answers(SEATS, "what do we think?") == ["lodestar", "kitchen", "soma"]
    assert room.who_answers(SEATS, "@soma and @kitchen, thoughts?") == ["kitchen", "soma"]   # seat order
    assert room.who_answers(SEATS, "mail me at me@kitchen.example") == ["lodestar", "kitchen", "soma"]
    assert room.who_answers(SEATS, "@nobody hi") == ["lodestar", "kitchen", "soma"]
    assert room.who_answers(SEATS, "anything", to="soma") == ["soma"]
    assert room.who_answers(SEATS, "anything", to=["soma", "lodestar"]) == ["lodestar", "soma"]
    assert "no seat called margin" in room.who_answers(SEATS, "x", to=["margin"])


def test_every_line_handed_over_says_who_said_it():
    said = room.handover([
        {"role": "user", "content": "go round"},
        {"role": "assistant", "content": "from before the room", "meta": {}},
        {"role": "assistant", "content": "lodestar says", "seat": "lodestar"},
        {"role": "assistant", "content": "I said", "seat": "kitchen"},
        {"role": "user", "content": "the table spoke", "by": "Super Dude Bros"},
    ], me="kitchen", you="Chad", voice="wren", resumed=True)
    assert said.splitlines() == [
        "In the room since you last spoke:", "",
        "[Chad] go round", "[wren] from before the room", "[lodestar] lodestar says",
        "[kitchen (you)] I said", "[Super Dude Bros] the table spoke", "",
        "Your turn, kitchen."]


def test_pass_is_only_a_pass():
    assert room.passed("pass") and room.passed("  Pass.") and room.passed("(pass)")
    assert not room.passed("I'll pass on that, but here's why")


# ── A room, through the app ───────────────────────────────────────────────

@pytest.fixture
def runs(tmp_path, monkeypatch):
    log = tmp_path / "claude-runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_SEATS", json.dumps({
        "lodestar": "lodestar here, from {cwd}", "kitchen": "kitchen disagrees", "soma": "soma here"}))
    return lambda: [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


@pytest.fixture
def repos(tmp_path):
    out = {}
    for name in ("SerenLodestar", "SerenKitchen", "SerenSymposium"):
        (tmp_path / name).mkdir()
        out[name] = tmp_path / name
    return out


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


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def a_room(client, repos, *names):
    sid = client.post("/api/sessions", json={"settings": {"voice": "claude"}}).json()["id"]
    for name in names:
        r = client.post(f"/api/sessions/{sid}/seats", json={"name": name, "cwd": str(repos[f"Seren{name.title()}"])})
        assert r.status_code == 201, r.text
    return sid


def test_a_message_goes_round_the_table_in_order_each_in_its_own_folder(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    events = stream(client, {"session": sid, "prompt": "should the room fork warm seats?"})

    assert [e["type"] for e in events if e["type"] in ("seat", "done", "saved", "round")] == [
        "seat", "done", "saved", "seat", "done", "saved", "round"]
    assert all(e.get("seat") for e in events if e["type"] != "round")      # every line says whose
    first, second = runs()
    assert Path(first["cwd"]).name == "SerenLodestar" and Path(second["cwd"]).name == "SerenKitchen"

    # the second heard the first, labelled - and the person's words, once
    assert second["prompt"] == ("The room so far:\n\n[Chad] should the room fork warm seats?\n"
                                "[lodestar] lodestar here, from SerenLodestar\n\nYour turn, kitchen.")
    # and each was told where it sits
    system = flag(second["argv"], "--append-system-prompt")
    assert "seats: lodestar, kitchen. You are kitchen." in system

    s = client.get(f"/api/sessions/{sid}").json()
    assert [(m["role"], m.get("seat"), m["content"]) for m in s["messages"]] == [
        ("user", None, "should the room fork warm seats?"),
        ("assistant", "lodestar", "lodestar here, from SerenLodestar"),
        ("assistant", "kitchen", "kitchen disagrees")]
    lod, kit = s["seats"]
    assert lod["claude"]["seen"] == 2 and kit["claude"]["seen"] == 3
    assert lod["claude"]["session"] != kit["claude"]["session"]


def test_next_round_each_resumes_its_own_and_hears_only_whats_new(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    stream(client, {"session": sid, "prompt": "one"})
    stream(client, {"session": sid, "prompt": "two"})
    l1, k1, l2, k2 = runs()

    assert flag(l2["argv"], "--resume") == flag(l1["argv"], "--session-id")
    assert flag(k2["argv"], "--resume") == flag(k1["argv"], "--session-id")
    # lodestar missed kitchen's last word, then the new message; not its own
    assert l2["prompt"] == ("In the room since you last spoke:\n\n[kitchen] kitchen disagrees\n"
                            "[Chad] two\n\nYour turn, lodestar.")
    assert k2["prompt"] == ("In the room since you last spoke:\n\n[Chad] two\n"
                            "[lodestar] lodestar here, from SerenLodestar\n\nYour turn, kitchen.")


def test_naming_a_seat_asks_only_that_one(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    events = stream(client, {"session": sid, "prompt": "@kitchen just you"})
    assert [e["seat"] for e in events if e["type"] == "seat"] == ["kitchen"]
    stream(client, {"session": sid, "prompt": "and you", "to": "lodestar"})
    assert [Path(r["cwd"]).name for r in runs()] == ["SerenKitchen", "SerenLodestar"]
    r = client.post("/api/chat/stream", json={"session": sid, "prompt": "x", "to": "margin"})
    assert r.status_code == 400 and "no seat called margin" in r.json()["error"]


def test_a_seat_that_passes_says_nothing_but_has_heard(client, repos, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SEATS", json.dumps({"lodestar": "pass", "kitchen": "kitchen disagrees"}))
    sid = a_room(client, repos, "lodestar", "kitchen")
    events = stream(client, {"session": sid, "prompt": "anything to add?"})

    said = [e["type"] for e in events if e.get("seat") == "lodestar"]
    assert "pass" in said and "done" not in said and "chunk" in said     # it spoke "pass", nothing kept
    s = client.get(f"/api/sessions/{sid}").json()
    # the person's words went in with the seat that passed (it came back first)
    assert [(m["role"], m.get("seat")) for m in s["messages"]] == [("user", None), ("assistant", "kitchen")]
    assert s["seats"][0]["claude"]["seen"] == 1                       # heard the message, said nothing
    assert "[Chad] anything to add?" in runs()[1]["prompt"]           # kitchen got it once, from the chat


def test_inviting_someone_keeps_the_claude_who_was_there(client, repos, runs):
    sid = client.post("/api/sessions", json={
        "settings": {"voice": "claude", "cwd": str(repos["SerenSymposium"])}}).json()["id"]
    stream(client, {"session": sid, "prompt": "just us so far"})
    first = client.get(f"/api/sessions/{sid}").json()["claude"]

    s = client.post(f"/api/sessions/{sid}/seats", json={"name": "kitchen", "cwd": str(repos["SerenKitchen"])}).json()
    assert "claude" not in s
    host, guest = s["seats"]
    assert host == {"name": "symposium", "cwd": str(repos["SerenSymposium"]), "claude": first}
    assert guest == {"name": "kitchen", "cwd": str(repos["SerenKitchen"])}

    stream(client, {"session": sid, "prompt": "say hi to kitchen"})
    _, h, g = runs()
    assert flag(h["argv"], "--resume") == first["session"]           # the same conversation carries on
    assert h["prompt"].startswith("In the room since you last spoke:\n\n[Chad] say hi to kitchen")
    assert g["prompt"].startswith("The room so far:\n\n[Chad] just us so far\n[claude] Hello there\n")


def test_a_terminals_conversation_can_take_a_seat(client, repos, runs, tmp_path):
    talk = write(tmp_path / "projects", repos["SerenLodestar"], [rec("user", "lodestar stuff")], age=3600)
    sid = a_room(client, repos, "kitchen")
    s = client.post(f"/api/sessions/{sid}/seats", json={"name": "lodestar", "pickup": talk}).json()
    seat = room.seat(s, "lodestar")
    assert seat == {"name": "lodestar", "cwd": str(repos["SerenLodestar"]), "claude": {"session": talk, "seen": 0}}
    assert talk not in [r["id"] for r in client.get("/api/claude/sessions").json()["sessions"]]

    stream(client, {"session": sid, "prompt": "welcome"})
    k, l = runs()
    assert flag(l["argv"], "--resume") == talk and "--fork-session" not in l["argv"]
    assert l["prompt"].startswith("In the room since you last spoke:\n\n[Chad] welcome\n[kitchen] kitchen disagrees")


def test_a_warm_conversation_takes_its_seat_forked(client, repos, runs, tmp_path):
    talk = write(tmp_path / "projects", repos["SerenLodestar"], [rec("user", "still typing")], age=5)
    sid = a_room(client, repos, "kitchen")
    client.post(f"/api/sessions/{sid}/seats", json={"name": "lodestar", "pickup": talk})
    stream(client, {"session": sid, "prompt": "hi", "to": "lodestar"})
    (run,) = runs()
    assert "--fork-session" in run["argv"]
    seat = room.seat(client.get(f"/api/sessions/{sid}").json(), "lodestar")
    assert seat["claude"]["session"] != talk and "fork" not in seat["claude"]


def test_seats_come_and_go_and_what_they_said_stays(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    assert client.post(f"/api/sessions/{sid}/seats", json={"name": "kitchen"}).status_code == 400   # taken
    assert client.post(f"/api/sessions/{sid}/seats", json={"name": "no spaces"}).status_code == 400
    stream(client, {"session": sid, "prompt": "hi"})

    s = client.delete(f"/api/sessions/{sid}/seats/kitchen").json()
    assert [x["name"] for x in s["seats"]] == ["lodestar"]
    assert [m.get("seat") for m in s["messages"]] == [None, "lodestar", "kitchen"]
    assert client.delete(f"/api/sessions/{sid}/seats/kitchen").status_code == 404
    s = client.delete(f"/api/sessions/{sid}/seats/lodestar").json()
    assert "seats" not in s                                            # a plain chat again


def test_rewinding_what_a_seat_heard_starts_it_over(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    stream(client, {"session": sid, "prompt": "one"})
    s = client.post(f"/api/sessions/{sid}/rewind", json={"to": 2}).json()
    lod, kit = s["seats"]
    assert lod["claude"]["seen"] == 2 and "claude" not in kit           # kitchen heard past the cut

    stream(client, {"session": sid, "prompt": "again"})
    k2 = runs()[-1]
    assert "--session-id" in k2["argv"] and k2["prompt"].startswith("The room so far:")


def test_what_a_room_cant_do_yet_it_says(client, repos, runs):
    sid = a_room(client, repos, "lodestar")
    stream(client, {"session": sid, "prompt": "one"})
    r = client.post("/api/chat/stream", json={"session": sid, "retry": True})
    assert r.status_code == 409 and "retry" in r.json()["error"]
    r = client.post("/api/chat", json={"session": sid, "prompt": "two"})
    assert r.status_code == 400 and "streams" in r.json()["error"]
