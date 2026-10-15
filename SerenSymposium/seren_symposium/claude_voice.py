"""
seren_symposium.claude_voice
════════════════════════════════════════════════════════════════════════

The other voice a chat can have: Claude, through the `claude` CLI on this
machine, instead of a local model through Lodestar.

WHY A SUBPROCESS AND NOT AN API CALL:
    The CLI is the harness the Claude who answers already lives in - its
    login, its MCP servers (a Workbench, the stores), its hooks. Symposium
    becomes the front end of that harness rather than a second, thinner one.
    The cost is that this voice exists only where the CLI is installed.

ONE CHAT, ONE CONVERSATION:
    A chat's first Claude turn starts a CLI session with a fresh id
    (--session-id); the id is written to the chat once the turn comes back,
    and every later turn resumes it (--resume). A turn that fails leaves no
    id behind, so the next one simply starts fresh. A chat that already has
    messages from another voice hands them over once, as context in front of
    that first prompt.

THE COMMAND IS OURS:
    The program and its flags come from the yaml (ClaudeConfig); a request
    supplies only the prompt, which goes to stdin - never into an argument a
    shell might parse. The system prompt and model come from the folder or
    chat settings, like every other setting.

WHAT COMES OUT:
    The CLI's stream-json, translated line by line into the shapes Lodestar's
    /chat/stream uses, so the shell reads one stream whoever is answering:

        text delta (top level)     -> {"type": "chunk", "content": ...}
        tool_use starting          -> {"type": "tool_status", "tool": ...}
        result, success            -> {"type": "done", "response", "model", "node", "tool_rounds"}
        result, error / no result  -> {"type": "error", "error", "detail"?}

    and, for the steps under the fold of an answer (kept in its receipt):

        thinking delta             -> {"type": "thinking", "content": ...}
        tool_use, input complete   -> {"type": "tool_call", "id", "tool", "summary"}
        its tool_result            -> {"type": "tool_result", "id", "ok", "preview"}

    Text from inside a subagent (parent_tool_use_id set) is not ours to show.

ASKED, NOT REFUSED:
    -p has nobody at a terminal to say yes, so a tool not on the allowed list
    was refused. With claude.ask, the CLI is given a permission tool of ours
    instead (seren_symposium.permit): it asks Symposium, Symposium asks you
    in the chat, and your answer goes back. Where to ask, and the turn's
    ticket, ride in the CLI's environment - which it hands to that tool.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from contextlib import aclosing
from typing import Any, AsyncIterator, Optional

from .config import ClaudeConfig

# Bookkeeping the CLI does in front of every deferred MCP tool: not a tool
# the answer reached for, so not one the receipt names.
QUIET_TOOLS = {"ToolSearch"}

# Set by a parent Claude Code; a child that inherits them thinks it is nested.
PARENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

STDERR_TAIL = 2000
# What a turn keeps of its steps: enough to see what it did and why, not a
# second transcript of every file it read.
MAX_STEPS, MAX_THINKING, MAX_RESULT = 80, 8000, 400


def available(cfg: ClaudeConfig) -> bool:
    """Whether this machine can run the voice at all."""
    return bool(cfg.enabled and cfg.command and shutil.which(cfg.command[0]))


def handover(history: list[dict[str, Any]], prompt: str, resumed: bool = False) -> str:
    """The prompt for a Claude turn when the chat has messages this Claude
    conversation never saw - all of them on its first turn, or the ones
    another voice answered since its last: those, plainly, then the ask."""
    if not history:
        return prompt
    lines = ["In this chat, while another voice was answering:" if resumed
             else "Earlier in this chat, before you were answering in it:", ""]
    for m in history:
        lines.append(f"[{m.get('role')}] {m.get('content', '')}")
    lines += ["", "Now:", "", prompt]
    return "\n".join(lines)


def folder_rule(path: str, tool: str = "Edit") -> str:
    """A permission rule letting the CLI use `tool` in `path` and below - in
    the spelling it matches: `//` then the absolute path with forward slashes,
    a Windows drive as /c (lower case, no colon): C:/a/b -> Edit(//c/a/b/**).
    (Found by trying it against claude 2.1 on Windows, 14 Oct 2026: the
    `//C:/...` spelling is not matched, and the write is refused.)"""
    p = str(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return f"{tool}(/{p.rstrip('/')}/**)"


# What "Always" means for each kind of ask: no wider than the thing asked
# about - the folder a file is in, the first word of a command, a web
# site - except for a tool with no handle like that, which is the whole tool.
FILE_TOOLS = {"Read": "Read", "Glob": "Read", "Grep": "Read", "LS": "Read",
              "Edit": "Edit", "Write": "Edit", "MultiEdit": "Edit", "NotebookEdit": "Edit"}


def _target(tool: str, tool_input: dict[str, Any]) -> str:
    for k in ("file_path", "notebook_path", "path"):
        if isinstance(tool_input.get(k), str) and tool_input[k]:
            return tool_input[k]
    return ""


def suggest_rule(tool: str, tool_input: Any) -> str:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool in FILE_TOOLS and (target := _target(tool, tool_input)):
        # Spelled the way the CLI sent it, not the way this OS spells paths:
        # on Linux, Path("D:\\a\\b.txt") is one file name with no folder.
        windows = "\\" in target or (len(target) > 1 and target[1] == ":")
        where = PureWindowsPath(target) if windows else PurePosixPath(target)
        folder = where if tool in ("Glob", "Grep", "LS") or not where.suffix else where.parent
        return folder_rule(str(folder), FILE_TOOLS[tool])
    if tool == "Bash" and isinstance(tool_input.get("command"), str):
        first = tool_input["command"].strip().split()[:1]
        return f"Bash({first[0]}:*)" if first else "Bash"
    if tool == "WebFetch" and isinstance(tool_input.get("url"), str):
        from urllib.parse import urlparse
        host = urlparse(tool_input["url"]).hostname
        return f"WebFetch(domain:{host})" if host else "WebFetch"
    return tool


def summarize(tool: str, tool_input: Any, limit: int = 300) -> str:
    """The ask, as a line a person can judge: the command, the file, the
    address - or the input itself, cut short."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    for k in ("command", "file_path", "notebook_path", "url", "path", "pattern", "query"):
        if isinstance(tool_input.get(k), str) and tool_input[k]:
            return tool_input[k][:limit]
    s = json.dumps(tool_input, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def permit_server() -> list[str]:
    """How the CLI starts our permission tool. pythonw where there is one: a
    console program started under the window would flash a console."""
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return [str(quiet if os.name == "nt" and quiet.is_file() else exe), "-m", "seren_symposium.permit"]


def argv(cfg: ClaudeConfig, *, resume: Optional[str], new_id: str,
         model: str = "", system_prompt: str = "", add_dirs: Optional[list[str]] = None,
         allow: Optional[list[str]] = None, ask: bool = False) -> list[str]:
    exe = shutil.which(cfg.command[0]) or cfg.command[0]
    out = [exe, *cfg.command[1:], "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages"]
    out += ["--resume", resume] if resume else ["--session-id", new_id]
    if model := (model or cfg.model):
        out += ["--model", model]
    if system_prompt:
        out += ["--append-system-prompt", system_prompt]
    if cfg.permission_mode:
        out += ["--permission-mode", cfg.permission_mode]
    for d in [*cfg.add_dirs, *(add_dirs or [])]:
        out += ["--add-dir", os.path.expanduser(d)]
    if tools := [*cfg.allowed_tools, *(allow or [])]:
        out += ["--allowedTools", *tools]
    if ask:
        mcp = {"mcpServers": {"symposium": {"command": permit_server()[0], "args": permit_server()[1:]}}}
        out += ["--mcp-config", json.dumps(mcp), "--permission-prompt-tool", "mcp__symposium__permit"]
    return out


def _env(permit: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(os.environ)
    for k in PARENT_ENV:
        env.pop(k, None)
    if permit:
        # Not argv: any process can read a command line. The CLI hands its
        # environment to the permission tool it starts.
        env["SYMPOSIUM_PERMIT_URL"], env["SYMPOSIUM_PERMIT_TICKET"] = permit["url"], permit["ticket"]
    return env


async def _raw_lines(cmd: list[str], prompt: str, cwd: str, timeout: float,
                     stderr: list[str], env: Optional[dict[str, str]] = None) -> AsyncIterator[str]:
    """The child's stdout, line by line. Popen and a reader thread rather
    than asyncio's subprocess support: that needs the Proactor loop on
    Windows, and which loop uvicorn picks is not ours to count on.

    The child is killed if it outlives `timeout`, or if whoever is reading
    stops (the shell closed mid-answer)."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd or None, env=env or _env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def feed() -> None:
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass

    def read_out() -> None:
        for raw in proc.stdout:
            loop.call_soon_threadsafe(queue.put_nowait, raw.decode("utf-8", "replace"))
        loop.call_soon_threadsafe(queue.put_nowait, None)

    def read_err() -> None:
        for raw in proc.stderr:
            stderr.append(raw.decode("utf-8", "replace"))

    def watch() -> None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stderr.append(f"killed after {timeout:.0f}s\n")
            proc.kill()

    for fn in (feed, read_out, read_err, watch):
        threading.Thread(target=fn, daemon=True).start()
    try:
        while (line := await queue.get()) is not None:
            yield line
    finally:
        if proc.poll() is None:
            proc.kill()


class ClaudeTurn:
    """What one turn learned that the caller needs afterwards."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.model: Optional[str] = None
        self.tools: list[str] = []
        # Under the fold: [{"kind": "thinking", "text"} | {"kind": "tool", "id",
        # "tool", "summary", "ok"?, "result"?} | {"kind": "asked", ...}]
        self.steps: list[dict[str, Any]] = []

    def step(self, s: dict[str, Any]) -> None:
        if len(self.steps) < MAX_STEPS:
            self.steps.append(s)

    def think(self, text: str) -> None:
        last = self.steps[-1] if self.steps else None
        if last is not None and last["kind"] == "thinking" and not last.get("closed"):
            if len(last["text"]) < MAX_THINKING:
                last["text"] = (last["text"] + text)[:MAX_THINKING]
        else:
            self.step({"kind": "thinking", "text": text[:MAX_THINKING]})

    def kept_steps(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.items() if k != "closed"} for s in self.steps
                if s["kind"] != "thinking" or s["text"].strip()]


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


async def events(cfg: ClaudeConfig, *, prompt: str, resume: Optional[str], model: str = "",
                 system_prompt: str = "", add_dirs: Optional[list[str]] = None,
                 allow: Optional[list[str]] = None,
                 turn: Optional[ClaudeTurn] = None,
                 permit: Optional[dict[str, str]] = None) -> AsyncIterator[dict[str, Any]]:
    """One turn, as Symposium stream events (see the module docstring).
    Always ends with exactly one `done` or `error`. `permit` ({url, ticket})
    is where the CLI's permission asks go; without it, unlisted tools are
    refused as before."""
    turn = turn or ClaudeTurn(resume or str(uuid.uuid4()))
    cmd = argv(cfg, resume=resume, new_id=turn.session_id, model=model, system_prompt=system_prompt,
               add_dirs=add_dirs, allow=allow, ask=bool(permit))
    stderr: list[str] = []
    blocks: dict[Any, dict[str, Any]] = {}      # content block index -> what it is so far
    try:
        lines = _raw_lines(cmd, prompt, cfg.cwd and os.path.expanduser(cfg.cwd), cfg.timeout_seconds, stderr,
                           env=_env(permit))
        async with aclosing(lines):
            async for line in lines:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "system" and ev.get("subtype") == "init":
                    turn.model = ev.get("model")
                elif kind == "stream_event" and not ev.get("parent_tool_use_id"):
                    inner = ev.get("event") or {}
                    block = inner.get("content_block") or {}
                    delta = inner.get("delta") or {}
                    at = inner.get("index")
                    if inner.get("type") == "content_block_start":
                        blocks[at] = {"type": block.get("type"), "id": block.get("id"),
                                      "name": block.get("name"), "json": ""}
                        if block.get("type") == "tool_use":
                            name = block.get("name") or "a tool"
                            if name not in QUIET_TOOLS:
                                turn.tools.append(name)
                                yield {"type": "tool_status", "tool": name}
                    elif inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        yield {"type": "chunk", "content": delta.get("text", "")}
                    elif inner.get("type") == "content_block_delta" and delta.get("type") == "thinking_delta":
                        turn.think(delta.get("thinking", ""))
                        yield {"type": "thinking", "content": delta.get("thinking", "")}
                    elif inner.get("type") == "content_block_delta" and delta.get("type") == "input_json_delta":
                        if at in blocks:
                            blocks[at]["json"] += delta.get("partial_json", "")
                    elif inner.get("type") == "content_block_stop":
                        b = blocks.pop(at, None) or {}
                        if b.get("type") == "thinking" and turn.steps and turn.steps[-1]["kind"] == "thinking":
                            turn.steps[-1]["closed"] = True
                        elif b.get("type") == "tool_use" and (b.get("name") or "") not in QUIET_TOOLS:
                            try:
                                given = json.loads(b["json"] or "{}")
                            except ValueError:
                                given = {}
                            call = {"id": b.get("id"), "tool": b.get("name") or "a tool",
                                    "summary": summarize(b.get("name") or "", given, 200)}
                            turn.step({"kind": "tool", **call})
                            yield {"type": "tool_call", **call}
                elif kind == "user" and not ev.get("parent_tool_use_id"):
                    for c in (ev.get("message") or {}).get("content") or []:
                        if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                            continue
                        step = next((s for s in turn.steps if s.get("id") == c.get("tool_use_id")), None)
                        if step is None:
                            continue
                        step["ok"] = not c.get("is_error")
                        step["result"] = _result_text(c.get("content")).strip()[:MAX_RESULT]
                        yield {"type": "tool_result", "id": step["id"], "ok": step["ok"], "preview": step["result"]}
                elif kind == "result":
                    if ev.get("is_error") or ev.get("subtype") != "success":
                        errors = ev.get("errors") or []
                        yield {"type": "error", "error": "claude did not finish the answer",
                               "detail": "; ".join(map(str, errors)) or ev.get("result") or ev.get("subtype")}
                    else:
                        yield {"type": "done", "response": ev.get("result") or "", "model": turn.model,
                               "node": cfg.name, "tool_rounds": max(0, int(ev.get("num_turns") or 1) - 1)}
                    return
    except OSError as ex:
        yield {"type": "error", "error": "could not start claude", "detail": f"{type(ex).__name__}: {ex}"}
        return
    yield {"type": "error", "error": "claude stopped before answering",
           "detail": "".join(stderr)[-STDERR_TAIL:].strip() or "no output"}
