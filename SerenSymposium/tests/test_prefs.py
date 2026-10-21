"""What the window sets is kept beside the chats, not in the yaml - so the
reinstall that rewrites the yaml cannot put it back. Laid over the yaml at
start; sparse until something is changed; projects taken over on the first
change to one."""
from __future__ import annotations

import json

from seren_symposium.config import FolderPreset, SymposiumConfig


def config(tmp_path, **ui) -> SymposiumConfig:
    cfg = SymposiumConfig()
    cfg.sessions.dir = str(tmp_path / "sessions")
    cfg.claude.name = "wren"                                       # as Starwright wrote it
    cfg.folders = [FolderPreset(name="Main", voice="claude"), FolderPreset(name="music", temperature=0.9)]
    for k, v in ui.items():
        setattr(cfg.ui, k, v)
    return cfg


def names(tc):
    return [f["name"] for f in tc.get("/api/config").json()["folders"]]


def test_the_yaml_shows_through_until_the_window_changes_it(make_client, tmp_path):
    tc = make_client(config(tmp_path))
    c = tc.get("/api/config").json()
    assert (c["you"], c["claude"]["name"]) == ("you", "wren")
    assert not (tmp_path / "symposium-prefs.json").exists(), "nothing changed, nothing written"

    r = tc.patch("/api/prefs", json={"you": " Alex ", "claude_name": "Sam"})
    assert r.json() == {"you": "Alex", "claude_name": "Sam", "confirm_destructive": True}
    assert json.loads((tmp_path / "symposium-prefs.json").read_text(encoding="utf-8")) == \
        {"you": "Alex", "claude_name": "Sam"}


def test_a_reinstall_does_not_undo_what_the_window_set(make_client, tmp_path):
    make_client(config(tmp_path)).patch("/api/prefs", json={"claude_name": "Sam", "confirm_destructive": False})
    # The reinstall: a fresh yaml, the old name in it, the app started again.
    c = make_client(config(tmp_path)).get("/api/config").json()
    assert c["claude"]["name"] == "Sam" and c["confirm_destructive"] is False


def test_bad_app_settings_are_refused(make_client, tmp_path):
    tc = make_client(config(tmp_path))
    assert tc.patch("/api/prefs", json={"you": "  "}).status_code == 400
    assert tc.patch("/api/prefs", json={"lodestar": "http://elsewhere"}).status_code == 400
    assert tc.patch("/api/prefs", json={"confirm_destructive": "no"}).status_code == 400
    assert tc.patch("/api/prefs", json={}).status_code == 400


def test_a_new_project_is_kept_and_the_yaml_ones_come_along(make_client, tmp_path):
    tc = make_client(config(tmp_path))
    r = tc.post("/api/projects", json={"name": "art", "voice": "claude", "temperature": 0.7})
    assert r.status_code == 201 and r.json()["temperature"] == 0.7
    assert names(tc) == ["Main", "music", "art"]
    # restarted on a yaml that knows nothing of art
    assert names(make_client(config(tmp_path))) == ["Main", "music", "art"]
    assert tc.post("/api/projects", json={"name": "art"}).status_code == 400, "already there"
    assert tc.post("/api/projects", json={"name": " "}).status_code == 400
    assert tc.post("/api/projects", json={"name": "x", "voice": "gpt"}).status_code == 400
    assert tc.post("/api/projects", json={"name": "x", "colour": "red"}).status_code == 400


def test_a_yaml_project_renamed_stays_renamed_and_takes_its_chats(make_client, tmp_path):
    tc = make_client(config(tmp_path))
    sid = tc.post("/api/sessions", json={"folder": "music"}).json()["id"]
    r = tc.patch("/api/projects", json={"name": "music", "rename": "songs"})
    assert r.json()["moved"] == 1 and r.json()["temperature"] == 0.9, "its settings go with it"
    assert tc.get(f"/api/sessions/{sid}").json()["folder"] == "songs"
    assert names(make_client(config(tmp_path))) == ["Main", "songs"], "music does not come back"


def test_a_project_renamed_onto_another_merges_into_it(make_client, tmp_path):
    tc = make_client(config(tmp_path))
    sid = tc.post("/api/sessions", json={"folder": "music"}).json()["id"]
    r = tc.patch("/api/projects", json={"name": "music", "rename": "Main"})
    assert r.json()["voice"] == "claude", "the one it went into keeps its settings"
    assert names(tc) == ["Main"]
    assert tc.get(f"/api/sessions/{sid}").json()["folder"] == "Main"


def test_a_projects_settings_change_and_drop_back(make_client, tmp_path):
    tc = make_client(config(tmp_path))
    tc.patch("/api/projects", json={"name": "music", "set": {"system_prompt": "Be brief.", "max_tokens": 256}})
    tc.patch("/api/projects", json={"name": "music", "set": {"temperature": None}})
    music = next(f for f in tc.get("/api/config").json()["folders"] if f["name"] == "music")
    assert (music["system_prompt"], music["max_tokens"], music["temperature"]) == ("Be brief.", 256, None)
    assert tc.patch("/api/projects", json={"name": "music", "set": {"temperature": "hot"}}).status_code == 400


def test_a_project_removed_leaves_its_chats_loose(make_client, tmp_path):
    tc = make_client(config(tmp_path))
    sid = tc.post("/api/sessions", json={"folder": "music"}).json()["id"]
    assert tc.post("/api/projects/remove", json={"name": "music"}).json() == {"removed": "music", "moved": 1}
    assert tc.get(f"/api/sessions/{sid}").json()["folder"] == ""
    assert names(tc) == ["Main"]
    assert tc.post("/api/projects/remove", json={"name": "music"}).status_code == 404


def test_a_broken_prefs_file_is_left_alone_and_the_yaml_stands(make_client, tmp_path):
    (tmp_path / "symposium-prefs.json").write_text("{not json", encoding="utf-8")
    c = make_client(config(tmp_path)).get("/api/config").json()
    assert c["claude"]["name"] == "wren" and [f["name"] for f in c["folders"]] == ["Main", "music"]
    assert (tmp_path / "symposium-prefs.json").read_text(encoding="utf-8") == "{not json"
