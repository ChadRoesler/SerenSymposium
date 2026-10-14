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

    Text from inside a subagent (parent_tool_use_id set) is not ours to show.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import uuid
from contextlib import aclosing
from typing import Any, AsyncIterator, Optional

from .config import ClaudeConfig

# Bookkeeping the CLI does in front of every deferred MCP tool: not a tool
# the answer reached for, so not one the receipt names.
QUIET_TOOLS = {"ToolSearch"}

# Set by a parent Claude Code; a child that inherits them thinks it is nested.
PARENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

STDERR_TAIL = 2000


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


def argv(cfg: ClaudeConfig, *, resume: Optional[str], new_id: str,
         model: str = "", system_prompt: str = "") -> list[str]:
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
    if cfg.allowed_tools:
        out += ["--allowedTools", *cfg.allowed_tools]
    return out


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for k in PARENT_ENV:
        env.pop(k, None)
    return env


async def _raw_lines(cmd: list[str], prompt: str, cwd: str, timeout: float,
                     stderr: list[str]) -> AsyncIterator[str]:
    """The child's stdout, line by line. Popen and a reader thread rather
    than asyncio's subprocess support: that needs the Proactor loop on
    Windows, and which loop uvicorn picks is not ours to count on.

    The child is killed if it outlives `timeout`, or if whoever is reading
    stops (the shell closed mid-answer)."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd or None, env=_env(),
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


async def events(cfg: ClaudeConfig, *, prompt: str, resume: Optional[str], model: str = "",
                 system_prompt: str = "", turn: Optional[ClaudeTurn] = None
                 ) -> AsyncIterator[dict[str, Any]]:
    """One turn, as Symposium stream events (see the module docstring).
    Always ends with exactly one `done` or `error`."""
    turn = turn or ClaudeTurn(resume or str(uuid.uuid4()))
    cmd = argv(cfg, resume=resume, new_id=turn.session_id, model=model, system_prompt=system_prompt)
    stderr: list[str] = []
    try:
        lines = _raw_lines(cmd, prompt, cfg.cwd and os.path.expanduser(cfg.cwd), cfg.timeout_seconds, stderr)
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
                    if inner.get("type") == "content_block_start" and block.get("type") == "tool_use":
                        name = block.get("name") or "a tool"
                        if name not in QUIET_TOOLS:
                            turn.tools.append(name)
                            yield {"type": "tool_status", "tool": name}
                    elif inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        yield {"type": "chunk", "content": delta.get("text", "")}
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
