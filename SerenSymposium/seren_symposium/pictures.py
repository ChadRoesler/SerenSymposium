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
                shows it at full size now and when the gallery is tidied
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


async def translate(client: httpx.AsyncClient, text: str, guide: str, model_override: str = "",
                    claude: Any = None, claude_model: str = "") -> dict[str, Any]:
    """Words -> a prompt in the model's style, by whoever answers the chat:
    Claude (one shot, `claude` is the ClaudeConfig) when the chat is Claude's,
    else the chat's own local model through Lodestar's chat (model_override
    routes it to the node that model lives on). {ok, prompt, negative, model,
    node} or {ok: false, error}."""
    system = TRANSLATE_SYSTEM.format(guide=guide or "plain words")
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


def keep_drawn(store: SessionStore, sid: str, asked: str, why: str, out: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The drawing into the chat: each picture a file beside the session,
    the turn recorded - the request as what you said, the pictures as the
    answer with a receipt (model, node, seed, seconds). None when there is
    no such session or nothing came back."""
    images = out.get("images") or []
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
        entries.append(entry)
    if not entries:
        return None
    lines = [f"![{why or asked}](files/{e['name']})" for e in entries]
    caption = f"*{why}*" if why else ""
    answer = "\n\n".join([*lines, caption]).strip()
    meta = {"voice": "picture", "model": out.get("model"), "node": out.get("node"), "seed": out.get("seed"),
            "seconds": out.get("seconds"), "prompt": out.get("prompt"), "negative": out.get("negative"),
            "loras": out.get("loras"), "width": out.get("width"), "height": out.get("height"),
            "notes": out.get("notes") or []}
    saved = store.record(sid, asked, answer, meta, answer_files=entries)
    return {"files": entries, "answer": answer, "meta": meta, "session": saved}


__all__ = ["shape_options", "parse_translation", "translate", "keep_drawn", "picture_name", "slug", "DRAW_TIMEOUT",
           "TRANSLATE_SYSTEM"]
