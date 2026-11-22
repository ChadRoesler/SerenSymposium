"""Putting a chat away without losing it, and finding anything said in any
chat. 21 Nov 2026, Chad: "archival, or just hiding from main project area"
and "Search" - Remove sends a chat where nothing looks again, and the only
search box there was covered one chat's files."""
from __future__ import annotations


def chat(tc, folder="den", title="", messages=()):
    body = {"folder": folder, "title": title,
            "messages": [{"role": r, "content": c} for r, c in messages]}
    return tc.post("/api/sessions", json=body).json()["id"]


# ── Archive ───────────────────────────────────────────────────────────────

def test_an_archived_chat_stays_listed_and_flagged_and_comes_back(client):
    sid = chat(client, title="old bench")
    before = client.get(f"/api/sessions/{sid}").json()["updated"]
    r = client.patch(f"/api/sessions/{sid}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"]
    listed = {s["id"]: s for s in client.get("/api/sessions").json()["sessions"]}
    assert listed[sid]["archived"]                       # the window folds it, the server keeps it
    assert r.json()["updated"] == before                 # putting it away is not something said in it
    r = client.patch(f"/api/sessions/{sid}", json={"archived": False})
    assert "archived" not in r.json()
    assert client.get("/api/sessions").json()["sessions"][0]["archived"] is None


def test_archived_takes_only_true_or_false(client):
    sid = chat(client)
    assert client.patch(f"/api/sessions/{sid}", json={"archived": "yes"}).status_code == 400


def test_talking_in_an_archived_chat_brings_it_back(client):
    store = client.app.state.sessions
    sid = chat(client)
    client.patch(f"/api/sessions/{sid}", json={"archived": True})
    store.record(sid, "you still there?", "always", {})
    assert "archived" not in store.get(sid)


def test_a_late_answer_in_an_archived_chat_brings_it_back(client):
    """A build left running in the background finishes after the chat was
    put away: someone spoke in it, so it is back where it can be seen."""
    store = client.app.state.sessions
    sid = chat(client)
    client.patch(f"/api/sessions/{sid}", json={"archived": True})
    store.record_woken(sid, None, "the flash finished", {}, "conv-1")
    assert "archived" not in store.get(sid)


# ── Search ────────────────────────────────────────────────────────────────

def find(tc, q):
    return tc.get("/api/sessions/search", params={"q": q}).json()["sessions"]


def test_search_finds_a_message_and_says_where(client):
    sid = chat(client, title="soma", messages=[("user", "the proto boards are late"),
                                               ("assistant", "measure the STACK height first")])
    chat(client, title="other", messages=[("user", "nothing to see")])
    hits = find(client, "stack height")
    assert [h["id"] for h in hits] == [sid]
    (m,) = hits[0]["matches"]
    assert m["index"] == 1 and m["role"] == "assistant" and "STACK height" in m["snippet"]


def test_every_word_must_turn_up_in_one_message(client):
    chat(client, messages=[("user", "proto boards"), ("assistant", "stack height")])
    assert find(client, "proto height") == []


def test_title_hits_come_first(client):
    body = chat(client, title="nothing", messages=[("user", "a wren on the nightstand")])
    named = chat(client, title="The Rokeby Wren")
    assert [h["id"] for h in find(client, "wren")] == [named, body]
    assert find(client, "wren")[0]["title_hit"]


def test_search_reaches_archived_chats(client):
    sid = chat(client, messages=[("user", "the bedsheet as a robe")])
    client.patch(f"/api/sessions/{sid}", json={"archived": True})
    (hit,) = find(client, "bedsheet")
    assert hit["id"] == sid and hit["archived"]


def test_many_hits_in_one_chat_are_counted_but_few_shown(client):
    chat(client, messages=[("user", f"glasses {n}") for n in range(7)])
    (hit,) = find(client, "glasses")
    assert hit["hits"] == 7 and len(hit["matches"]) == 3
    assert hit["matches"][0]["index"] == 6               # newest first


def test_a_long_message_is_cut_around_the_hit(client):
    chat(client, messages=[("user", "x " * 500 + "needle" + " y" * 500)])
    (m,) = find(client, "needle")[0]["matches"]
    assert "needle" in m["snippet"] and m["snippet"].startswith("…") and m["snippet"].endswith("…")
    assert len(m["snippet"]) < 120


def test_the_snippet_is_cut_where_the_words_are_together(client):
    """21 Nov 2026, on the real chats: "reading glasses" was cut at
    "Symposium reading it", paragraphs away from any glasses."""
    chat(client, messages=[("assistant", "Symposium reading it. " + "filler " * 80
                            + "the only thing I'm wearing is the reading glasses")])
    (m,) = find(client, "reading glasses")[0]["matches"]
    assert "reading glasses" in m["snippet"]


def test_an_empty_search_finds_nothing(client):
    chat(client, messages=[("user", "anything")])
    assert find(client, "   ") == []


def test_search_is_not_mistaken_for_a_chat_id(client):
    assert client.get("/api/sessions/search").status_code == 200


# ── Pictures: full, strip or folded ───────────────────────────────────────
# Same layers as interrupting: every chat's (prefs), then the chat's own.

def test_every_chats_picture_layout_is_strip_until_set(client):
    assert client.get("/api/config").json()["pictures"] == "strip"
    assert client.patch("/api/prefs", json={"pictures": "folded"}).status_code == 200
    assert client.get("/api/config").json()["pictures"] == "folded"
    assert client.patch("/api/prefs", json={"pictures": "wall"}).status_code == 400


def test_a_chat_can_lay_its_pictures_out_its_own_way(client):
    sid = chat(client)
    r = client.patch(f"/api/sessions/{sid}", json={"settings": {"pictures": "full"}})
    assert r.json()["settings"]["pictures"] == "full"
    assert client.patch(f"/api/sessions/{sid}", json={"settings": {"pictures": "wall"}}).status_code == 400
    r = client.patch(f"/api/sessions/{sid}", json={"settings": {"pictures": None}})
    assert "pictures" not in r.json()["settings"]                 # back to every chat's
