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

EVERYTHING IT SAYS IS THE ANSWER:
    The CLI's own `result` is only the text after the last tool. What Claude
    says before and between tools ("let me look in that folder") used to
    stream into the window and then vanish when that result replaced it, and
    it never reached the saved chat or a later handover (22 Oct 2026). Each
    top-level text block is now a `said` step in the turn, in order among the
    thinking and the tools; the answer is all of them, joined; and the steps
    keep where each piece was said, so the window can show the words with the
    thinking and the tools folded between them.

ASKED, NOT REFUSED:
    -p has nobody at a terminal to say yes, so a tool not on the allowed list
    was refused. With claude.ask, the CLI is given a permission tool of ours
    instead (seren_symposium.permit): it asks Symposium, Symposium asks you
    in the chat, and your answer goes back. Where to ask, and the turn's
    ticket, ride in the CLI's environment - which it hands to that tool.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from contextlib import aclosing
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Optional

from .config import ClaudeConfig

log = logging.getLogger("seren_symposium")

# Bookkeeping the CLI does in front of every deferred MCP tool: not a tool
# the answer reached for, so not one the receipt names.
QUIET_TOOLS = {"ToolSearch"}

_UUIDISH = re.compile(r"^[0-9a-fA-F-]{8,64}$")

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
    if resumed and all(m.get("stopped") for m in history):
        # Only its own answer, cut short: said plainly as that, not as
        # something another voice did (app.as_told has why it goes back).
        lines = ["Your last answer in this chat was stopped by the person before it finished. The chat kept "
                 "what you had said, and your own record of that turn may be missing it. You had said:", ""]
        lines += [m.get("content") or "(nothing yet)" for m in history]
        return "\n".join(lines + ["", "Now:", "", prompt])
    lines = ["In this chat, while another voice was answering:" if resumed
             else "Earlier in this chat, before you were answering in it:", ""]
    for m in history:
        who = f"{m.get('role')}, stopped before it finished" if m.get("stopped") else m.get("role")
        lines.append(f"[{who}] {m.get('content', '')}")
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


def suggest_rules(tool: str, tool_input: Any) -> list[str]:
    """What an Always could be, narrowest first (22 Oct 2026: one per tool
    meant a Workbench asked a hundred times): the folder, the command's first
    word, the site, the one MCP tool - then the whole kind, the whole
    command, every site, the whole MCP server."""
    narrow = suggest_rule(tool, tool_input)
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    wider: list[str] = []
    if tool in FILE_TOOLS and _target(tool, tool_input):
        wider.append(FILE_TOOLS[tool])
    elif tool in ("Bash", "WebFetch") and narrow != tool:
        wider.append(tool)
    elif tool.startswith("mcp__") and tool.count("__") >= 2:
        wider.append(tool[:tool.index("__", 5) + 2] + "*")
    return list(dict.fromkeys([narrow, *wider]))


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


def user_mcp_servers(names: list[str], path: str = "") -> Optional[dict[str, Any]]:
    """The named servers, as Claude Code's user config (~/.claude.json)
    defines them - url, headers helper and all, so no token is copied into
    Symposium's config. None when a name is not there (or the file is not):
    the caller then loads servers the loose way rather than lose one."""
    if not names:
        return {}
    file = os.path.expanduser(path or "~/.claude.json")
    try:
        known = (json.load(open(file, encoding="utf-8")) or {}).get("mcpServers") or {}
    except (OSError, ValueError) as ex:
        log.warning("claude.mcp_servers: cannot read %s (%s); loading every server", file, ex)
        return None
    missing = [n for n in names if n not in known]
    if missing:
        log.warning("claude.mcp_servers: %s not in %s; loading every server", ", ".join(missing), file)
        return None
    return {n: known[n] for n in names}


# What the CLI says it loaded, kept for the prompt stack (seren_symposium.stack):
# a turn's init event names the model, the mode, every tool, the MCP servers
# and how each is doing, the skills, agents, commands and plugins. Names only,
# capped, so a box with everything installed still keeps a small session file.
MAX_INIT_ITEMS = 600


def digest_init(ev: dict[str, Any]) -> dict[str, Any]:
    """The CLI's init event as the prompt stack keeps it - never its session
    id, where its key came from, or its sockets."""
    def names(v: Any) -> list[str]:
        out = []
        for x in (v if isinstance(v, list) else [])[:MAX_INIT_ITEMS]:
            n = x.get("name") if isinstance(x, dict) else x
            if isinstance(n, str) and n:
                out.append(n[:200])
        return out
    servers = [{"name": str(s["name"])[:200], "status": str(s.get("status") or "")[:40],
                "source": str(s.get("source") or "")[:40]}
               for s in (ev.get("mcp_servers") or [])[:100] if isinstance(s, dict) and s.get("name")]
    memory = ev.get("memory_paths") if isinstance(ev.get("memory_paths"), dict) else {}
    return {"version": str(ev.get("claude_code_version") or ""), "model": str(ev.get("model") or ""),
            "mode": str(ev.get("permissionMode") or ""), "cwd": str(ev.get("cwd") or ""),
            "output_style": str(ev.get("output_style") or ""), "memory_auto": str(memory.get("auto") or ""),
            "tools": names(ev.get("tools")), "mcp_servers": servers, "skills": names(ev.get("skills")),
            "agents": names(ev.get("agents")), "slash_commands": names(ev.get("slash_commands")),
            "plugins": names(ev.get("plugins"))}


def argv(cfg: ClaudeConfig, *, resume: Optional[str], new_id: str,
         model: str = "", system_prompt: str = "", add_dirs: Optional[list[str]] = None,
         allow: Optional[list[str]] = None, ask: bool = False, replace: bool = False,
         mode: Optional[str] = None, own_tools: bool = True, fork: bool = False,
         ephemeral: bool = False, effort: str = "", thinking_display: str = "",
         resume_at: Optional[str] = None) -> list[str]:
    """`replace`: the system prompt is the whole one (--system-prompt, the
    preface in front), not an addition to the CLI's own. `mode` and
    `own_tools=False`: the turn's permissions were worked out in layers
    (seren_symposium.permissions) and `allow` is the whole list; without
    them, the config's mode and list, plus `allow`. `fork`: resume into a
    new id, leaving the conversation resumed as it was (a terminal's, still
    open - seren_symposium.terminal); the new id comes back in `init`.
    `ephemeral`: nothing of this turn is kept and nobody is told it happened -
    the CLI writes no transcript and runs no hooks (so no session-start
    bookmark, no kitchen magnet for a session that is gone a moment later).
    With `fork`, that is a side copy of a conversation: asked once, thrown
    away, the conversation itself untouched (side_copy)."""
    exe = shutil.which(cfg.command[0]) or cfg.command[0]
    out = [exe, *cfg.command[1:], "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages"]
    # Fed a message at a time, so the process can outlive its first answer
    # (claude.background; see Cli).
    if cfg.background:
        out += ["--input-format", "stream-json"]
    out += ["--resume", resume] if resume else ["--session-id", new_id]
    if resume and fork:
        out += ["--fork-session"]
    # Taken up at an earlier entry, not at its end: an edit or a retry
    # branches the conversation there (sessions.branch_point).
    if resume and resume_at and _UUIDISH.match(resume_at):
        out += ["--resume-session-at", resume_at]
    if ephemeral:
        out += ["--no-session-persistence", "--settings", json.dumps({"disableAllHooks": True})]
    if model := (model or cfg.model):
        out += ["--model", model]
    # Worked out in layers (seren_symposium.permissions.thinking); blank
    # leaves it to the CLI. --thinking-display is a flag --help does not list
    # (Claude Code 2.1.329): summarized, highlights or omitted.
    if effort:
        out += ["--effort", effort]
    if thinking_display:
        out += ["--thinking-display", thinking_display]
    if system_prompt:
        out += ["--system-prompt" if replace else "--append-system-prompt", system_prompt]
    if mode := (cfg.permission_mode if mode is None else mode):
        out += ["--permission-mode", mode]
    for d in [*cfg.add_dirs, *(add_dirs or [])]:
        out += ["--add-dir", os.path.expanduser(d)]
    if tools := list(dict.fromkeys([*(cfg.allowed_tools if own_tools else []), *(allow or [])])):
        out += ["--allowedTools", *tools]
    # Claude Code's own extras, cut down (claude.builtin_tools, claude.skills).
    if cfg.builtin_tools:
        out += ["--tools", ",".join(dict.fromkeys([*cfg.builtin_tools, "ToolSearch"]))]
    if not cfg.skills:
        out += ["--disable-slash-commands"]
    named = user_mcp_servers(cfg.mcp_servers, cfg.claude_json)
    # alwaysLoad: that server's tools are in the list from the start, not
    # behind ToolSearch (claude.mcp_always_load).
    servers: dict[str, Any] = {n: ({**s, "alwaysLoad": True} if n in cfg.mcp_always_load else s)
                               for n, s in (named or {}).items()}
    if ask:
        servers["symposium"] = {"command": permit_server()[0], "args": permit_server()[1:]}
        out += ["--permission-prompt-tool", "mcp__symposium__permit"]
    if servers:
        out += ["--mcp-config", json.dumps({"mcpServers": servers})]
    if named:
        out += ["--strict-mcp-config"]
    return out


def _env(permit: Optional[dict[str, str]] = None, cfg: Optional[ClaudeConfig] = None) -> dict[str, str]:
    env = dict(os.environ)
    for k in PARENT_ENV:
        env.pop(k, None)
    if cfg is not None:
        # What Claude Code brings of its own that this house did not ask for:
        # the account's claude.ai connectors, and its auto memory.
        # Set or cleared, never inherited: a Symposium that was itself started
        # by a Claude Code session (17 Nov 2026, a test run from inside one)
        # carries that session's switches, and the yaml is what decides here.
        env.pop("ENABLE_CLAUDEAI_MCP_SERVERS", None)
        env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        if not cfg.claudeai_connectors:
            env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        if cfg.auto_memory == "off":
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if permit:
        # Not argv: any process can read a command line. The CLI hands its
        # environment to the permission tool it starts.
        env["SYMPOSIUM_PERMIT_URL"], env["SYMPOSIUM_PERMIT_TICKET"] = permit["url"], permit["ticket"]
    return env


class Cli:
    """One claude CLI process: started, handed a prompt, read line by line.

    A process that outlives its answer (claude.background). The CLI can run
    a command in the background and, in its streaming-input mode, keeps
    going after it has answered: it can be asked something else meanwhile,
    and when the task ends it starts a turn of its own to say so. Symposium
    used to end the process at the first answer, which killed the task with
    it - so a seat either lost her build or held her turn open on it and
    froze the room (Chad, 19 Nov 2026, the soma and psycheion firmware
    builds). Measured on the real CLI, 2.1.326, the same day:

        the task outlived the first `result`                       (3.9s / 43s)
        a second message, sent at 4.1s, was answered at 4.8s
        at 43s `system task_notification`, then a turn nobody asked for
        `system background_tasks_changed` lists what is in flight ({tasks})
        `--resume` works the same fed this way
        stdin closed with a task running: the CLI waits for it, reports, exits

    So with claude.background the prompt goes as one stream-json line and
    stdin is left open; without it, the old way - the prompt, then EOF.

    Popen and reader threads rather than asyncio's subprocess support: that
    needs the Proactor loop on Windows, and which loop uvicorn picks is not
    ours to count on."""

    def __init__(self, cmd: list[str], cwd: str, env: dict[str, str], stream: bool, ticket: Optional[str] = None):
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self.stderr: list[str] = []
        self.stream = stream
        self.ticket = ticket                # where this process's permission asks go (its first turn's)
        self.tasks: list[dict[str, Any]] = []       # background tasks in flight, as the CLI last listed them
        self.task_seen: dict[str, float] = {}       # task id -> when the CLI first listed it (it says no start time)
        self.notes: list[str] = []                  # what the CLI said of tasks that ended, since the last answer
        self.started = time.time()
        self.idle = asyncio.Event()         # clear while it is in a turn it began on its own
        self.idle.set()
        self.watcher: Optional[asyncio.Task] = None
        self.owner: dict[str, Any] = {}             # {chat, seat}: whose it is, for the ledger (seren_symposium.behind)
        self.last_woke = 0.0                        # when what it said on its own was last kept in the chat
        self.cwd = cwd or ""
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd or None, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for fn in (self._read_out, self._read_err):
            threading.Thread(target=fn, daemon=True).start()

    def _read_out(self) -> None:
        try:
            for raw in self.proc.stdout:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, raw.decode("utf-8", "replace"))
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        except RuntimeError:
            pass                            # the loop it was read for has closed: nobody is listening

    def _read_err(self) -> None:
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace"))

    def send(self, prompt: str) -> None:
        """Hand it a message. Written from a thread: a long handover can fill
        the pipe, and the loop must not wait on the CLI reading it."""
        if self.stream:
            data = (json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n").encode("utf-8")
        else:
            data = prompt.encode("utf-8")

        def feed() -> None:
            try:
                self.proc.stdin.write(data)
                if self.stream:
                    self.proc.stdin.flush()
                else:
                    self.proc.stdin.close()
            except (OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.alive():
            self.proc.kill()

    def deadline(self, seconds: float) -> threading.Timer:
        """Kill it if the turn now starting outlives `seconds` (cancel the timer when the turn ends)."""
        def late() -> None:
            if self.alive():
                self.stderr.append(f"killed after {seconds:.0f}s\n")
                self.proc.kill()
        timer = threading.Timer(seconds, late)
        timer.daemon = True
        timer.start()
        return timer


# Processes set aside with a task still running, by the conversation they are
# (the CLI's session id): fed the next message for that conversation instead
# of a second process being started beside them, and watched meanwhile (_watch).
PARKED: dict[str, Cli] = {}
# What a set-aside conversation calls when it answers on its own:
# (the `done` or `error` event, the turn's ClaudeTurn, what the CLI said of the tasks that ended).
Wake = Callable[[dict[str, Any], "ClaudeTurn", list[str]], Awaitable[None]]
# The set-aside conversations, written down so they outlive Symposium
# (seren_symposium.behind; the app sets it). None: memory only, as before.
LEDGER: Optional[Any] = None
# Conversations an earlier Symposium let go, still running: picked up from the
# ledger at start (behind.pick_up), shown like PARKED ones until they end.
LET_GO: dict[str, dict[str, Any]] = {}


def _details(cli: Cli) -> list[dict[str, Any]]:
    return [{"id": str(t.get("task_id") or ""), "type": str(t.get("task_type") or ""),
             "description": str(t.get("description") or "")[:500],
             "since": cli.task_seen.get(str(t.get("task_id") or ""), cli.started)}
            for t in cli.tasks]


def _write_down(cli: Cli, key: str) -> None:
    if LEDGER is not None and cli.owner.get("chat"):
        LEDGER.put(key, {**cli.owner, "conversation": key, "pid": cli.proc.pid, "started": cli.started,
                         "cwd": cli.cwd, "tasks": _details(cli), "last_woke": cli.last_woke,
                         "written": time.time()})


def _cross_off(key: Optional[str]) -> None:
    if LEDGER is not None and key:
        LEDGER.drop(key)


def parked(key: Optional[str]) -> Optional[dict[str, Any]]:
    """What conversation `key` has running in the background, for the window:
    {tasks: [words], since, details: [{id, type, description, since}]} - the
    details so a task can be opened and looked into (23 Nov 2026: "after 5
    hours its still going ... being able to click and see it"). One an
    earlier Symposium let go says so: `let_go` (when)."""
    cli = PARKED.get(key or "")
    if cli is None or not cli.alive():
        gone = LET_GO.get(key or "")
        if gone is None:
            return None
        tasks = gone.get("tasks") or []
        return {"tasks": [str(t.get("description") or t.get("type") or "a task")[:200] for t in tasks],
                "since": gone.get("started"), "details": tasks, "let_go": gone.get("let_go")}
    return {"tasks": [str(t.get("description") or t.get("task_type") or "a task")[:200] for t in cli.tasks],
            "since": cli.started, "details": _details(cli)}


def parked_ticket(key: Optional[str]) -> Optional[str]:
    """Where a set-aside process's permission asks go: its first turn's ticket, which the turn now using it answers."""
    cli = PARKED.get(key or "")
    return cli.ticket if cli is not None and cli.alive() else None


def stop_parked(key: Optional[str]) -> bool:
    """End a set-aside process, and what it was running. False when there was none."""
    cli = PARKED.pop(key or "", None)
    if cli is None:
        gone = LET_GO.pop(key or "", None)
        if gone is None:
            return False
        kill_tree(gone.get("pid"))
        _cross_off(key)
        return True
    if cli.watcher is not None:
        cli.watcher.cancel()
    kill_tree(cli.proc.pid)
    cli.kill()
    _cross_off(key)
    return True


def kill_tree(pid: Any) -> None:
    """A CLI and everything it started - its background tasks run as its
    children, and ending the CLI alone can leave them running unseen. Only a
    pid that is still a claude (behind.pid_alive), never a stranger's."""
    from .behind import pid_alive
    if not pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def stop_all_parked() -> None:
    for key in list(PARKED):
        stop_parked(key)


def let_go_all_parked() -> None:
    """Symposium is going: what the conversations have running runs on. The
    watchers stop, the processes do not (their input closes as we go, and the
    CLI then finishes its task, reports into its transcript and exits); the
    ledger keeps them, stamped, for the next start to pick up (behind)."""
    now = time.time()
    for key, cli in list(PARKED.items()):
        PARKED.pop(key, None)
        if cli.watcher is not None:
            cli.watcher.cancel()                # cancelled = taken: _watch leaves the process be
        if LEDGER is not None and cli.owner.get("chat") and cli.alive():
            _write_down(cli, key)
            entry = LEDGER.entries().get(key)
            if entry is not None:
                LEDGER.put(key, {**entry, "let_go": now})
        try:
            cli.proc.stdin.close()              # nothing more will be asked: it finishes, reports, exits
        except (OSError, ValueError, AttributeError):
            pass


async def _take_parked(key: str) -> Optional[Cli]:
    """The set-aside process for conversation `key`, to be handed a message:
    out of the registry and no longer watched. A turn it had begun on its own
    (its task just ended) is let finish first, so that answer is not taken
    for the reply to what is about to be asked."""
    cli = PARKED.get(key)
    if cli is None:
        return None
    try:
        await asyncio.wait_for(cli.idle.wait(), timeout=120.0)
    except asyncio.TimeoutError:
        pass                                # said it was waking and never did: ask it anyway
    if PARKED.get(key) is not cli:
        return None                         # it ended, or was stopped, meanwhile
    PARKED.pop(key, None)
    if cli.watcher is not None:
        cli.watcher.cancel()
        try:
            await cli.watcher
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - only waiting for it to let go
            pass
        cli.watcher = None
    return cli if cli.alive() else None


def _park(cli: Cli, key: str, cfg: ClaudeConfig, on_wake: Wake, owner: Optional[dict[str, Any]] = None) -> None:
    PARKED[key] = cli
    if owner:
        cli.owner = dict(owner)
    _write_down(cli, key)
    cli.watcher = asyncio.get_running_loop().create_task(_watch(cli, key, cfg, on_wake))


async def _watch(cli: Cli, key: str, cfg: ClaudeConfig, on_wake: Wake) -> None:
    """A set-aside process, read while nobody is asking it anything. When a
    task of its ends the CLI starts a turn of its own; that turn's answer is
    handed to `on_wake`. With nothing left in flight after an answer, or
    after claude.background_max_seconds, the process is ended."""
    turn, blocks = ClaudeTurn(key), {}
    taken = False
    try:
        while True:
            left = cli.started + cfg.background_max_seconds - time.time()
            if left <= 0:
                log.info("claude conversation %s: set aside for %.0fs, ended", key, cfg.background_max_seconds)
                return
            try:
                line = await asyncio.wait_for(cli.queue.get(), timeout=left)
            except asyncio.TimeoutError:
                continue
            if line is None:
                return                      # the process ended by itself
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "system" or ev.get("subtype") in ("init", "task_notification"):
                cli.idle.clear()            # it has begun a turn of its own (a task ending starts one)
            for out in _digest(ev, turn, blocks, cfg, cli):
                if out["type"] == "background":
                    _write_down(cli, key)   # _digest has just taken the new list in
                if out["type"] not in ("done", "error"):
                    continue
                why, cli.notes = list(cli.notes), []
                try:
                    await on_wake(out, turn, why)
                except Exception:  # noqa: BLE001 - what it said must not end the watch
                    log.exception("claude conversation %s came back on its own and could not be kept", key)
                cli.last_woke = time.time()     # kept: a pick-up after a restart must not say it again
                if cli.tasks:
                    _write_down(cli, key)
                turn, blocks = ClaudeTurn(key), {}
                cli.idle.set()
                if not cli.tasks:
                    return                  # nothing left running: it can go
    except asyncio.CancelledError:
        taken = True                        # handed a message (_take_parked) or stopped: not ours to end
        raise
    finally:
        cli.idle.set()
        if not taken:
            if PARKED.get(key) is cli:
                PARKED.pop(key, None)
            if cli.tasks:
                kill_tree(cli.proc.pid)     # set aside too long: what it runs goes with it, not on unseen
            cli.kill()
            _cross_off(key)


class ClaudeTurn:
    """What one turn learned that the caller needs afterwards."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.model: Optional[str] = None
        self.init: dict[str, Any] = {}          # digest_init of the CLI's init event
        # The id of the last entry of this turn's that a later turn can be
        # branched at (sessions.branch_point): a finished answer or a tool's
        # result - never a tool call still waiting on its result, as a stopped
        # turn can end on. The stream's uuids are the transcript's own
        # (measured 24 Nov 2026).
        self.safe_uuid: Optional[str] = None
        self.tools: list[str] = []
        # Under the fold: [{"kind": "thinking", "text"} | {"kind": "tool", "id",
        # "tool", "summary", "ok"?, "result"?} | {"kind": "asked", ...}]
        self.steps: list[dict[str, Any]] = []
        # Pictures a tool sent back as image content (make_image's drawing):
        # [{"tool_use_id", "mime", "data": bytes}], for the caller to keep
        # in the chat - the tool's own copy is on another box.
        self.images: list[dict[str, Any]] = []

    def step(self, s: dict[str, Any]) -> None:
        if len(self.steps) < MAX_STEPS:
            self.steps.append(s)

    def say(self, text: str) -> None:
        """Words of the answer, kept in their place among the steps. Never
        capped by MAX_STEPS: they are the answer, not a note about it."""
        last = self.steps[-1] if self.steps else None
        if last is not None and last["kind"] == "said" and not last.get("closed"):
            last["text"] += text
        else:
            self.steps.append({"kind": "said", "text": text})

    def close_said(self) -> None:
        if self.steps and self.steps[-1]["kind"] == "said":
            self.steps[-1]["closed"] = True

    def said(self) -> list[dict[str, Any]]:
        return [s for s in self.steps if s["kind"] == "said" and s["text"].strip()]

    def answer(self, result: str) -> str:
        """The whole answer: every piece it said, in order. The CLI's result
        (the last piece) stands alone when nothing streamed, and is added if
        it somehow is not among what did."""
        parts = [s["text"].strip() for s in self.said()]
        if not parts:
            return result
        text = "\n\n".join(parts)
        if result and result.strip() and result.strip() not in text:
            self.say("")
            self.steps[-1]["text"] = result.strip()
            text += "\n\n" + result.strip()
        return text

    def final(self, result: str) -> str:
        """The last piece it said: the conclusion, without the working said
        on the way there ("Checking the real box..."). The CLI's result is
        that piece; the last words streamed, when it gives none."""
        if result and result.strip():
            return result.strip()
        said = self.said()
        return said[-1]["text"].strip() if said else ""

    def think(self, text: str) -> None:
        last = self.steps[-1] if self.steps else None
        if last is not None and last["kind"] == "thinking" and not last.get("closed"):
            if len(last["text"]) < MAX_THINKING:
                last["text"] = (last["text"] + text)[:MAX_THINKING]
        else:
            self.step({"kind": "thinking", "text": text[:MAX_THINKING]})

    def mark(self) -> dict[str, str]:
        """For the answer's meta: which CLI conversation it was and where a
        later turn can branch it (sessions.branch_point). Nothing when the
        turn never got as far as a safe entry."""
        return {"cli_session": self.session_id, "cli_at": self.safe_uuid} if self.safe_uuid else {}

    def kept_steps(self) -> list[dict[str, Any]]:
        """The steps as the receipt keeps them. A lone `said` at the very end
        is the answer in its usual place (the steps fold above it), so it is
        not kept twice; `said` steps are kept when the answer had words before
        or between its steps, which is when their places matter."""
        kept = [{k: v for k, v in s.items() if k != "closed"} for s in self.steps
                if s["kind"] not in ("thinking", "said") or s["text"].strip()]
        said = [i for i, s in enumerate(kept) if s["kind"] == "said"]
        if said == [len(kept) - 1]:
            kept.pop()
        return kept


MAX_TOOL_IMAGES = 8


def _result_images(content: Any, tool_use_id: Any) -> list[dict[str, Any]]:
    """The base64 image blocks in a tool_result's content, decoded."""
    out = []
    for c in content if isinstance(content, list) else []:
        src = c.get("source") if isinstance(c, dict) and c.get("type") == "image" else None
        if not isinstance(src, dict) or src.get("type") != "base64":
            continue
        try:
            data = base64.b64decode(src.get("data") or "", validate=True)
        except (ValueError, TypeError):
            continue
        if data:
            out.append({"tool_use_id": tool_use_id, "mime": src.get("media_type") or "", "data": data})
    return out


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _digest(ev: dict[str, Any], turn: ClaudeTurn, blocks: dict[Any, dict[str, Any]], cfg: ClaudeConfig,
            cli: Optional[Cli] = None) -> Iterator[dict[str, Any]]:
    """One line of the CLI's stream, as Symposium's events (none, for most).
    `blocks`: content block index -> what it is so far, across lines. A
    `done` or an `error` ends the turn; the process may go on (Cli)."""
    kind = ev.get("type")
    if kind in ("assistant", "user") and not ev.get("parent_tool_use_id") and isinstance(ev.get("uuid"), str):
        content = (ev.get("message") or {}).get("content")
        blocks_in = [c.get("type") for c in content if isinstance(c, dict)] if isinstance(content, list) else []
        if kind == "user" and "tool_result" in blocks_in:
            turn.safe_uuid = ev["uuid"]
        elif kind == "assistant" and "tool_use" not in blocks_in:
            turn.safe_uuid = ev["uuid"]
    if kind == "system" and ev.get("subtype") == "init":
        turn.model = ev.get("model")
        # The conversation this turn is in, as the CLI has it: the
        # one asked for, or - forked - a new one.
        if isinstance(ev.get("session_id"), str) and ev["session_id"]:
            turn.session_id = ev["session_id"]
        turn.init = digest_init(ev)
    elif kind == "system" and ev.get("subtype") == "background_tasks_changed" and cli is not None:
        # What it has running in the background, whole, each time it changes.
        cli.tasks = [t for t in ev.get("tasks") or [] if isinstance(t, dict)]
        for t in cli.tasks:
            if isinstance(t.get("task_id"), str):
                cli.task_seen.setdefault(t["task_id"], time.time())
        yield {"type": "background", "tasks": [str(t.get("description") or t.get("task_type") or "a task")[:200]
                                               for t in cli.tasks]}
    elif kind == "system" and ev.get("subtype") == "task_notification" and cli is not None:
        if ev.get("is_backgrounded") is not False and isinstance(ev.get("summary"), str) and ev["summary"]:
            cli.notes.append(ev["summary"][:300])
    elif kind == "stream_event" and not ev.get("parent_tool_use_id"):
        inner = ev.get("event") or {}
        block = inner.get("content_block") or {}
        delta = inner.get("delta") or {}
        at = inner.get("index")
        if inner.get("type") == "content_block_start":
            blocks[at] = {"type": block.get("type"), "id": block.get("id"),
                          "name": block.get("name"), "json": ""}
            if block.get("type") == "text":
                turn.close_said()               # a new block of words is a new piece
            if block.get("type") == "tool_use":
                name = block.get("name") or "a tool"
                if name not in QUIET_TOOLS:
                    turn.tools.append(name)
                    yield {"type": "tool_status", "tool": name}
        elif inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            turn.say(delta.get("text", ""))
            yield {"type": "chunk", "content": delta.get("text", "")}
        elif inner.get("type") == "content_block_delta" and delta.get("type") == "thinking_delta":
            turn.think(delta.get("thinking", ""))
            yield {"type": "thinking", "content": delta.get("thinking", "")}
        elif inner.get("type") == "content_block_delta" and delta.get("type") == "input_json_delta":
            if at in blocks:
                blocks[at]["json"] += delta.get("partial_json", "")
        elif inner.get("type") == "content_block_stop":
            b = blocks.pop(at, None) or {}
            if b.get("type") == "text":
                turn.close_said()
            if b.get("type") == "thinking" and turn.steps and turn.steps[-1]["kind"] == "thinking":
                turn.steps[-1]["closed"] = True
            elif b.get("type") == "tool_use" and (b.get("name") or "") not in QUIET_TOOLS:
                try:
                    given = json.loads(b["json"] or "{}")
                except ValueError:
                    given = {}
                # `sid`: whose transcript holds it in full (terminal.tool_step).
                # A chat's conversation can be started over, so the step says;
                # and a step opened while its turn still runs is not kept yet.
                call = {"id": b.get("id"), "tool": b.get("name") or "a tool",
                        "summary": summarize(b.get("name") or "", given, 200), "sid": turn.session_id}
                turn.step({"kind": "tool", **call})
                yield {"type": "tool_call", **call}
    elif kind == "user" and not ev.get("parent_tool_use_id"):
        content = (ev.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                continue
            got = _result_images(c.get("content"), c.get("tool_use_id"))
            turn.images += got[:max(0, MAX_TOOL_IMAGES - len(turn.images))]
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
            result = ev.get("result") or ""
            yield {"type": "done", "response": turn.answer(result), "final": turn.final(result),
                   "model": turn.model, "node": cfg.name,
                   "tool_rounds": max(0, int(ev.get("num_turns") or 1) - 1)}


async def events(cfg: ClaudeConfig, *, prompt: str, resume: Optional[str], model: str = "",
                 system_prompt: str = "", add_dirs: Optional[list[str]] = None,
                 allow: Optional[list[str]] = None,
                 turn: Optional[ClaudeTurn] = None,
                 permit: Optional[dict[str, str]] = None,
                 replace: bool = False, mode: Optional[str] = None,
                 own_tools: bool = True, cwd: str = "", fork: bool = False,
                 ephemeral: bool = False, on_wake: Optional[Wake] = None,
                 effort: str = "", thinking_display: str = "",
                 owner: Optional[dict[str, Any]] = None,
                 resume_at: Optional[str] = None) -> AsyncIterator[dict[str, Any]]:
    """One turn, as Symposium stream events (see the module docstring).
    Always ends with exactly one `done` or `error`. `permit` ({url, ticket})
    is where the CLI's permission asks go; without it, unlisted tools are
    refused as before. `cwd`: where this turn runs, over claude.cwd (a chat
    picked up from a terminal runs where its conversation was started).

    `on_wake`: this conversation may leave a task running behind its answer
    (claude.background). Then the process is not ended with the turn but set
    aside (PARKED) under the conversation's id; the next turn that resumes
    that conversation is handed to it rather than to a second process, and
    what it says when the task ends goes to `on_wake`. Without `on_wake` (a
    one-shot, a side copy) the process ends with its answer, as it always did."""
    turn = turn or ClaudeTurn(resume or str(uuid.uuid4()))
    blocks: dict[Any, dict[str, Any]] = {}      # content block index -> what it is so far
    where = cwd or cfg.cwd
    cli: Optional[Cli] = None
    kept = False                                # set aside rather than ended
    try:
        if resume and not fork and cfg.background:
            cli = await _take_parked(resume)
        if cli is None:
            cmd = argv(cfg, resume=resume, new_id=turn.session_id, model=model, system_prompt=system_prompt,
                       add_dirs=add_dirs, allow=allow, ask=bool(permit), replace=replace,
                       mode=mode, own_tools=own_tools, fork=fork, ephemeral=ephemeral,
                       effort=effort, thinking_display=thinking_display, resume_at=resume_at)
            cli = Cli(cmd, where and os.path.expanduser(where), _env(permit, cfg), stream=cfg.background,
                      ticket=(permit or {}).get("ticket"))
        cli.notes.clear()
        timer = cli.deadline(cfg.timeout_seconds)
        try:
            cli.send(prompt)
            while (line := await cli.queue.get()) is not None:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                ended = False
                for out in _digest(ev, turn, blocks, cfg, cli):
                    ended = ended or out["type"] in ("done", "error")
                    yield out
                if ended:
                    if on_wake is not None and cfg.background and not ephemeral and cli.tasks and cli.alive():
                        _park(cli, turn.session_id, cfg, on_wake, owner)
                        kept = True
                    elif not ephemeral:
                        _cross_off(turn.session_id)     # nothing of it left running
                    return
        finally:
            timer.cancel()
    except OSError as ex:
        yield {"type": "error", "error": "could not start claude", "detail": f"{type(ex).__name__}: {ex}"}
        return
    finally:
        # Ended with its answer - or by whoever was reading stopping (the
        # person's stop, the shell closed mid-answer) - unless it was set aside.
        if cli is not None and not kept:
            cli.kill()
    yield {"type": "error", "error": "claude stopped before answering",
           "detail": "".join(cli.stderr if cli is not None else [])[-STDERR_TAIL:].strip() or "no output"}


async def one_shot(cfg: ClaudeConfig, prompt: str, system_prompt: str = "", model: str = "") -> dict[str, Any]:
    """One question, one answer, no session kept: a fresh CLI session that is
    never resumed. For a small job in the chat's own voice - the picture
    panel's translate, which asks Claude to write a prompt in a model's
    style. Returns {ok, response, model, node} or {ok: false, error, detail}."""
    out: dict[str, Any] = {"ok": False, "error": "claude gave no answer"}
    async for ev in events(cfg, prompt=prompt, resume=None, model=model, system_prompt=system_prompt):
        if ev["type"] == "done":
            out = {"ok": True, "response": ev.get("response") or "", "model": ev.get("model"), "node": ev.get("node")}
        elif ev["type"] == "error":
            out = {"ok": False, "error": ev.get("error"), "detail": ev.get("detail")}
    return out


async def side_copy(cfg: ClaudeConfig, *, prompt: str, resume: str, **turn: Any) -> dict[str, Any]:
    """One question to a throwaway copy of conversation `resume`: forked, so
    it has all of that conversation in mind and the conversation never hears
    of it; ephemeral, so the copy is not saved and no hooks run; and it is
    stopped the moment it reaches for a tool, before the tool runs - a side
    copy is asked for words, and it has the whole house's tools in reach.
    `turn`: what a turn in that chat is given (system prompt, model, folders,
    where it runs), so the copy is the same conversation and the CLI's cache
    of it is reused. Returns {ok, response, model, node} or {ok: false, ...}.

    Checked on the real CLI (2.1.326, 17 Nov 2026): the copy answered from
    the conversation, got a new id, wrote no transcript, ran no hooks."""
    out: dict[str, Any] = {"ok": False, "error": "claude gave no answer"}
    spoken = events(cfg, prompt=prompt, resume=resume, fork=True, ephemeral=True, **turn)
    async with aclosing(spoken):
        async for ev in spoken:
            if ev["type"] == "tool_status":
                return {"ok": False, "error": "the side copy reached for a tool instead of answering",
                        "detail": f"{ev.get('tool') or 'a tool'} - stopped before it ran"}
            if ev["type"] == "done":
                out = {"ok": True, "response": ev.get("final") or ev.get("response") or "",
                       "model": ev.get("model"), "node": ev.get("node")}
            elif ev["type"] == "error":
                out = {"ok": False, "error": ev.get("error"), "detail": ev.get("detail")}
    return out
