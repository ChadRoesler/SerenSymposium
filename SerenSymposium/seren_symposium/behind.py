"""
seren_symposium.behind
════════════════════════════════════════════════════════════════════════

What the chats left running in the background, kept on disk so it outlives
Symposium (23 Nov 2026).

A conversation can leave a task running behind its answer (claude.background;
claude_voice keeps the process aside, PARKED, and reads it). That registry
lived in memory only, so a crash lost it, and a restart - every upgrade
pulled - killed the process holding the task on the way out. Either way the
work went on or died unseen, and nobody was told how it ended. Chad, 23 Nov:
"what happens if symposium crashes or goes down, while a long running
background task is ongoing?" and, of letting tasks outlive a restart and
picking them up after: "your call is quite literally what i had in mind".

So now:

  LEDGER   every set-aside conversation is written down here as it is set
           aside and as its tasks change: the chat, the seat, the CLI's
           conversation id and process id, each task with when it began.
           Taken off when nothing of it is left running, or when it is
           stopped (the chip's ×).
  LET GO   a clean shutdown no longer kills those processes: they are let
           go, and run on. (Measured on the real CLI, 23 Nov: its parent
           gone, the CLI ran its task to the end, reported into its own
           transcript, and exited.)
  PICK UP  on the next start, what the ledger holds is shown as running
           again, from before the restart, and watched: when the CLI is gone
           and its tasks have ended, what it said about them (its own turn,
           in its transcript) is put in the chat as a wake, as if Symposium
           had never left. A CLI that died before it could say anything is
           asked, by resuming its conversation with a short note.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

FILE_NAME = "background.json"
# What a live pid's program must be called to count as ours (pid_alive).
PROCESS_NAMES = ("claude",)
# How often pick_up looks at what it is waiting on, in seconds.
PICKUP_EVERY = 10.0
EXITED = re.compile(r"\[exited with code (-?\d+)\]\s*$")
TAIL_BYTES = 4096


class Ledger:
    """{conversation id: entry} in one small JSON file, written whole each
    time (it holds a handful of entries). Safe from any thread."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def entries(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return self._read()

    def put(self, key: str, entry: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            data[key] = entry
            self._write(data)

    def drop(self, key: str) -> None:
        with self.lock:
            data = self._read()
            if data.pop(key, None) is not None:
                self._write(data)

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".background-", suffix=".json", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not write %s", self.path)


def pid_alive(pid: Any) -> bool:
    """Whether process `pid` is still running AND is a claude: a pid is
    handed out again once its process is gone, and a stranger's must not
    pass for ours."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
                return False
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return any(n in os.path.basename(buf.value).lower() for n in PROCESS_NAMES)
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").lower()
        return any(n in cmdline for n in PROCESS_NAMES)
    except OSError:
        return True


def task_ended(conversation: str, task_id: str, dirs: list[Path]) -> Optional[dict[str, Any]]:
    """{code, tail} when task `task_id`'s output file says it exited, None
    while it runs or when there is no file to say (an agent task)."""
    for root in dirs:
        for p in root.glob(f"*/{conversation}/tasks/{task_id}.output") if root.is_dir() else []:
            try:
                size = p.stat().st_size
                with p.open("rb") as f:
                    f.seek(max(0, size - TAIL_BYTES))
                    tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            m = EXITED.search(tail)
            return {"code": int(m.group(1)), "tail": tail} if m else None
    return None


def told_after(transcript: Optional[Path], since: float) -> list[str]:
    """What the CLI said in each turn it began on its own when a task ended,
    after `since`: for every task notification in its transcript written
    later than that, the last thing it said before the next one. (Measured
    23 Nov 2026: the notification is a `user` record holding
    <task-notification>, then the CLI's turn - tools perhaps, then words.)
    Empty when it never got to say anything."""
    if transcript is None:
        return []
    from .terminal import _at, _records
    told: list[str] = []
    in_turn, said = False, ""
    for r in _records(transcript):
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = msg.get("content")
        text = content if isinstance(content, str) else " ".join(
            str(c.get("text") or "") for c in content if isinstance(c, dict) and c.get("type") == "text") \
            if isinstance(content, list) else ""
        if r.get("type") == "user" and "<task-notification>" in text:
            if in_turn and said:
                told.append(said)
            in_turn, said = (_at(r) or 0) > since, ""
        elif r.get("type") == "assistant" and in_turn and text.strip():
            said = text.strip()
    if in_turn and said:
        told.append(said)
    return told


async def pick_up(ledger: Ledger, let_go: dict[str, dict[str, Any]], transcript_of, on_told, on_silent,
                  every: Optional[float] = None) -> None:
    """At start: what the ledger holds ran on while Symposium was away (let
    go, or a crash). Each is shown as running again (into `let_go`, which
    claude_voice.parked reads) and watched until its CLI is gone; then what it
    said is handed to `on_told(entry, [answers])`, or, if it said nothing,
    `on_silent(entry)` - and it is crossed off. Runs until none are left."""
    for key, entry in ledger.entries().items():
        let_go[key] = {**entry, "let_go": entry.get("let_go") or entry.get("written") or entry.get("started")}
    if let_go:
        log.info("picking up %d conversation(s) left running before this start", len(let_go))
    import asyncio
    while let_go:
        for key, entry in list(let_go.items()):
            if pid_alive(entry.get("pid")):
                continue                        # still at it, or still saying how it went
            let_go.pop(key, None)
            since = max(float(entry.get("started") or 0), float(entry.get("last_woke") or 0))
            try:
                told = await asyncio.to_thread(told_after, transcript_of(key), since)
                if told:
                    await on_told(entry, told)
                else:
                    await on_silent(entry)
            except Exception:  # noqa: BLE001 - one that cannot be told must not stop the rest
                log.exception("could not tell chat %s how conversation %s ended", entry.get("chat"), key)
            finally:
                ledger.drop(key)
        if let_go:
            await asyncio.sleep(every or PICKUP_EVERY)
