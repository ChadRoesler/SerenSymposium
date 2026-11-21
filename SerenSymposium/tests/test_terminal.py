"""Picking up a terminal's conversation: the CLI's own transcripts read
(seren_symposium.terminal), and a chat that resumes one - in its folder,
forked when a terminal may still be in it."""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from seren_symposium import terminal
from seren_symposium.config import SymposiumConfig

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def rec(kind, content=None, **extra):
    r = {"type": kind, "timestamp": "2026-11-14T20:35:01.000Z", **extra}
    if content is not None:
        r["message"] = {"role": kind, "content": content}
    return r


def write(root: Path, cwd: Path, records, sid=None, age=0.0, entrypoint="cli") -> str:
    """A transcript as the CLI keeps one: <root>/<slug>/<id>.jsonl, `age`
    seconds since it was last written, begun at a terminal ("cli") unless
    said otherwise ("sdk-cli": headless, as a hippocampus wake is)."""
    sid = sid or str(uuid.uuid4())
    d = root / str(cwd).replace(":", "-").replace("\\", "-").replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    # compact, its type first, as the CLI writes them: the list's glance
    # looks for '"type":"user"' before it parses a line
    p.write_text("".join(json.dumps({**r, "cwd": str(cwd), "sessionId": sid, "entrypoint": entrypoint},
                                    separators=(",", ":")) + "\n"
                         for r in records), encoding="utf-8")
    then = time.time() - age
    os.utime(p, (then, then))
    return sid


A_TALK = [
    rec("user", "hey, can i get your help with some lodestar stuff?"),
    rec("assistant", [{"type": "thinking", "thinking": "hmm"}]),
    rec("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
    rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "file text"}]),
    rec("assistant", [{"type": "text", "text": "The provolone was unattended."}]),
    rec("assistant", [{"type": "text", "text": "Now, Lodestar."}]),
    rec("user", "<command-name>/clear</command-name>"),
    rec("user", "hook output", isMeta=True),
    rec("user", "a subagent's ask", isSidechain=True),
    rec("ai-title", aiTitle="Loadstar stuff"),
    rec("user", [{"type": "text", "text": "<system-reminder>noise</system-reminder>\nthanks!"}]),
    rec("assistant", [{"type": "text", "text": "Any time."}]),
    {"type": "api-request", "requestId": "x"},
    '{"type": "user", "message": {"content": "half a li',          # a line still being written
]


@pytest.fixture
def box(tmp_path):
    root, repo = tmp_path / "projects", tmp_path / "SerenLodestar"
    root.mkdir()
    repo.mkdir()
    return root, repo


def write_raw(root, cwd, records, **kw):
    """write(), but a str record goes in as the line it is (a torn one)."""
    sid = write(root, cwd, [r for r in records if isinstance(r, dict)], **kw)
    p = next(root.glob(f"*/{sid}.jsonl"))
    torn = [r for r in records if isinstance(r, str)]
    if torn:
        with p.open("a", encoding="utf-8") as f:
            f.write("\n".join(torn))
        then = time.time() - kw.get("age", 0.0)
        os.utime(p, (then, then))
    return sid


# ── Reading a transcript ──────────────────────────────────────────────────

def test_a_transcript_brings_the_words_and_leaves_the_workings(box):
    root, repo = box
    sid = write_raw(root, repo, A_TALK)
    t = terminal.transcript(terminal.find(root, sid))

    assert t["id"] == sid and t["cwd"] == str(repo) and t["title"] == "Loadstar stuff"
    assert [(m["role"], m["content"]) for m in t["messages"]] == [
        ("user", "hey, can i get your help with some lodestar stuff?"),
        ("assistant", "The provolone was unattended.\n\nNow, Lodestar."),   # one answer, said in two
        ("user", "thanks!"),
        ("assistant", "Any time."),
    ]


def test_the_harness_own_notices_are_nobodys_words(box):
    """A background task finishing and a subagent's report are written by the
    CLI as the person's turn - as a `user` record, or queued mid-answer like
    something they sent. They are the harness talking to the model. 18 Nov
    2026, the first tangle on 5.7.0: a picked-up chat held eight of them as
    Chad's, and a seat was handed them as "[Chad]" lines."""
    root, repo = box
    note = "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>"
    report = '<agent-message from="a09">\n[Subagent hand-back] set autoMemoryEnabled to false\n</agent-message>'
    sid = write(root, repo, [
        rec("user", "start the test copy"),
        rec("assistant", [{"type": "text", "text": "Starting it."}]),
        rec("user", note),
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": report}},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "and the jello?"}},
        rec("assistant", [{"type": "text", "text": "It is up."}]),
    ])
    t = terminal.transcript(terminal.find(root, sid))
    assert [(m["role"], m["content"]) for m in t["messages"]] == [
        ("user", "start the test copy"), ("assistant", "Starting it."),
        ("user", "and the jello?"), ("assistant", "It is up.")]
    # ...and one a pickup kept before it knew better is not handed on as the person's
    assert terminal.harness_notice({"role": "user", "content": note})
    assert terminal.harness_notice({"role": "user", "content": "  " + report})
    assert not terminal.harness_notice({"role": "user", "content": "what is a <task-notification>?"})
    assert not terminal.harness_notice({"role": "assistant", "content": note})
    assert not terminal.harness_notice({"role": "user", "content": note, "source": "game"})


def test_what_was_said_mid_answer_comes_over_in_its_place(box):
    """A message sent while an answer was under way is kept by the CLI as a
    queued_command attachment, not a user record."""
    root, repo = box
    sid = write(root, repo, [
        rec("user", "scaffold the pickup"),
        rec("assistant", [{"type": "text", "text": "Reading the code."}]),
        {"type": "queue-operation", "operation": "enqueue", "content": "update your magnet"},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "update your magnet"}},
        {"type": "attachment", "attachment": {"type": "edited_text_file", "filename": "x.py"}},
        rec("assistant", [{"type": "text", "text": "Magnet's up."}]),
    ])
    t = terminal.transcript(terminal.find(root, sid))
    assert [(m["role"], m["content"]) for m in t["messages"]] == [
        ("user", "scaffold the pickup"), ("assistant", "Reading the code."),
        ("user", "update your magnet"), ("assistant", "Magnet's up.")]


def test_a_title_the_person_gave_wins_over_the_guessed_one(box):
    root, repo = box
    sid = write(root, repo, [rec("user", "hi"), rec("custom-title", customTitle="the tangle"),
                             rec("ai-title", aiTitle="Casual greeting chat")])
    assert terminal.transcript(terminal.find(root, sid))["title"] == "the tangle"


def test_without_a_title_the_first_thing_said_names_it(box):
    root, repo = box
    sid = write(root, repo, [rec("user", "is   the jello\nset yet")])
    assert terminal.transcript(terminal.find(root, sid))["title"] == "is the jello set yet"


def test_an_id_that_is_not_one_never_becomes_a_path(box):
    root, _ = box
    assert terminal.find(root, "../../etc/passwd") is None
    assert terminal.find(root, "*") is None
    assert terminal.find(root, str(uuid.uuid4())) is None


def test_the_list_is_newest_first_marks_the_warm_and_skips_the_empty(box):
    root, repo = box
    old = write(root, repo, [rec("user", "old")], age=7200)
    new = write(root, repo, [rec("user", "new"), rec("user", "again")], age=5)
    write(root, repo, [rec("ai-title", aiTitle="opened and left")], age=1)

    rows = terminal.listing(root)
    assert [r["id"] for r in rows] == [new, old]
    assert rows[0]["warm"] and not rows[1]["warm"]
    assert rows[0]["said"] == 2
    assert terminal.listing(root, exclude=frozenset({new})) == rows[1:]


def test_no_projects_folder_is_an_empty_list(tmp_path):
    assert terminal.listing(tmp_path / "nope") == []


# ── Picking one up ────────────────────────────────────────────────────────

@pytest.fixture
def runs(tmp_path, monkeypatch):
    log = tmp_path / "claude-runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return lambda: [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


@pytest.fixture
def client(make_client, box):
    root, _ = box
    cfg = SymposiumConfig()
    cfg.claude.command = [sys.executable, FAKE]
    cfg.claude.projects_dir = str(root)
    return make_client(cfg)


def stream(client, body):
    r = client.post("/api/chat/stream", json=body)
    assert r.status_code == 200, r.text
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def test_a_cold_conversation_is_taken_over_where_it_began(client, box, runs):
    root, repo = box
    sid = write_raw(root, repo, A_TALK, age=3600)

    s = client.post("/api/claude/sessions/pickup", json={"id": sid}).json()
    assert s["title"] == "Loadstar stuff" and len(s["messages"]) == 4
    assert s["settings"] == {"voice": "claude", "cwd": str(repo)}
    assert s["claude"] == {"session": sid, "seen": 4}                    # nothing to hand over

    stream(client, {"session": s["id"], "prompt": "where were we"})
    (run,) = runs()
    assert flag(run["argv"], "--resume") == sid and "--fork-session" not in run["argv"]
    assert run["prompt"] == "where were we"                              # it has seen it all
    assert Path(run["cwd"]) == repo                                      # --resume finds it only there
    assert client.get(f"/api/sessions/{s['id']}").json()["claude"] == {"session": sid, "seen": 6}


def test_a_warm_conversation_is_forked_so_the_terminal_keeps_its_own(client, box, runs):
    root, repo = box
    sid = write_raw(root, repo, A_TALK, age=5)

    s = client.post("/api/claude/sessions/pickup", json={"id": sid}).json()
    assert s["claude"]["fork"] is True

    stream(client, {"session": s["id"], "prompt": "one"})
    stream(client, {"session": s["id"], "prompt": "two"})
    first, second = runs()
    assert flag(first["argv"], "--resume") == sid and "--fork-session" in first["argv"]
    forked = client.get(f"/api/sessions/{s['id']}").json()["claude"]
    assert forked["session"] != sid and "fork" not in forked             # its own now
    assert flag(second["argv"], "--resume") == forked["session"] and "--fork-session" not in second["argv"]


def test_the_person_can_say_fork_or_not(client, box):
    root, repo = box
    warm = write(root, repo, [rec("user", "hi")], age=5)
    cold = write(root, repo, [rec("user", "hi")], age=3600)
    assert "fork" not in client.post("/api/claude/sessions/pickup", json={"id": warm, "fork": False}).json()["claude"]
    assert client.post("/api/claude/sessions/pickup", json={"id": cold, "fork": True}).json()["claude"]["fork"]


def test_the_list_leaves_out_what_is_already_a_chat_here(client, box):
    root, repo = box
    a = write(root, repo, [rec("user", "a")], age=60)
    b = write(root, repo, [rec("user", "b")], age=30)
    assert [r["id"] for r in client.get("/api/claude/sessions").json()["sessions"]] == [b, a]

    client.post("/api/claude/sessions/pickup", json={"id": b})
    assert [r["id"] for r in client.get("/api/claude/sessions").json()["sessions"]] == [a]
    r = client.post("/api/claude/sessions/pickup", json={"id": b})
    assert r.status_code == 409


def test_a_chat_loaded_in_by_accident_is_removed_without_deleting_anything(client, box, tmp_path):
    """Chad, 17 Nov 2026: "an ability to remove not just delete a chat, so
    that if one is accidently loaded into symposium ... it can be removed
    without deleting." The chat leaves the window; its file and attachments
    are set aside; the terminal's conversation is untouched and free again."""
    root, repo = box
    talk = write(root, repo, [rec("user", "still mine, in a terminal")], age=3600)
    transcript = next(root.glob(f"*/{talk}.jsonl"))
    before = transcript.read_bytes()
    s = client.post("/api/claude/sessions/pickup", json={"id": talk}).json()
    kept = client.post(f"/api/sessions/{s['id']}/files", json={"name": "note.txt", "data": "aGk="}).json()["name"]

    gone = client.post(f"/api/sessions/{s['id']}/remove", json={})
    assert gone.status_code == 200 and gone.json()["conversations"] == [talk]
    assert client.get(f"/api/sessions/{s['id']}").status_code == 404
    assert s["id"] not in [x["id"] for x in client.get("/api/sessions").json()["sessions"]]

    aside = Path(gone.json()["kept"])
    assert json.loads((aside / f"{s['id']}.json").read_text(encoding="utf-8"))["title"] == s["title"]
    assert (aside / f"{s['id']}.files" / kept).read_bytes() == b"hi"            # what was attached, too
    assert transcript.read_bytes() == before                                    # the conversation: not a byte
    assert [r["id"] for r in client.get("/api/claude/sessions").json()["sessions"]] == [talk]   # to be picked up again
    assert client.post("/api/claude/sessions/pickup", json={"id": talk}).status_code == 201
    assert client.post(f"/api/sessions/{s['id']}/remove", json={}).status_code == 404


def test_a_hippocampus_wake_is_never_listed_nor_picked_up(client, box):
    """Headless runs live where they were started (Chad, 15 Nov 2026)."""
    root, repo = box
    wake = write(root, repo, [rec("user", "You were woken by the hippocampus, not by a person.")],
                 age=30, entrypoint="sdk-cli")
    person = write(root, repo, [rec("user", "hey cute butt")], age=60)
    assert [r["id"] for r in client.get("/api/claude/sessions").json()["sessions"]] == [person]
    r = client.post("/api/claude/sessions/pickup", json={"id": wake})
    assert r.status_code == 403 and "terminal" in r.json()["error"]


def test_a_terminals_conversation_carried_on_headless_is_still_a_terminals(box):
    """How it BEGAN decides: the records a pickup's own turns add say sdk-cli."""
    root, repo = box
    sid = write(root, repo, [rec("user", "hi")])
    p = next(root.glob(f"*/{sid}.jsonl"))
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**rec("user", "from symposium"), "entrypoint": "sdk-cli", "cwd": str(repo)},
                           separators=(",", ":")) + "\n")
    assert terminal.transcript(p)["terminal"]
    (row,) = terminal.listing(root)
    assert row["id"] == sid and row["said"] == 2


def test_the_list_counts_what_the_full_read_counts(box):
    """The list's glance and the pickup's full read agree: same words said,
    same title - mid-answer messages, commands and tool results included."""
    root, repo = box
    sid = write(root, repo, A_TALK[:-1] + [
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "and the jello?"}}])
    full = terminal.transcript(terminal.find(root, sid))
    (row,) = terminal.listing(root)
    assert row["said"] == sum(m["role"] == "user" for m in full["messages"]) == 3
    assert row["title"] == full["title"] == "Loadstar stuff"


def test_a_row_is_read_again_once_its_transcript_grows(box):
    root, repo = box
    sid = write(root, repo, [rec("user", "one")], age=60)
    assert terminal.listing(root)[0]["said"] == 1
    p = next(root.glob(f"*/{sid}.jsonl"))
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**rec("user", "two"), "entrypoint": "cli"}, separators=(",", ":")) + "\n")
    assert terminal.listing(root)[0]["said"] == 2


def test_pickup_refuses_what_is_not_a_conversation_here(client):
    assert client.post("/api/claude/sessions/pickup", json={"id": "../x"}).status_code == 400
    assert client.post("/api/claude/sessions/pickup", json={"id": str(uuid.uuid4())}).status_code == 404


def test_a_chat_whose_folder_is_gone_says_so_rather_than_running_elsewhere(client, box, runs, tmp_path):
    root, _ = box
    gone = tmp_path / "moved-away"
    gone.mkdir()
    sid = write(root, gone, [rec("user", "hi")], age=3600)
    s = client.post("/api/claude/sessions/pickup", json={"id": sid}).json()
    gone.rmdir()

    r = client.post("/api/chat/stream", json={"session": s["id"], "prompt": "still there?"})
    assert r.status_code == 409 and "moved-away" in r.json()["error"]
    assert runs() == []


def test_any_claude_chat_can_be_given_a_folder_to_work_in(client, tmp_path, runs):
    elsewhere = tmp_path / "SerenKitchen"
    elsewhere.mkdir()
    sid = client.post("/api/sessions", json={"settings": {"voice": "claude", "cwd": str(elsewhere)}}).json()["id"]
    stream(client, {"session": sid, "prompt": "hi"})
    assert Path(runs()[0]["cwd"]) == elsewhere
