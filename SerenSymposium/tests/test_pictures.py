"""
The picture panel's back half (seren_symposium.pictures, 15 Oct 2026).

Pinned here:
- options: Lodestar's catalog shaped for the panel (drawable models first,
  each with how it wants to be prompted); ?model= relays the LoRAs that fit
- translate: a local model through Lodestar's chat, told the style guide;
  JSON in the answer becomes {prompt, negative}, prose becomes the prompt
- plan: relayed, with Lodestar's own refusal when nothing can draw it
- draw: Lodestar draws, the pictures are KEPT - files beside the chat, a
  turn recorded (the request as what you said, the picture as the answer,
  a receipt of model / node / seed), shown at full size by the chat's own
  files route; without a session nothing is kept
- the gallery is relayed, the file streamed, never a path on another box
- what made a picture is kept on its file entry (`drawn`: prompt, negative,
  model, LoRAs and weights, steps / cfg / sampler, seed, size) and answered
  by .../files/{name}/settings; a picture from before that says so plainly
  (kept "receipt" or "none"), never an error (24 Oct 2026)
"""
from __future__ import annotations

import base64
import json

import httpx

from seren_symposium.pictures import drawn_settings, model_name, parse_translation, picture_name, shape_options, slug

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 60


def _lodestar(seen: list):
    """A Lodestar that catalogs, translates, plans, draws and has a gallery."""
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.url.params), req.headers.get("authorization")))
        p = req.url.path
        if p == "/api/v1/comfy/catalog":
            return httpx.Response(200, json={"files": {
                "checkpoints/krea2Turbo.safetensors": {"role": "checkpoint", "name": "krea2Turbo.safetensors", "arch": "krea2",
                                                       "kind": "image", "drawable_on": ["node-c"], "nodes": ["node-c"],
                                                       "style": {"style": "prose", "guide": "Sentences: describe the scene."}},
                "checkpoints/sdxl.safetensors": {"role": "checkpoint", "name": "sdxl.safetensors", "arch": "sdxl", "base": "Pony",
                                                 "kind": "image", "drawable_on": ["node-a", "node-c"], "style": {"style": "booru", "guide": "Booru tags."}},
                "diffusion_models/wan.safetensors": {"role": "diffusion", "name": "wan.safetensors", "arch": "wan", "kind": "video", "drawable_on": []},
                "loras/x.safetensors": {"role": "lora", "name": "x.safetensors", "arch": "sdxl"},
                "checkpoints/broken.safetensors": {"role": "checkpoint", "name": "broken", "error": "unreadable"}},
                "nodes": [{"node": "node-a", "families": ["sdxl", "sd15"], "summary": "a jetson"},
                          {"node": "node-c", "families": [], "summary": "a spark"}]})
        if p == "/api/v1/comfy/compatible":
            return httpx.Response(200, json={"model": req.url.params["model"], "loras": [{"name": "x.safetensors", "fit": "good", "weight": 0.7}]})
        if p == "/api/v1/chat":
            body = json.loads(req.content)
            assert "Sentences: describe the scene." in body["system_prompt"], "the style guide is the instruction"
            return httpx.Response(200, json={"response": 'Here you go: {"prompt": "a houseboat at dawn, mist on the river", "negative": "people"}',
                                             "model": "qwen", "node": "node-a"})
        if p == "/api/v1/image/plan":
            body = json.loads(req.content)
            if body.get("model") == "wan.safetensors":
                return httpx.Response(400, json={"error": "no image node draws wan", "nodes": ["node-a: does not draw wan"]})
            return httpx.Response(200, json={"ok": True, "node": "node-c", "also": [], "arch": "krea2",
                                             "model": {"name": body.get("model") or "krea2Turbo.safetensors"}, "prompt": body["prompt"],
                                             "negative": "people, " + body.get("negative", ""), "width": 1024, "height": 1024,
                                             "seed": 7, "loras": body.get("loras") or [], "settings": {"steps": 8, "cfg": 1.0, "sampler": "euler"},
                                             "notes": ["the model's profile chose the LoRAs"], "style": {"style": "prose", "guide": "..."},
                                             "workflow": {"1": {}}})
        if p == "/api/v1/image":
            body = json.loads(req.content)
            assert body["holder"] == "symposium"
            return httpx.Response(200, json={"ok": True, "node": "node-c", "model": {"name": "krea2Turbo.safetensors"}, "arch": "krea2",
                                             "prompt": body["prompt"], "negative": "people", "seed": 42, "width": 1024, "height": 1024,
                                             "loras": [dict(l, fit="good") for l in body.get("loras") or []], "seconds": 31.5, "notes": [],
                                             "settings": {"steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple"},
                                             "images": [{"filename": "7_00001_.png", "mime": "image/png", "data": base64.b64encode(PNG).decode()}]})
        if p == "/api/v1/gallery":
            return httpx.Response(200, json={"nodes": [{"node": "node-a", "service": "seren-workbench-x", "days": [{"day": "2026-10-15", "pictures": 2}], "count": 2}], "count": 2})
        if p == "/api/v1/gallery/node-a/2026-10-15":
            return httpx.Response(200, json={"node": "node-a", "day": "2026-10-15", "count": 1, "pictures": [{"file": "a.png", "bytes": 68, "mime": "image/png", "note": {"why": "x"}}]})
        if p == "/api/v1/gallery/node-a/2026-10-15/a.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404, json={"error": "not found", "detail": p})
    return handler


def _wire(client, seen):
    client.app.state.client = httpx.AsyncClient(base_url="http://lodestar", transport=httpx.MockTransport(_lodestar(seen)),
                                                headers={"Authorization": "Bearer head-secret"})


# ── the pieces ──────────────────────────────────────────────────────────────

def test_the_catalog_is_shaped_for_the_panel():
    got = shape_options({"files": {
        "a": {"role": "checkpoint", "name": "video.safetensors", "arch": "wan", "kind": "video", "drawable_on": ["n"]},
        "b": {"role": "checkpoint", "name": "nope.safetensors", "arch": "sdxl", "kind": "image", "drawable_on": []},
        "c": {"role": "checkpoint", "name": "yes.safetensors", "arch": "sdxl", "kind": "image", "drawable_on": ["n"], "style": {"style": "sdxl", "guide": "g"}},
        "d": {"role": "lora", "name": "l.safetensors"}, "e": {"role": "checkpoint", "name": "bad", "error": "x"}},
        "nodes": [{"node": "n", "families": ["sdxl"], "summary": "s"}]})
    assert [m["name"] for m in got["models"]] == ["yes.safetensors", "video.safetensors", "nope.safetensors"], "drawable first, pictures before video"
    assert got["models"][0]["style"] == {"style": "sdxl", "guide": "g"} and got["nodes"][0]["node"] == "n"


def test_a_translation_is_the_json_in_the_answer_or_the_whole_answer():
    assert parse_translation('Sure! {"prompt": "a cat", "negative": "dogs"} ok?') == {"prompt": "a cat", "negative": "dogs"}
    assert parse_translation("a cat on a mat") == {"prompt": "a cat on a mat", "negative": ""}
    assert parse_translation('{"not": "a prompt"} a cat') == {"prompt": '{"not": "a prompt"} a cat', "negative": ""}
    assert slug("The Lantern, reaching!") == "the-lantern-reaching" and slug("") == "picture"
    assert picture_name(7, "a houseboat", 0, 1, "image/png").endswith("-7-a-houseboat.png")
    assert picture_name(7, "a houseboat", 1, 2, "image/jpeg").endswith("-7-a-houseboat-2.jpg")


# ── through the app ─────────────────────────────────────────────────────────

def test_options_translate_and_plan_go_through_lodestar(client):
    seen = []
    _wire(client, seen)
    opts = client.get("/api/pictures/options").json()
    assert [m["name"] for m in opts["models"]] == ["krea2Turbo.safetensors", "sdxl.safetensors", "wan.safetensors"]
    assert opts["models"][0]["style"]["style"] == "prose" and [n["node"] for n in opts["nodes"]] == ["node-a", "node-c"]
    assert seen[-1][3] == "Bearer head-secret", "Lodestar's bearer, attached by the client"
    loras = client.get("/api/pictures/options", params={"model": "sdxl.safetensors"}).json()
    assert loras["loras"][0]["name"] == "x.safetensors"
    t = client.post("/api/pictures/translate", json={"text": "a houseboat at dawn", "guide": "Sentences: describe the scene."}).json()
    assert t == {"ok": True, "prompt": "a houseboat at dawn, mist on the river", "negative": "people", "model": "qwen", "node": "node-a",
                 "voice": "lodestar"}
    assert client.post("/api/pictures/translate", json={"text": "  "}).status_code == 400
    p = client.post("/api/pictures/plan", json={"prompt": "a houseboat", "model": "krea2Turbo.safetensors"}).json()
    assert p["node"] == "node-c" and p["seed"] == 7 and p["workflow"]
    r = client.post("/api/pictures/plan", json={"prompt": "x", "model": "wan.safetensors"})
    assert r.status_code == 400 and "wan" in r.json()["error"]


def test_a_drawing_is_kept_in_the_chat_as_a_turn_and_a_file(client):
    seen = []
    _wire(client, seen)
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    r = client.post("/api/pictures/draw", json={"session": sid, "asked": "a houseboat at dawn", "why": "the houseboat song",
                                                "request": {"prompt": "a houseboat at dawn, mist", "model": "krea2Turbo.safetensors"}})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["ok"] and got["session"] == sid and got["seed"] == 42 and "images" not in got, "the bytes stay in the chat's files"
    (f,) = got["files"]
    assert f["orig"].startswith("picture-") and f["orig"].endswith("-42-the-houseboat-song.png") and f["type"] == "image/png"
    s = client.get(f"/api/sessions/{sid}").json()
    asked, said = s["messages"]
    assert asked["content"] == "a houseboat at dawn" and said["content"].startswith(f"![the houseboat song](files/{f['name']})")
    assert said["meta"]["voice"] == "picture" and said["meta"]["node"] == "node-c" and said["meta"]["seed"] == 42
    assert said["files"][0]["name"] == f["name"]
    shown = client.get(f"/api/sessions/{sid}/files/{f['name']}")
    assert shown.status_code == 200 and shown.content == PNG and shown.headers["content-type"] == "image/png"
    assert s["title"] == "a houseboat at dawn", "a chat that began with a picture is named for the request"
    assert client.post("/api/pictures/draw", json={"session": "nope", "request": {"prompt": "x"}}).status_code == 404
    assert client.post("/api/pictures/draw", json={"request": "x"}).status_code == 400
    loose = client.post("/api/pictures/draw", json={"request": {"prompt": "x"}}).json()
    assert loose["ok"] and loose["images"][0]["data"], "no session: the picture comes back and is kept nowhere"


def test_what_made_a_picture_is_kept_with_it_and_answered(client):
    """Click a picture, see what made it: the prompt and negative as drawn,
    the model, each LoRA with its weight, steps / cfg / sampler, seed, size -
    on the picture's own file entry, and through the settings route."""
    seen = []
    _wire(client, seen)
    sid = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    req = {"prompt": "a houseboat at dawn, mist", "negative": "text", "model": "krea2Turbo.safetensors",
           "loras": [{"name": "x.safetensors", "weight": 0.7}], "width": 1024, "height": 1024}
    got = client.post("/api/pictures/draw", json={"session": sid, "asked": "a houseboat", "request": req}).json()
    (f,) = got["files"]
    drawn = f["drawn"]
    assert drawn["prompt"] == "a houseboat at dawn, mist" and drawn["negative"] == "people", "what Lodestar drew, its negative"
    assert drawn["model"] == "krea2Turbo.safetensors" and drawn["arch"] == "krea2" and drawn["node"] == "node-c"
    assert drawn["loras"] == [{"name": "x.safetensors", "weight": 0.7}]
    assert drawn["settings"] == {"steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple"}
    assert drawn["seed"] == 42 and (drawn["width"], drawn["height"]) == (1024, 1024)
    assert drawn["request"] == req, "the request as asked, without the holder Symposium added"
    kept = client.get(f"/api/sessions/{sid}").json()["messages"][1]
    assert kept["files"][0]["drawn"] == drawn, "kept in the chat, not only answered"
    assert kept["meta"]["settings"]["steps"] == 8, "the turn's receipt has the steps too"
    made = client.get(f"/api/sessions/{sid}/files/{f['name']}/settings").json()
    assert made == {"name": f["name"], "kept": "all", "drawn": drawn, "note": ""}
    # a fork takes the picture and what made it
    fork = client.post(f"/api/sessions/{sid}/fork", json={"at": 2}).json()["id"]
    assert client.get(f"/api/sessions/{fork}/files/{f['name']}/settings").json()["kept"] == "all"
    assert client.get(f"/api/sessions/{sid}/files/nope.png/settings").status_code == 404
    assert client.get("/api/sessions/nope/files/x.png/settings").status_code == 404


def test_a_picture_from_before_says_so_plainly(client):
    """Pictures drawn before `drawn` was kept: what their turn's receipt has,
    and a note that steps, cfg and the sampler were not kept. An attached
    picture has nothing: kept "none", said plainly - 200, not an error."""
    old = [{"role": "user", "content": "a houseboat"},
           {"role": "assistant", "content": "![a houseboat](files/0a1b2c3d-picture-1.png)",
            "meta": {"voice": "picture", "model": {"name": "krea2Turbo.safetensors"}, "node": "node-c", "seed": 7,
                     "prompt": "a houseboat, mist", "negative": "people", "loras": [], "width": 1024, "height": 1024},
            "files": [{"name": "0a1b2c3d-picture-1.png", "orig": "picture-1.png", "type": "image/png", "size": 68}]},
           {"role": "user", "content": "look at this", "files": [{"name": "4e5f6a7b-cat.png", "orig": "cat.png",
                                                                   "type": "image/png", "size": 68}]},
           {"role": "assistant", "content": "a cat"}]
    sid = client.post("/api/sessions", json={"folder": "den", "messages": old}).json()["id"]
    made = client.get(f"/api/sessions/{sid}/files/0a1b2c3d-picture-1.png/settings")
    assert made.status_code == 200
    made = made.json()
    assert made["kept"] == "receipt" and "not kept" in made["note"]
    assert made["drawn"] == {"model": "krea2Turbo.safetensors", "node": "node-c", "seed": 7, "prompt": "a houseboat, mist",
                             "negative": "people", "loras": [], "width": 1024, "height": 1024}
    cat = client.get(f"/api/sessions/{sid}/files/4e5f6a7b-cat.png/settings")
    assert cat.status_code == 200 and cat.json()["kept"] == "none" and cat.json()["drawn"] is None
    assert "no settings were kept" in cat.json()["note"]


def test_an_edited_plan_s_drawing_keeps_what_the_request_said():
    """A plan's workflow, edited and drawn, comes back with no description:
    the request fills in, the workflow itself is not kept (it is big, and
    flagged instead); a model named by catalog key is kept by file name."""
    drawn = drawn_settings({"ok": True, "node": "node-c", "seconds": 12, "images": []},
                           {"prompt": "a lighthouse", "seed": 3, "workflow": {"1": {"inputs": {}}}, "node": "node-c",
                            "holder": "symposium", "save_as": "symposium"})
    assert drawn["prompt"] == "a lighthouse" and drawn["seed"] == 3 and drawn["from_workflow"] is True
    assert "workflow" not in drawn["request"] and "holder" not in drawn["request"]
    assert drawn["loras"] == [] and drawn["settings"] == {}
    assert model_name("checkpoints/sdxl/pony.safetensors") == "pony.safetensors"
    assert model_name({"name": "krea2Turbo.safetensors"}) == "krea2Turbo.safetensors" and model_name(None) is None


def test_translate_asks_whoever_answers_the_chat(make_client, tmp_path, monkeypatch):
    """A Claude chat's translate goes to Claude (one shot, no session
    resumed, the style guide as its instructions); a local chat's goes to
    its own model through Lodestar, routed by model_override."""
    import sys
    from pathlib import Path
    from seren_symposium.config import FolderPreset, SymposiumConfig
    fake = str(Path(__file__).with_name("fake_claude.py"))
    log = tmp_path / "claude-runs.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", 'Here: {"prompt": "A small bird on a sill at dusk, lamplight behind.", "negative": "people"}')
    cfg = SymposiumConfig()
    cfg.claude.command = [sys.executable, fake]
    cfg.folders = [FolderPreset(name="den", voice="claude"), FolderPreset(name="lab", model="qwen-local.gguf")]
    client = make_client(cfg)
    seen = []
    _wire(client, seen)
    den = client.post("/api/sessions", json={"folder": "den"}).json()["id"]
    t = client.post("/api/pictures/translate", json={"text": "a bird on a windowsill", "guide": "Sentences: describe the scene.",
                                                     "session": den}).json()
    assert t["ok"] and t["voice"] == "claude" and t["prompt"].startswith("A small bird") and t["negative"] == "people"
    (run,) = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert run["prompt"] == "a bird on a windowsill" and "--resume" not in run["argv"], "one shot, nothing resumed"
    system = run["argv"][run["argv"].index("--append-system-prompt") + 1]
    assert "Sentences: describe the scene." in system and "Use no tools" in system
    assert not any(p == "/api/v1/chat" for _, p, _, _ in seen), "Lodestar's chat was not asked"
    # the lab: a local chat, its own model named for Lodestar to route
    lab = client.post("/api/sessions", json={"folder": "lab"}).json()["id"]
    t = client.post("/api/pictures/translate", json={"text": "a bird on a windowsill", "guide": "Sentences: describe the scene.",
                                                     "session": lab}).json()
    assert t["ok"] and t["voice"] == "lodestar" and t["model"] == "qwen"
    assert any(p == "/api/v1/chat" for _, p, _, _ in seen)
    # no session: the folder's voice; no folder: a local model
    t = client.post("/api/pictures/translate", json={"text": "x", "guide": "Sentences: describe the scene.", "folder": "den"}).json()
    assert t["voice"] == "claude"


def test_the_gallery_is_relayed_and_the_picture_streamed(client):
    seen = []
    _wire(client, seen)
    assert client.get("/api/pictures/gallery").json()["count"] == 2
    assert client.get("/api/pictures/gallery/node-a/2026-10-15").json()["pictures"][0]["file"] == "a.png"
    r = client.get("/api/pictures/gallery/node-a/2026-10-15/a.png")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/pictures/gallery/node-a/2026-10-15/b.png").status_code == 404


def test_lodestar_down_is_said_plainly(client):
    r = client.get("/api/pictures/options")
    assert r.status_code == 502 and "lodestar" in r.text.lower()
    assert client.post("/api/pictures/translate", json={"text": "x"}).status_code == 502


def test_every_panel_element_the_script_reaches_for_is_in_the_page():
    """picEl("x") in the script is #pic-x in the page. A name with no
    element throws before the request is sent and the panel just sits
    there: the translate button once reached for a `go` that was never
    drawn."""
    import re
    from pathlib import Path
    ui = Path(__file__).resolve().parents[1] / "seren_symposium" / "ui"
    script = (ui / "scripts.js").read_text(encoding="utf-8")
    page = (ui / "body.html").read_text(encoding="utf-8")
    reached = set(re.findall(r'picEl\("([a-z-]+)"\)', script))
    assert reached, "the panel's script reaches for its elements through picEl"
    missing = sorted(x for x in reached if f'id="pic-{x}"' not in page)
    assert missing == [], f"the script reaches for pic-{', pic-'.join(missing)} but the page has no such element"
