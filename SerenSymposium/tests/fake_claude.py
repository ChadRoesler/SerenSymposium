"""A stand-in for the claude CLI: reads the prompt from stdin and prints
stream-json in the shapes the real one does (captured from claude 2.1),
so the claude voice is tested without a login, a network or a bill.

FAKE_CLAUDE picks what happens:  ok (default) | fail | die
FAKE_CLAUDE_LOG, when set, gets one JSON line per run: {argv, prompt}.
FAKE_CLAUDE_REPLY, when set, is the answer instead of "Hello there".
FAKE_CLAUDE_SAVE=name: the answer first writes a small PNG of that name into
the first --add-dir (as Claude saving an image into the chat would).
FAKE_CLAUDE_DRAW=1: a make_image call whose result is text AND an image block
(how an MCP tool's picture reaches the CLI), naming the file it saved.
FAKE_CLAUDE_ASK={"tool_name", "input"}: as the real CLI does with a tool not on
the list, start the --permission-prompt-tool from --mcp-config, ask it over
stdio, and answer "asked: allow" or "asked: deny" with what came back.
FAKE_CLAUDE_WAIT=path: after thinking, wait (up to 20s) until that file exists
before answering - a turn a test can hold open, then let finish.
FAKE_CLAUDE_SAY_FIRST=words: say them (a text block) before the first tool, as
Claude does when it says what it is about to look at.
"""
import base64
import json
import os
import subprocess
import sys
import time
import uuid

# The smallest PNG there is: one transparent pixel.
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def out(d):
    print(json.dumps(d), flush=True)


def event(inner, parent=None):
    out({"type": "stream_event", "event": inner, "parent_tool_use_id": parent})


def ask_permission(args, ask):
    """What the CLI does: start the permission tool it was given, as an MCP
    server over stdio, and call it."""
    server = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["symposium"]
    tool = args[args.index("--permission-prompt-tool") + 1]
    assert tool == "mcp__symposium__permit", tool
    p = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, encoding="utf-8")
    for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "permit", "arguments": {**ask, "tool_use_id": "toolu_fake"}}}):
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
    for line in p.stdout:
        reply = json.loads(line)
        if reply.get("id") == 2:
            p.stdin.close()
            p.wait(timeout=10)
            return json.loads(reply["result"]["content"][0]["text"])
    raise SystemExit("the permission tool never answered")


def main():
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    if log := os.environ.get("FAKE_CLAUDE_LOG"):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": args, "prompt": prompt}) + "\n")
    sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
    mode = os.environ.get("FAKE_CLAUDE", "ok")

    out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model",
         "permissionMode": "default", "cwd": os.getcwd(), "claude_code_version": "9.9.9",
         "apiKeySource": "none", "uuid": "not-for-keeping",
         "tools": ["Task", "Bash", "Read", "ToolSearch", "mcp__workbench__get_time", "mcp__workbench__recall",
                   "mcp__claude_ai_Gmail__search"],
         "mcp_servers": [{"name": "workbench", "status": "connected", "source": "user"},
                         {"name": "claude.ai Gmail", "status": "needs-auth", "source": "claudeai"}],
         "skills": ["dataviz"], "agents": ["Explore"], "slash_commands": ["dataviz", "review"], "plugins": [],
         "memory_paths": {"auto": os.environ.get("FAKE_CLAUDE_MEMORY", "")}})
    if mode == "die":
        print("boom: the fake fell over", file=sys.stderr, flush=True)
        sys.exit(2)
    if mode == "fail":
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "errors": ["No conversation found with session ID: " + sid], "session_id": sid})
        sys.exit(1)

    event({"type": "message_start", "message": {}})
    if first := os.environ.get("FAKE_CLAUDE_SAY_FIRST"):
        event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": first}})
        event({"type": "content_block_stop", "index": 0})
    for name in ("ToolSearch", "mcp__workbench__get_time"):
        tid = "toolu_" + uuid.uuid4().hex[:12]
        event({"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}})
        event({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": '{"zone": '}})
        event({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": '"UTC"}'}})
        event({"type": "content_block_stop", "index": 0})
        out({"type": "user", "parent_tool_use_id": None,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                      "content": [{"type": "text", "text": "12:00 UTC"}]}]}})
    if os.environ.get("FAKE_CLAUDE_DRAW"):
        tid = "toolu_" + uuid.uuid4().hex[:12]
        event({"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": tid, "name": "mcp__wren-workbench__make_image", "input": {}}})
        event({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": '{"prompt": "a wren"}'}})
        event({"type": "content_block_stop", "index": 0})
        said = json.dumps({"ok": True, "saved": ["/mnt/nvme/x/gallery/2026-10-15/023502-42-a-wren.png"], "shown": 1})
        out({"type": "user", "parent_tool_use_id": None,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": [
                 {"type": "text", "text": said},
                 {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": base64.b64encode(PNG).decode()}}]}]}})
    event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
          parent="toolu_sub")
    event({"type": "content_block_delta", "index": 0,
           "delta": {"type": "text_delta", "text": "a subagent talking"}}, parent="toolu_sub")
    event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    event({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    event({"type": "content_block_stop", "index": 0})
    if gate := os.environ.get("FAKE_CLAUDE_WAIT"):
        deadline = time.time() + 20
        while not os.path.exists(gate) and time.time() < deadline:
            time.sleep(0.05)
    if save := os.environ.get("FAKE_CLAUDE_SAVE"):
        with open(os.path.join(args[args.index("--add-dir") + 1], save), "wb") as f:
            f.write(PNG)
    reply = os.environ.get("FAKE_CLAUDE_REPLY", "Hello there")
    if (want := os.environ.get("FAKE_CLAUDE_ASK")) and "--permission-prompt-tool" in args:
        reply = f"asked: {ask_permission(args, json.loads(want))['behavior']}"
    for word in (reply[:5], reply[5:]):
        event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": word}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply,
         "num_turns": 3, "session_id": sid})


if __name__ == "__main__":
    main()
