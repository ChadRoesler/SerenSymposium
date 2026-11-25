"""
seren_symposium.pictures
════════════════════════════════════════════════════════════════════════

The picture panel's back half: a request in words becomes a prompt the
chosen model reads, a plan says what would be drawn and where, the drawing
comes back into the chat and is kept with it.

    options     what the cluster can draw: the models (with how each wants
                to be prompted - Lodestar's STYLES guide), the LoRAs that fit
                one, the nodes
    translate   words -> {prompt, negative} in the model's style, written by
                a local model through Lodestar's chat, with the style guide
                as its instructions. A person can edit the result; it is a
                draft, never the last word.
    plan        Lodestar's /api/v1/image/plan, relayed: node, model, the
                prompt as the node will see it, the workflow, the notes
    draw        Lodestar's /api/v1/image, then the pictures are KEPT: saved
                into the chat's files and recorded as a turn - the request
                as what you said, the picture as the answer - so the chat
                shows it at full size now and when the gallery is tidied.
                Each picture's file entry carries `drawn`: what made it
    made        what made one picture in a chat (made_of), for the viewer
    gallery     every node's gallery through Lodestar, for the gallery tab

Symposium does not plan or draw anything itself; Lodestar owns the catalog,
the templates and the nodes. What is Symposium's is the keeping. 15 Oct 2026.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
from typing import Any, Optional

import httpx

from .sessions import SessionStore

DRAW_TIMEOUT = 900.0
TRANSLATE_SYSTEM = (
    "You turn a request for a picture into a prompt for an image model. Write for THIS model: {guide}\n"
    "Answer with JSON only, no prose around it: {{\"prompt\": \"...\", \"negative\": \"...\"}}. "
    "The prompt says what to draw in the model's style; the negative lists what to keep out, or is empty. "
    "Keep what the request asked for; add only what the style needs (composition, light, medium). "
    "Never add people the request did not ask for."
)
# `chat`: the translator is handed the chat's recent messages with the request.
CONTEXT_NOTE = (
    " The request comes out of a conversation, given with it for context only. Use the conversation to work out "
    "who and what the request means (\"us\", \"that\", a name, the place, what is going on) and how those people "
    "and things have been described; where it does not say what someone looks like, do not invent a likeness. "
    "Draw only the picture asked for, not the conversation."
)
# `fork`: asked of a side copy of the chat's own conversation, which was there.
FORK_ASK = (
    "(From the picture panel, not a message from {you}. You are a side copy of this conversation, made for this one "
    "question: nothing you say here is shown in the chat or remembered, and the conversation carries on without it.)\n\n"
    "Turn this request for a picture into a prompt for an image model, using what you know from this conversation "
    "about who and what it means and what they look like:\n\n{text}\n\n"
    "Write for THIS model: {guide}\n"
    "Answer with JSON only, no prose around it: {{\"prompt\": \"...\", \"negative\": \"...\"}}. "
    "The prompt says what to draw in the model's style; the negative lists what to keep out, or is empty. "
    "Keep what the request asked for; add only what the style needs. Use no tools: write it from what you already know."
)
_JSON = re.compile(r"\{.*\}", re.S)
_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str, n: int = 40) -> str:
    s = _SLUG.sub("-", (text or "").lower()).strip("-")
    return s[:n].rstrip("-") or "picture"


def shape_options(catalog: dict[str, Any]) -> dict[str, Any]:
    """Lodestar's catalog as the panel wants it: the models someone can
    draw with (drawable on some node), each with its family, base and how
    it wants to be prompted; and the nodes."""
    models = []
    for key, f in (catalog.get("files") or {}).items():
        if f.get("role") not in ("checkpoint", "diffusion") or f.get("error"):
            continue
        models.append({"key": key, "name": f.get("name") or key, "family": f.get("arch"), "base": f.get("base"),
                       "kind": f.get("kind"), "drawable_on": f.get("drawable_on") or [], "nodes": f.get("nodes") or [],
                       "style": f.get("style") or {}})
    models.sort(key=lambda m: (not m["drawable_on"], m["kind"] != "image", m["family"] or "", m["name"]))
    return {"models": models,
            "nodes": [{"node": n.get("node"), "families": n.get("families") or "all", "summary": n.get("summary"),
                       "profiles": n.get("profiles") or {}} for n in catalog.get("nodes") or []]}


def parse_translation(text: str) -> dict[str, str]:
    """The model's answer as {prompt, negative}: the JSON in it, or the
    whole thing as the prompt when it did not manage JSON."""
    m = _JSON.search(text or "")
    if m:
        try:
            got = json.loads(m.group(0))
            if isinstance(got, dict) and got.get("prompt"):
                return {"prompt": str(got["prompt"]).strip(), "negative": str(got.get("negative") or "").strip()}
        except ValueError:
            pass
    return {"prompt": (text or "").strip(), "negative": ""}


def chat_context(messages: list[dict[str, Any]], you: str, voice: str, limit: int, max_chars: int,
                 viewer: Optional[str] = None) -> str:
    """The chat's latest messages as text a translator can read: who said
    what, newest last, at most `limit` of them and `max_chars` in all. A long
    message keeps its END (where an answer lands on what it means), a seat's
    answer is its last piece, and a stopped or empty one is left out. So is
    what was kept for one seat alone (`only_for`), unless `viewer` is that seat."""
    lines: list[str] = []
    room = max(0, max_chars)
    for m in reversed(messages[-max(0, limit):] if limit > 0 else []):
        if m.get("only_for") and m.get("only_for") != viewer:
            continue
        words = str(m.get("final") or m.get("content") or "").strip()
        if not words or room <= 0:
            continue
        who = you if m.get("role") == "user" else (m.get("seat") or voice)
        if m.get("source"):                          # fed in by something else (the game table, say): never the person's
            who = str(m.get("by") or m["source"])
        each = min(room, 1500)
        if len(words) > each:
            if each < 200:                           # no room left for more than a scrap of it: stop here
                break
            words = "…" + words[-each:]
        lines.append(f"{who}: {words}")
        room -= len(words)
    return "\n\n".join(reversed(lines))


async def translate_fork(claude: Any, turn: dict[str, Any], text: str, guide: str, you: str) -> dict[str, Any]:
    """Words -> a prompt, drafted by a throwaway copy of the chat's own Claude
    conversation (claude_voice.side_copy): it knows who "us" is and what has
    been going on, because it was there. `turn`: {resume, ...what a turn in
    that chat runs with}. Nothing is saved and the chat never hears of it."""
    from . import claude_voice
    asked = FORK_ASK.format(you=you or "the person", guide=guide or "plain words", text=text)
    out = await claude_voice.side_copy(claude, prompt=asked, **turn)
    if not out.get("ok"):
        return out
    return {"ok": True, **parse_translation(out.get("response") or ""), "model": out.get("model"), "node": out.get("node")}


async def translate(client: httpx.AsyncClient, text: str, guide: str, model_override: str = "",
                    claude: Any = None, claude_model: str = "", context: str = "") -> dict[str, Any]:
    """Words -> a prompt in the model's style, by whoever answers the chat:
    Claude (one shot, `claude` is the ClaudeConfig) when the chat is Claude's,
    else the chat's own local model through Lodestar's chat (model_override
    routes it to the node that model lives on). `context`: the chat's recent
    messages (chat_context), handed over so the request's "us" and "that"
    mean something. {ok, prompt, negative, model, node} or {ok: false, error}."""
    system = TRANSLATE_SYSTEM.format(guide=guide or "plain words")
    if context:
        system += CONTEXT_NOTE
        text = f"The conversation so far:\n\n{context}\n\n---\nThe picture asked for:\n{text}"
    if claude is not None:
        from . import claude_voice
        out = await claude_voice.one_shot(claude, prompt=text, system_prompt=system + " Use no tools.",
                                          model=claude_model)
        if not out.get("ok"):
            return out
        return {"ok": True, **parse_translation(out.get("response") or ""), "model": out.get("model"),
                "node": out.get("node")}
    payload: dict[str, Any] = {"prompt": text, "system_prompt": system, "temperature": 0.4, "max_tokens": 400}
    if model_override:
        payload["model_override"] = model_override
    try:
        r = await client.post("/api/v1/chat", json=payload, timeout=180.0)
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Lodestar unreachable: {e}"}
    if r.status_code != 200:
        try:
            detail = r.json()
        except ValueError:
            detail = {"error": r.text[:300]}
        return {"ok": False, "error": detail.get("error") or f"Lodestar answered {r.status_code}",
                "detail": detail.get("detail")}
    out = r.json()
    got = parse_translation(out.get("response") or "")
    return {"ok": True, **got, "model": out.get("model"), "node": out.get("node")}


def picture_name(seed: Any, why: str, i: int, count: int, mime: str) -> str:
    ext = ".jpg" if "jpeg" in (mime or "") else ".png"
    stamp = dt.datetime.now().strftime("%H%M%S")
    return f"picture-{stamp}-{seed}-{slug(why)}{f'-{i + 1}' if count > 1 else ''}{ext}"


def model_name(model: Any) -> Optional[str]:
    """Lodestar names the model by catalog key ("checkpoints/x.safetensors")
    or as {name, ...}; the file's name either way."""
    if isinstance(model, dict):
        model = model.get("name") or model.get("key")
    return str(model).replace("\\", "/").rsplit("/", 1)[-1] if model else None


def drawn_settings(out: dict[str, Any], request: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """What made a picture, kept on its file entry as `drawn` (24 Oct 2026:
    the user wanted to click a picture and see what made it - prompt, negative,
    the LoRAs and their weights, the lot). Lodestar's answer first: it says
    what was actually drawn, the model's own negative added, the profile's
    LoRAs chosen. The request fills what the answer leaves out - an edited
    plan's workflow comes back with no description - and is kept as asked,
    less the workflow, which can be the size of the rest put together."""
    req = {k: v for k, v in (request or {}).items() if k not in ("workflow", "holder", "save_as")}

    def got(k: str) -> Any:
        return out.get(k) if out.get(k) not in (None, "") else req.get(k)

    loras = out.get("loras") if out.get("loras") is not None else req.get("loras")
    worn = [{"name": l.get("name"), "weight": l.get("weight")} if isinstance(l, dict) else {"name": l, "weight": None}
            for l in loras or []]
    drawn = {"model": model_name(got("model")), "arch": out.get("arch"), "base": out.get("base"),
             "vae": model_name(out.get("vae")), "node": out.get("node"), "prompt": got("prompt"),
             "negative": got("negative"), "seed": got("seed"), "width": got("width"), "height": got("height"),
             "count": got("count"), "loras": worn, "settings": dict(out.get("settings") or {}),
             "notes": list(out.get("notes") or []), "seconds": out.get("seconds"),
             "at": dt.datetime.now().isoformat(timespec="seconds")}
    drawn = {k: v for k, v in drawn.items() if v is not None}
    if req:
        drawn["request"] = req
    if isinstance((request or {}).get("workflow"), dict):
        drawn["from_workflow"] = True          # drawn from an edited plan: the workflow said the rest
    return drawn


def keep_drawn(store: SessionStore, sid: str, asked: str, why: str, out: dict[str, Any],
               request: Optional[dict[str, Any]] = None, seat: Optional[str] = None,
               private: bool = False) -> Optional[dict[str, Any]]:
    """The drawing into the chat: each picture a file beside the session,
    carrying what made it (drawn_settings), the turn recorded - the request
    as what you said, the pictures as the answer with a receipt (model,
    node, seed, seconds). None when there is no such session or nothing
    came back.

    `seat`: a seat of a room showed it - what IT sees (25 Nov 2026, Chad:
    an NPC "displays what they see ... this would be hidden knowledge"). The
    pictures are its answer, under its name; with `private`, the request and
    the pictures are that seat's alone (`only_for`): no other seat is handed
    them, and it is - so it knows what it showed you."""
    images = out.get("images") or []
    drawn = drawn_settings(out, request)
    entries = []
    for i, im in enumerate(images):
        try:
            data = base64.b64decode(im.get("data") or "")
        except (ValueError, TypeError):
            continue
        if not data:
            continue
        mime = im.get("mime") or "image/png"
        entry = store.add_file(sid, picture_name(out.get("seed"), why or asked, i, len(images), mime), data, mime)
        if entry is None:
            return None
        # on the entry, not a sidecar file: it goes where the picture goes -
        # a fork, an export, an import - and the files folder holds only files
        entry["drawn"] = {**drawn, **({"index": i + 1} if len(images) > 1 else {})}
        entries.append(entry)
    if not entries:
        return None
    lines = [f"![{why or asked}](files/{e['name']})" for e in entries]
    caption = f"*{why}*" if why else ""
    answer = "\n\n".join([*lines, caption]).strip()
    meta = {"voice": "picture", "model": out.get("model"), "node": out.get("node"), "seed": out.get("seed"),
            "seconds": out.get("seconds"), "prompt": out.get("prompt"), "negative": out.get("negative"),
            "loras": out.get("loras"), "width": out.get("width"), "height": out.get("height"),
            "settings": out.get("settings") or {}, "notes": out.get("notes") or []}
    saved = store.record(sid, asked, answer, meta, answer_files=entries)
    if saved is not None and seat:
        saved = store.shown_by(sid, seat, private) or saved
    return {"files": entries, "answer": answer, "meta": meta, "session": saved}


# What a turn's receipt kept before the pictures carried `drawn` (before 24
# Oct 2026): enough to show, never steps, cfg or the sampler.
_RECEIPT = ("model", "node", "prompt", "negative", "seed", "width", "height", "loras", "notes", "seconds")
NOT_KEPT = "no settings were kept for this picture - it was not drawn here, or was drawn before Symposium kept them"
PARTIAL = ("drawn before Symposium kept every setting: this is what the chat's receipt has; "
           "steps, cfg and the sampler were not kept")


def made_of(session: dict[str, Any], name: str) -> Optional[dict[str, Any]]:
    """What made picture `name` in this chat: {name, kept, drawn, note}.
    kept is "all" (it carries `drawn`), "receipt" (an older drawing: what
    its turn's receipt has), or "none" (attached, a screen share, or drawn
    before even the receipt). None when no message in the chat has it."""
    for m in session.get("messages") or []:
        for f in m.get("files") or []:
            if f.get("name") != name:
                continue
            if isinstance(f.get("drawn"), dict):
                return {"name": name, "kept": "all", "drawn": f["drawn"], "note": ""}
            meta = m.get("meta") or {}
            if meta.get("voice") == "picture" and meta.get("prompt"):
                old = {k: meta.get(k) for k in _RECEIPT if meta.get(k) not in (None, "")}
                if "model" in old:
                    old["model"] = model_name(old["model"])
                return {"name": name, "kept": "receipt", "drawn": old, "note": PARTIAL}
            return {"name": name, "kept": "none", "drawn": None, "note": NOT_KEPT}
    return None


__all__ = ["shape_options", "parse_translation", "translate", "keep_drawn", "picture_name", "slug", "DRAW_TIMEOUT",
           "TRANSLATE_SYSTEM", "drawn_settings", "made_of", "model_name"]
