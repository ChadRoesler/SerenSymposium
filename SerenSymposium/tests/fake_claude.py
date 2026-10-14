"""A stand-in for the claude CLI: reads the prompt from stdin and prints
stream-json in the shapes the real one does (captured from claude 2.1),
so the claude voice is tested without a login, a network or a bill.

FAKE_CLAUDE picks what happens:  ok (default) | fail | die
FAKE_CLAUDE_LOG, when set, gets one JSON line per run: {argv, prompt}.
"""
import json
import os
import sys
import uuid


def out(d):
    print(json.dumps(d), flush=True)


def event(inner, parent=None):
    out({"type": "stream_event", "event": inner, "parent_tool_use_id": parent})


def main():
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    if log := os.environ.get("FAKE_CLAUDE_LOG"):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": args, "prompt": prompt}) + "\n")
    sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
    mode = os.environ.get("FAKE_CLAUDE", "ok")

    out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model",
         "permissionMode": "default", "mcp_servers": []})
    if mode == "die":
        print("boom: the fake fell over", file=sys.stderr, flush=True)
        sys.exit(2)
    if mode == "fail":
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "errors": ["No conversation found with session ID: " + sid], "session_id": sid})
        sys.exit(1)

    event({"type": "message_start", "message": {}})
    for name in ("ToolSearch", "mcp__workbench__get_time"):
        event({"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": uuid.uuid4().hex, "name": name, "input": {}}})
        event({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": "{}"}})
    event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
          parent="toolu_sub")
    event({"type": "content_block_delta", "index": 0,
           "delta": {"type": "text_delta", "text": "a subagent talking"}}, parent="toolu_sub")
    event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    event({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    for word in ("Hello", " there"):
        event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": word}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello there"}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": "Hello there",
         "num_turns": 3, "session_id": sid})


if __name__ == "__main__":
    main()
