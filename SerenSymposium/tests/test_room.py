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

def test_an_at_name_quoted_as_code_calls_nobody():
    """20 Nov 2026: workbench reported the @ test as `@starwright hi` and
    starwright was called back into a round that was over. A name in code -
    inline or fenced - is quoted, not said."""
    names = ["symposium", "workbench", "starwright"]
    assert room.mentioned(names, "That's `@starwright hi` working as the table says.") == ([], False)
    assert room.mentioned(names, "| `@starwright! @workbench` | starwright, workbench only |") == ([], False)
    assert room.mentioned(names, "Try this:\n```\n@workbench! run it\n```\nthen tell me.") == ([], False)
    assert room.mentioned(names, "```\n@workbench never closed") == ([], False)
    # the holes starwright found in the first try: a double-backtick span (a backtick inside code),
    # a ~~~ fence, a four-backtick fence around three, and a fence closed by a longer one
    assert room.mentioned(names, "I quoted it as `` `@starwright hi` `` above") == ([], False)
    assert room.mentioned(names, "~~~\n@starwright hi\n~~~\nthat was the test") == ([], False)
    assert room.mentioned(names, "````\n```\n@workbench! x\n```\n@starwright still code\n````\nend") == ([], False)
    assert room.mentioned(names, "```\n@starwright\n`````\n@workbench outside") == (["workbench"], False)
    # an @ in a link's path or an email address is not a call (workbench's table, 20 Nov 2026)
    assert room.mentioned(names, "see https://x.com/@starwright/status and chad@workbench.local") == ([], False)
    assert room.mentioned(names, "(@starwright) and **@workbench**") == (["starwright", "workbench"], False)
    # a lone backtick is only a backtick: it does not swallow what follows
    assert room.mentioned(names, "it's a ` stray, @workbench") == (["workbench"], False)
    # inline backticks after a fence-looking start are a span, not a fence
    assert room.mentioned(names, "```@starwright``` but @workbench") == (["workbench"], False)
    # said outright, beside a quote, it still calls
    assert room.mentioned(names, "`@starwright` was quoted; @workbench, over to you") == (["workbench"], False)
    assert room.who_answers([{"name": n} for n in names], "see `@starwright!` - @workbench! only") == ["workbench"]


def test_everyone_answers_unless_someone_is_named():
    assert room.who_answers(SEATS, "what do we think?") == ["lodestar", "kitchen", "soma"]
    # @name goes first, in the order named; the rest follow in seat order
    assert room.who_answers(SEATS, "@soma and @kitchen, thoughts?") == ["soma", "kitchen", "lodestar"]
    assert room.who_answers(SEATS, "what about you, @kitchen.") == ["kitchen", "lodestar", "soma"]   # the sentence's full stop
    assert room.who_answers(SEATS, "@Soma first") == ["soma", "lodestar", "kitchen"]                 # however it's typed
    # @name! asks only the ones named
    assert room.who_answers(SEATS, "@soma! just you") == ["soma"]
    assert room.who_answers(SEATS, "@kitchen and @soma! only") == ["kitchen", "soma"]
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
    # a few words after it are still a pass; a real answer that starts with the word is not
    assert room.passed("Pass - nothing new from me.") and room.passed("pass, lodestar covered it\n")
    assert not room.passed("pass, but the venv is stale")
    assert not room.passed("Pass the fix to kitchen")
    assert not room.passed("pass.\n\nActually, one thing: the retry loop is wrong")
    assert not room.passed("passing tests: 12")


def test_the_table_hears_a_seats_last_piece_not_its_working():
    working = {"role": "assistant", "seat": "lodestar", "content": "Checking the box...\n\nIt is up.",
               "final": "It is up."}
    assert room.heard(working) == "It is up."
    assert room.heard({"role": "assistant", "seat": "lodestar", "content": "one piece"}) == "one piece"
    assert room.heard({"role": "user", "content": "the person", "final": "never theirs"}) == "the person"


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


def test_naming_a_seat_puts_it_first_and_a_bang_asks_only_that_one(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    events = stream(client, {"session": sid, "prompt": "@kitchen you first"})
    assert [e["seat"] for e in events if e["type"] == "seat"] == ["kitchen", "lodestar"]
    events = stream(client, {"session": sid, "prompt": "@kitchen! just you"})
    assert [e["seat"] for e in events if e["type"] == "seat"] == ["kitchen"]
    stream(client, {"session": sid, "prompt": "and you", "to": "lodestar"})
    assert [Path(r["cwd"]).name for r in runs()] == ["SerenKitchen", "SerenLodestar", "SerenKitchen", "SerenLodestar"]
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


def test_a_seat_that_works_first_and_then_passes_has_passed(client, repos, runs, monkeypatch):
    """The first tangle: 'Checking the real box... pass' was kept as an answer."""
    monkeypatch.setenv("FAKE_CLAUDE_SAY_FIRST", "Checking the real box...")
    monkeypatch.setenv("FAKE_CLAUDE_SEATS", json.dumps({"lodestar": "Pass - nothing new.", "kitchen": "kitchen disagrees"}))
    sid = a_room(client, repos, "lodestar", "kitchen")
    events = stream(client, {"session": sid, "prompt": "anything to add?"})

    assert "pass" in [e["type"] for e in events if e.get("seat") == "lodestar"]
    s = client.get(f"/api/sessions/{sid}").json()
    assert [(m["role"], m.get("seat")) for m in s["messages"]] == [("user", None), ("assistant", "kitchen")]


def test_the_next_seat_is_handed_the_conclusion_and_the_chat_keeps_it_all(client, repos, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SAY_FIRST", "Now the tests:")
    sid = a_room(client, repos, "lodestar", "kitchen")
    stream(client, {"session": sid, "prompt": "is it up?"})

    _, second = runs()
    assert second["prompt"] == ("The room so far:\n\n[Chad] is it up?\n"
                                "[lodestar] lodestar here, from SerenLodestar\n\nYour turn, kitchen.")
    s = client.get(f"/api/sessions/{sid}").json()
    said = s["messages"][1]
    assert said["content"] == "Now the tests:\n\nlodestar here, from SerenLodestar"      # the person reads all of it
    assert said["final"] == "lodestar here, from SerenLodestar"
    # messages that come in from outside keep it only while the answer still has those words
    from seren_symposium.sessions import clean_messages
    assert clean_messages([said])[0]["final"] == said["final"]
    assert "final" not in clean_messages([{**said, "content": "rewritten"}])[0]


def test_inviting_someone_keeps_the_claude_who_was_there(client, repos, runs):
    sid = client.post("/api/sessions", json={
        "settings": {"voice": "claude", "cwd": str(repos["SerenSymposium"])}}).json()["id"]
    stream(client, {"session": sid, "prompt": "just us so far"})
    first = client.get(f"/api/sessions/{sid}").json()["claude"]

    s = client.post(f"/api/sessions/{sid}/seats", json={"name": "kitchen", "cwd": str(repos["SerenKitchen"])}).json()
    assert "claude" not in s
    host, guest = s["seats"]
    assert host == {"name": "symposium", "cwd": str(repos["SerenSymposium"]), "hue": room.HUES[0], "claude": first}
    assert guest == {"name": "kitchen", "cwd": str(repos["SerenKitchen"]), "hue": room.HUES[1]}

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
    assert seat == {"name": "lodestar", "cwd": str(repos["SerenLodestar"]), "hue": room.HUES[1],
                    "claude": {"session": talk, "seen": 0}}
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


def test_a_seat_that_leaves_comes_back_as_herself_and_the_last_one_out_stays_with_the_chat(client, repos, runs):
    """Removing a seat used to throw away the chat's only note of which
    conversation she was: her words stayed, she did not, and the same name
    invited again was a stranger handed the whole chat. Now she is kept as
    having left, and comes back by name with her conversation and what she
    had already heard. Found 19 Nov 2026, before Chad's "what happens if I
    remove a wren" test; Margin's "send a seat home ... called back by name"."""
    sid = a_room(client, repos, "lodestar", "kitchen")
    stream(client, {"session": sid, "prompt": "hi"})
    before = {x["name"]: x for x in client.get(f"/api/sessions/{sid}").json()["seats"]}
    hers = before["kitchen"]["claude"]["session"]

    s = client.delete(f"/api/sessions/{sid}/seats/kitchen").json()
    assert [x["name"] for x in s["seats"]] == ["lodestar"]
    assert [m.get("seat") for m in s["messages"]] == [None, "lodestar", "kitchen"], "what she said stays"
    assert s["left"] == [{k: before["kitchen"][k] for k in ("name", "cwd", "hue", "claude")}]

    stream(client, {"session": sid, "prompt": "while she was out"})            # a round she misses
    s = client.post(f"/api/sessions/{sid}/seats", json={"name": "kitchen"}).json()
    kit = next(x for x in s["seats"] if x["name"] == "kitchen")
    assert kit["claude"]["session"] == hers and kit["cwd"] == before["kitchen"]["cwd"] and kit["hue"] == before["kitchen"]["hue"]
    assert "left" not in s
    stream(client, {"session": sid, "prompt": "welcome back"})
    back = runs()[-1]
    assert flag(back["argv"], "--resume") == hers, "her own conversation, resumed"
    assert "[Chad] while she was out" in back["prompt"] and "[Chad] hi" not in back["prompt"], "only what she missed"

    # the last one out: her conversation stays with the chat, which is a plain chat again
    client.delete(f"/api/sessions/{sid}/seats/kitchen")
    s = client.delete(f"/api/sessions/{sid}/seats/lodestar").json()
    assert "seats" not in s and s["claude"]["session"] == before["lodestar"]["claude"]["session"]
    assert s["settings"]["cwd"] == before["lodestar"]["cwd"]
    stream(client, {"session": sid, "prompt": "just us now"})
    assert flag(runs()[-1]["argv"], "--resume") == before["lodestar"]["claude"]["session"]


def test_the_table_can_be_reseated(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    s = client.patch(f"/api/sessions/{sid}/seats", json={"order": ["kitchen", "lodestar"]}).json()
    assert [x["name"] for x in s["seats"]] == ["kitchen", "lodestar"]
    assert [x["hue"] for x in s["seats"]] == [room.HUES[1], room.HUES[0]]          # a seat keeps its colour
    events = stream(client, {"session": sid, "prompt": "round we go"})
    assert [e["seat"] for e in events if e["type"] == "seat"] == ["kitchen", "lodestar"]
    for bad in (["kitchen"], ["kitchen", "kitchen"], ["kitchen", "margin"], "kitchen"):
        assert client.patch(f"/api/sessions/{sid}/seats", json={"order": bad}).status_code == 400


def test_a_seat_can_be_renamed_and_what_it_said_goes_with_it(client, repos, runs):
    sid = a_room(client, repos, "lodestar", "kitchen")
    stream(client, {"session": sid, "prompt": "one"})
    before = client.get(f"/api/sessions/{sid}").json()["seats"][0]["claude"]

    s = client.patch(f"/api/sessions/{sid}/seats/lodestar", json={"name": "north", "hue": 12}).json()
    north = s["seats"][0]
    assert (north["name"], north["hue"], north["claude"]) == ("north", 12, before)   # the same conversation
    assert [m.get("seat") for m in s["messages"]] == [None, "north", "kitchen"]
    assert client.patch(f"/api/sessions/{sid}/seats/north", json={"name": "kitchen"}).status_code == 400   # taken
    assert client.patch(f"/api/sessions/{sid}/seats/north", json={"name": "no spaces"}).status_code == 400
    assert client.patch(f"/api/sessions/{sid}/seats/north", json={"hue": 400}).status_code == 400
    assert client.patch(f"/api/sessions/{sid}/seats/lodestar", json={"name": "x"}).status_code == 404

    stream(client, {"session": sid, "prompt": "@north you there?"})
    n2 = runs()[2]
    assert n2["prompt"].endswith("Your turn, north.") and "You are north." in flag(n2["argv"], "--append-system-prompt")


def seats_of(events):
    return [(e["seat"], e.get("called_by")) for e in events if e["type"] == "seat"]


def test_a_seat_that_names_a_seat_still_due_only_moves_it_up(client, repos, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SEATS", json.dumps({
        "lodestar": "@margin your count is off", "kitchen": "kitchen disagrees", "margin": "margin here"}))
    repos["SerenMargin"] = repos["SerenLodestar"].parent / "SerenMargin"
    repos["SerenMargin"].mkdir()
    sid = a_room(client, repos, "lodestar", "kitchen", "margin")
    events = stream(client, {"session": sid, "prompt": "go round"})
    assert seats_of(events) == [("lodestar", None), ("margin", "lodestar"), ("kitchen", None)]
    assert events[-1] == {"type": "round", "seats": ["lodestar", "margin", "kitchen"], "volleys": 0}


def test_two_seats_argue_it_out_until_the_limit_and_then_the_person_referees(make_client, repos, runs, monkeypatch, tmp_path):
    """Chad, 17 Nov 2026: a back and forth like the backchannel, him the
    referee, not the relay. Each @name is a turn nobody asked for; the limit
    is what keeps two seats from going round all night."""
    monkeypatch.setenv("FAKE_CLAUDE_SEATS", json.dumps({
        "lodestar": "@kitchen what say you", "kitchen": "@lodestar no, you"}))
    cfg = SymposiumConfig()
    cfg.claude.command, cfg.claude.projects_dir, cfg.ui.you = [sys.executable, FAKE], str(tmp_path / "projects"), "Chad"
    cfg.claude.room_volleys = 3
    tc = make_client(cfg)
    sid = a_room(tc, repos, "lodestar", "kitchen")
    events = stream(tc, {"session": sid, "prompt": "settle it"})
    assert seats_of(events) == [("lodestar", None), ("kitchen", "lodestar"), ("lodestar", "kitchen"),
                                ("kitchen", "lodestar"), ("lodestar", "kitchen")]
    assert events[-1] == {"type": "round", "seats": ["lodestar", "kitchen", "lodestar", "kitchen", "lodestar"],
                          "volleys": 3, "capped": True}
    said = tc.get(f"/api/sessions/{sid}").json()["messages"]
    assert [m.get("seat") for m in said] == [None, "lodestar", "kitchen", "lodestar", "kitchen", "lodestar"]
    # each was handed only what the other had just said
    assert runs()[2]["prompt"] == ("In the room since you last spoke:\n\n[kitchen] @lodestar no, you\n\nYour turn, lodestar.")

    # a seat asked alone can still call one in; and with no volleys allowed, nobody calls anybody
    events = stream(tc, {"session": sid, "prompt": "@kitchen! only you"})
    assert seats_of(events)[:2] == [("kitchen", None), ("lodestar", "kitchen")]
    cfg.claude.room_volleys = 0
    events = stream(tc, {"session": sid, "prompt": "once round, no arguing"})
    assert seats_of(events) == [("lodestar", None), ("kitchen", None)]


def test_a_pass_calls_on_nobody_so_an_argument_ends_when_one_side_has_nothing_new(client, repos, runs, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SEATS", json.dumps({
        "lodestar": "pass, @kitchen has it", "kitchen": "@lodestar anything?"}))
    sid = a_room(client, repos, "kitchen", "lodestar")
    events = stream(client, {"session": sid, "prompt": "go"})
    assert seats_of(events) == [("kitchen", None), ("lodestar", "kitchen")]
    assert events[-1]["volleys"] == 0 and "capped" not in events[-1]


def test_a_round_stopped_keeps_the_seat_it_caught_and_asks_nobody_after(client, repos, runs, monkeypatch, tmp_path):
    """Stop in a room: the seat that was speaking is kept as far as it got,
    with the person's message; the seats after it are not asked."""
    import threading
    import time
    gate = tmp_path / "let-it-finish"
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    monkeypatch.setenv("FAKE_CLAUDE_SAY_FIRST", "Checking the real box...")
    sid = a_room(client, repos, "lodestar", "kitchen")
    got = {}
    asker = threading.Thread(target=lambda: got.update(r=client.post("/api/chat/stream",
                                                                    json={"session": sid, "prompt": "go round"})))
    asker.start()
    live, end = client.app.state.live, time.time() + 10
    while time.time() < end and not (sid in live and any(e["type"] == "thinking" for e in live[sid].events)):
        time.sleep(0.05)
    assert client.post(f"/api/sessions/{sid}/stop", json={}).json() == {"ok": True}
    asker.join(15)
    assert not asker.is_alive()

    events = [json.loads(l) for l in got["r"].text.splitlines() if l.strip()]
    assert seats_of(events) == [("lodestar", None)]                                # kitchen was never asked
    assert events[-1] == {"type": "round", "seats": ["lodestar"], "volleys": 0, "stopped": True}
    s = client.get(f"/api/sessions/{sid}").json()
    assert [(m["role"], m.get("seat"), m["content"]) for m in s["messages"]] == [
        ("user", None, "go round"), ("assistant", "lodestar", "Checking the real box...")]
    assert s["messages"][1]["meta"]["stopped"] is True
    lod, kit = s["seats"]
    assert lod["claude"]["seen"] == 1 and "claude" not in kit and s["answering"] is False

    # the next round: the seat is handed its own cut-short words back, said to be that,
    # and the other hears them as a turn that was stopped, not as a conclusion
    gate.write_text("go")
    stream(client, {"session": sid, "prompt": "carry on"})
    l2, k2 = runs()[-2:]
    assert l2["prompt"].startswith("In the room since you last spoke:\n\n"
                                   "[lodestar (you, stopped before you finished)] Checking the real box...")
    assert "Tools you had started before the stop" in l2["prompt"] and "[Chad] carry on" in l2["prompt"]
    assert "[lodestar, stopped before finishing] Checking the real box..." in k2["prompt"]


def test_each_seat_has_its_own_prompt_stack(client, repos, runs, monkeypatch):
    """Chad, 17 Nov 2026: "what wren is given, should have a separate per wren" -
    each seat is its own conversation in its own folder, so one stack for the
    chat showed nobody's."""
    (repos["SerenKitchen"] / "CLAUDE.md").write_text("the kitchen's rules", encoding="utf-8")
    sid = a_room(client, repos, "lodestar", "kitchen")
    stream(client, {"session": sid, "prompt": "one", "to": "lodestar"})         # kitchen has yet to hear it

    def layer(st, key):
        return next(x for x in st["layers"] if x["key"] == key)
    first = client.get("/api/stack", params={"session": sid}).json()
    assert (first["seat"], first["seats"], first["voice"]) == ("lodestar", ["lodestar", "kitchen"], "claude")
    assert "You are lodestar." in layer(first, "framing")["text"]
    assert "lodestar's last answer" in layer(first, "tools")["where"] and layer(first, "history")["resumes"] is True

    kit = client.get("/api/stack", params={"session": sid, "seat": "kitchen"}).json()
    assert kit["seat"] == "kitchen" and "You are kitchen." in layer(kit, "framing")["text"]
    assert "SerenKitchen" in layer(kit, "memory")["where"]                     # its own folder's files
    assert any(Path(f["path"]).name == "CLAUDE.md" for f in layer(kit, "memory")["files"])
    assert layer(kit, "history")["resumes"] is False and layer(kit, "history")["handed"] == 2
    assert client.get("/api/stack", params={"session": sid, "seat": "margin"}).status_code == 404
    # a seat's start-up report is the stack's, not the chat's: the window is not sent it
    assert all("claude_init" not in x for x in client.get(f"/api/sessions/{sid}").json()["seats"])


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


def test_a_round_can_be_asked_to_end_with_the_seat_speaking(client, repos, runs, monkeypatch, tmp_path):
    """Between stop (a seat cut off mid-word) and waiting out the whole round:
    {after: seat} lets the seat speaking finish and asks nobody after it, so
    what the person has to say next starts a new round. Chad, 18 Nov 2026,
    on which of the three it should be: "It depends ... main settings for
    default, per chat for chat, and then being able to override on the
    message sent". Nobody is stopped, so nothing is marked stopped."""
    import threading
    import time
    gate = tmp_path / "let-it-finish"
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    sid = a_room(client, repos, "lodestar", "kitchen", "symposium")
    got = {}
    asker = threading.Thread(target=lambda: got.update(r=client.post("/api/chat/stream",
                                                                    json={"session": sid, "prompt": "go round"})))
    asker.start()
    live, end = client.app.state.live, time.time() + 10
    while time.time() < end and not (sid in live and any(e["type"] == "thinking" for e in live[sid].events)):
        time.sleep(0.05)
    assert client.post(f"/api/sessions/{sid}/stop", json={"after": "seat"}).json() == {"ok": True, "after": "seat"}
    gate.write_text("go")
    asker.join(15)
    assert not asker.is_alive()

    events = [json.loads(l) for l in got["r"].text.splitlines() if l.strip()]
    assert seats_of(events) == [("lodestar", None)], "the two still due were never asked"
    assert events[-1] == {"type": "round", "seats": ["lodestar"], "volleys": 0, "cut": True,
                          "not_asked": ["kitchen", "symposium"]}
    assert not any(e["type"] == "stopped" for e in events), "nobody was cut off"
    s = client.get(f"/api/sessions/{sid}").json()
    assert [(m["role"], m.get("seat"), m["content"]) for m in s["messages"]] == [
        ("user", None, "go round"), ("assistant", "lodestar", "lodestar here, from SerenLodestar")]
    assert "stopped" not in s["messages"][1]["meta"] and s["answering"] is False

    # the next round is a round like any other: the ones not asked hear what they missed
    stream(client, {"session": sid, "prompt": "and now this"})
    k2 = runs()[-2]
    assert "[Chad] go round" in k2["prompt"] and "[lodestar] lodestar here, from SerenLodestar" in k2["prompt"] and "[Chad] and now this" in k2["prompt"]


def test_a_chat_with_one_voice_has_no_round_to_cut_and_the_setting_is_checked(client, tmp_path, monkeypatch):
    import threading
    import time
    gate = tmp_path / "let-it-finish"
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    tc = client
    sid = tc.post("/api/sessions", json={"settings": {"voice": "claude"}}).json()["id"]
    got = {}
    asker = threading.Thread(target=lambda: got.update(r=tc.post("/api/chat/stream", json={"session": sid, "prompt": "think"})))
    asker.start()
    end = time.time() + 10
    while time.time() < end and sid not in tc.app.state.live:
        time.sleep(0.05)
    assert tc.post(f"/api/sessions/{sid}/stop", json={"after": "seat"}).json() == {"ok": True, "after": "answer"}
    gate.write_text("go")
    asker.join(15)
    said = tc.get(f"/api/sessions/{sid}").json()["messages"][1]
    assert said["content"] == "Hello there" and "stopped" not in said["meta"], "the answer simply finished"

    # the setting: every chat's (kept in the prefs), and a chat's own over it
    assert tc.get("/api/config").json()["interrupt"] == "round"
    assert tc.patch("/api/prefs", json={"interrupt": "seat"}).status_code == 200
    assert tc.get("/api/config").json()["interrupt"] == "seat"
    assert tc.patch("/api/prefs", json={"interrupt": "soon"}).status_code == 400
    assert tc.patch(f"/api/sessions/{sid}", json={"settings": {"interrupt": "now"}}).json()["settings"]["interrupt"] == "now"
    assert tc.patch(f"/api/sessions/{sid}", json={"settings": {"interrupt": "soon"}}).status_code == 400
    assert "interrupt" not in tc.patch(f"/api/sessions/{sid}", json={"settings": {"interrupt": None}}).json()["settings"]
