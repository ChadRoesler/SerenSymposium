"""
seren_symposium.terminal
════════════════════════════════════════════════════════════════════════

The claude CLI's own conversations on this box - the ones started in a
terminal - seen from Symposium, so one can be PICKED UP into a chat here
instead of jumping between terminal windows.

WHERE THEY ARE:
    The CLI keeps every conversation as <projects>/<folder slug>/<id>.jsonl
    (~/.claude/projects by default, claude.projects_dir to say otherwise).
    Each line is a record; the ones read here are `user` and `assistant`
    (the words), `ai-title` / `custom-title` (its name) and the `cwd` every
    message carries. Everything else - api requests, file snapshots, hook
    bookkeeping - is the CLI's and is left alone. Read only: nothing here
    ever writes a transcript.

WHY THE FOLDER MATTERS:
    `--resume <id>` only finds a conversation from the folder it was started
    in (the slug is that folder). So a picked-up chat keeps the cwd its
    transcript names as its own `cwd` setting, and its turns run there - in
    that repo, with that repo's CLAUDE.md and project settings - not in
    claude.cwd.

TWO WRITERS, ONE TRANSCRIPT:
    A conversation still open in a terminal is still being written there.
    Resuming it here as well would put two processes on one file, each
    answering without the other's turns. So a transcript touched in the last
    WARM_SECONDS is called `warm`, and picking it up FORKS it by default
    (--fork-session on the first turn: a new id, the same past), leaving the
    terminal's own untouched. A cold one is taken over: the next terminal
    `claude --resume` of it sees what was said here.

ONLY A TERMINAL'S:
    Every record says how its conversation was entered (`entrypoint`): "cli"
    at a terminal, "sdk-cli" headless. Only one that BEGAN at a terminal is
    listed or picked up; a hippocampus wake, a ripple or a chat Symposium
    itself started never is, whatever its id.

WHAT COMES OVER:
    The words, not the workings: what the person typed and what was said
    back, in order. Tool calls and results, thinking, hook output and
    <system-reminder> blocks stay in the transcript - the CLI already has
    them, since the chat resumes that very conversation.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

# How the CLI marks a conversation begun at a terminal. A headless one (-p)
# says "sdk-cli": the hippocampus waking a session, the Observatory's ripple,
# Symposium's own turns. Those live where they were started and are never
# picked up (Chad, 15 Nov 2026: "hippocampus runs dont live in symposium").
TERMINAL = "cli"
# Touched this recently: maybe still open in a terminal.
WARM_SECONDS = 600
# How many transcripts the list looks at, newest first.
LIST_LIMIT = 40
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# What the CLI writes as a "user" message that the person never typed: a
# slash command's bookkeeping and its local output - and the harness's own
# notices, a background task finishing and a subagent's report (18 Nov 2026,
# the first tangle on 5.7.0: a picked-up chat had eight of them stored as the
# person's, and a seat was handed them as "[Chad]" lines, one a block of
# instruction-shaped text he never wrote).
_NOT_SAID = ("<command-name>", "<command-message>", "<local-command-", "<bash-input>", "<bash-stdout>",
             "<task-notification>", "<agent-message")


def harness_notice(m: Any) -> bool:
    """A stored message that is the harness's notice and not anyone's words:
    one a pickup kept before it knew better. Not handed on as the person's."""
    return (isinstance(m, dict) and m.get("role") == "user" and not m.get("source")
            and str(m.get("content") or "").lstrip().startswith(_NOT_SAID[-2:]))
MAX_TITLE = 120


def projects_dir(configured: str = "") -> Path:
    return Path(os.path.expanduser(configured)) if configured else Path.home() / ".claude" / "projects"


def is_id(sid: Any) -> bool:
    return isinstance(sid, str) and bool(_UUID.match(sid))


def find(root: Path, sid: str) -> Optional[Path]:
    """The transcript of conversation `sid`, or None. The id is checked
    before it is used to build a path."""
    if not is_id(sid) or not root.is_dir():
        return None
    return next((p for p in root.glob(f"*/{sid}.jsonl") if p.is_file()), None)


def _records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue          # a line half-written by a CLI still running
                if isinstance(r, dict):
                    yield r
    except OSError:
        return


_TOOL_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_FULL = 2_000_000      # one tool's full result as the window opens it; a bigger one is cut, and says so


def tool_step(path: Path, tool_id: str) -> Optional[dict[str, Any]]:
    """One tool call in full, as the CLI kept it: {tool, input, result, ok,
    cut} or None. A turn keeps a few hundred characters of each result
    (claude_voice.MAX_RESULT); the whole of it is here, read when someone
    opens it (22 Nov 2026: Chad could not see enough of a tool call to help
    with it)."""
    if not _TOOL_ID.match(tool_id or ""):
        return None
    call: Optional[dict[str, Any]] = None
    for r in _records(path):
        content = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("id") == tool_id:
                call = {"tool": str(c.get("name") or ""), "input": c.get("input")}
            elif c.get("type") == "tool_result" and c.get("tool_use_id") == tool_id:
                body = c.get("content")
                text = body if isinstance(body, str) else "\n".join(
                    str(b.get("text") or "") if b.get("type") == "text" else f"[{b.get('type')}]"
                    for b in body if isinstance(b, dict)) if isinstance(body, list) else ""
                return {**(call or {"tool": "", "input": None}), "ok": not c.get("is_error"),
                        "result": text[:MAX_FULL], "cut": len(text) > MAX_FULL}
    return {**call, "ok": None, "result": None, "cut": False} if call else None


_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TASK_TAIL = 24 * 1024     # how much of the end of a task's output the window is shown


def task_output_dirs() -> list[Path]:
    """Where the CLI keeps a background task's output: <temp>/claude/<hash>/
    <conversation>/tasks/<task id>.output (Claude Code 2.1.329 on Windows;
    CLAUDE_CODE_TMPDIR moves the <temp>)."""
    import tempfile
    roots = [os.environ.get("CLAUDE_CODE_TMPDIR") or "", tempfile.gettempdir()]
    return [Path(r) / "claude" for r in dict.fromkeys(roots) if r]


def task_info(transcript: Optional[Path], conversation: str, task_id: str,
              dirs: Optional[list[Path]] = None) -> dict[str, Any]:
    """A background task looked into: what started it (the tool call whose
    answer named its id: {tool, input}) and the end of what it has written so
    far ({tail, size, written_at, path}). Either may be missing - an agent
    task writes no output file, a transcript may be gone."""
    out: dict[str, Any] = {"started_by": None, "output": None}
    if not _TASK_ID.match(task_id or "") or not is_id(conversation):
        return out
    if transcript is not None:
        calls: dict[str, dict[str, Any]] = {}
        for r in _records(transcript):
            content = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
            for c in content if isinstance(content, list) else []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use" and isinstance(c.get("id"), str):
                    calls[c["id"]] = {"tool": str(c.get("name") or ""), "input": c.get("input"), "id": c["id"]}
                elif c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                    body = c.get("content")
                    text = body if isinstance(body, str) else " ".join(
                        str(b.get("text") or "") for b in body if isinstance(b, dict)) if isinstance(body, list) else ""
                    if task_id in text and out["started_by"] is None:     # the first: later ones read its output
                        out["started_by"] = calls[c["tool_use_id"]]
    for root in dirs if dirs is not None else task_output_dirs():
        for p in root.glob(f"*/{conversation}/tasks/{task_id}.output") if root.is_dir() else []:
            try:
                st = p.stat()
                with p.open("rb") as f:
                    f.seek(max(0, st.st_size - TASK_TAIL))
                    tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            out["output"] = {"tail": tail, "size": st.st_size, "written_at": st.st_mtime, "path": str(p),
                             "cut": st.st_size > TASK_TAIL}
            return out
    return out


def _at(r: dict[str, Any]) -> Optional[float]:
    ts = r.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return round(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(), 3)
    except ValueError:
        return None


def _user_text(r: dict[str, Any]) -> str:
    """What the person said in a `user` record - "" for one they did not
    (a tool's result, hook output, a command's bookkeeping)."""
    if r.get("isMeta") or r.get("isSidechain") or r.get("isCompactSummary"):
        return ""
    content = (r.get("message") or {}).get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return ""
    text = _REMINDER.sub("", "\n\n".join(p for p in parts if isinstance(p, str))).strip()
    return "" if text.startswith(_NOT_SAID) else text


def _queued_text(r: dict[str, Any]) -> str:
    """What the person said WHILE an answer was under way: the CLI keeps it
    not as a `user` record but as an `attachment` (a queued_command) handed
    to the model mid-turn (found 14 Nov 2026 - a pickup that read only
    `user` records lost every word said mid-turn)."""
    a = r.get("attachment")
    if r.get("isSidechain") or not isinstance(a, dict) or a.get("type") != "queued_command":
        return ""
    text = a.get("prompt")
    text = _REMINDER.sub("", text).strip() if isinstance(text, str) else ""
    # A task finishing mid-answer is queued the same way, and is nobody's words.
    return "" if text.startswith(_NOT_SAID) else text


def _assistant_text(r: dict[str, Any]) -> str:
    if r.get("isSidechain"):
        return ""
    content = (r.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n\n".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text" and b.get("text")).strip()


def transcript(path: Path) -> dict[str, Any]:
    """One conversation, read: {id, cwd, title, messages}. Messages are
    Symposium's shape - {role, content, at} - one assistant message for all
    that was said between two of the person's."""
    cwd, first, entry = "", "", ""
    titles: dict[str, str] = {}               # ai-title, custom-title: the latest of each
    messages: list[dict[str, Any]] = []
    said: list[str] = []
    said_at: Optional[float] = None

    def close() -> None:
        nonlocal said, said_at
        if said:
            messages.append({"role": "assistant", "content": "\n\n".join(said), "at": said_at or time.time()})
        said, said_at = [], None

    for r in _records(path):
        kind = r.get("type")
        if not cwd and isinstance(r.get("cwd"), str):
            cwd = r["cwd"]
        if not entry and isinstance(r.get("entrypoint"), str):
            entry = r["entrypoint"]           # how it STARTED: a later turn picked up here is sdk-cli
        if kind in ("ai-title", "custom-title"):
            name = r.get("customTitle") or r.get("aiTitle") or r.get("title")
            if isinstance(name, str) and name.strip():
                titles[kind] = name.strip()
        elif kind in ("user", "attachment"):
            text = _user_text(r) if kind == "user" else _queued_text(r)
            if text:
                close()
                messages.append({"role": "user", "content": text, "at": _at(r) or time.time()})
                first = first or text
        elif kind == "assistant":
            text = _assistant_text(r)
            if text:
                said.append(text)
                said_at = said_at or _at(r)
    close()
    # a name the person gave wins over the one the CLI guessed
    title = titles.get("custom-title") or titles.get("ai-title") or " ".join(first.split())[:48]
    return {"id": path.stem, "cwd": cwd, "title": title[:MAX_TITLE], "messages": messages,
            "terminal": entry == TERMINAL}


# What a list row needs, by (path, size, mtime): a transcript only grows, so a
# row read once is good until the file changes.
_ROWS: dict[str, tuple[tuple[int, float], dict[str, Any]]] = {}


def _glance(path: Path) -> dict[str, Any]:
    """A list row's worth of a transcript, without reading it as one: only
    the lines that can matter are parsed. A long conversation is a hundred MB
    and more of tool output and file snapshots (15 Nov 2026: one at 161 MB,
    and a full read of 40 of them took 14 s), none of which a row shows."""
    cwd, first, entry, said = "", "", "", 0
    titles: dict[str, str] = {}
    try:
        f = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return {"cwd": "", "title": "", "said": 0, "terminal": False}
    with f:
        for line in f:
            head = line[:80]
            if not entry and '"entrypoint":' in line:
                r = _loads(line)
                entry, cwd = str(r.get("entrypoint") or ""), cwd or str(r.get("cwd") or "")
                if entry != TERMINAL:
                    break             # a headless run: no row, so read no further
            if '"type":"ai-title"' in head or '"type":"custom-title"' in head:
                r = _loads(line)
                name = r.get("customTitle") or r.get("aiTitle")
                if isinstance(name, str) and name.strip():
                    titles[r.get("type")] = name.strip()
            elif '"type":"user"' in line and '"tool_result"' not in line or '"queued_command"' in line:
                r = _loads(line)
                text = _user_text(r) if r.get("type") == "user" else _queued_text(r)
                if text:
                    said += 1
                    first = first or text
    title = titles.get("custom-title") or titles.get("ai-title") or " ".join(first.split())[:48]
    return {"cwd": cwd, "title": title[:MAX_TITLE], "said": said, "terminal": entry == TERMINAL}


def _loads(line: str) -> dict[str, Any]:
    try:
        r = json.loads(line)
    except ValueError:
        return {}
    return r if isinstance(r, dict) else {}


def listing(root: Path, *, exclude: frozenset[str] = frozenset(), limit: int = LIST_LIMIT,
            now: Optional[float] = None) -> list[dict[str, Any]]:
    """The newest conversations on this box, newest first: {id, cwd, title,
    updated, said (how many times the person spoke), warm}. `exclude`: ids
    already a chat here (Symposium's own turns are CLI conversations too)."""
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    found = []
    for p in root.glob("*/*.jsonl"):
        if not is_id(p.stem) or p.stem in exclude:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        found.append((st.st_mtime, st.st_size, p))
    out = []
    for mtime, size, p in sorted(found, key=lambda t: t[0], reverse=True):
        if len(out) >= limit:
            break                     # the limit counts rows: the wakes skipped don't spend it
        key, stamp = str(p), (size, mtime)
        if key not in _ROWS or _ROWS[key][0] != stamp:
            _ROWS[key] = (stamp, _glance(p))
        g = _ROWS[key][1]
        if not g["terminal"] or not g["said"]:
            continue                  # a headless run, or opened and left: nothing to pick up
        out.append({"id": p.stem, "cwd": g["cwd"], "title": g["title"], "updated": round(mtime, 3),
                    "said": g["said"], "warm": now - mtime < WARM_SECONDS})
    return out
