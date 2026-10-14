"""
seren_symposium.permit
════════════════════════════════════════════════════════════════════════

    python -m seren_symposium.permit

The claude voice's permission prompt, in the window.

With -p nobody is at a terminal to answer "may I?", so a tool that is not on
the allowed list used to be refused outright - the folder Claude could not
read, 14 Oct 2026. The CLI can instead ask a tool of ours
(--permission-prompt-tool): this module is that tool, a stdio MCP server the
CLI starts for the turn. Each ask goes to Symposium over loopback, shows in
the chat as Allow once / Always / Deny, and the answer comes back as the
CLI wants it:

    {"behavior": "allow", "updatedInput": {...}}
    {"behavior": "deny", "message": "..."}

Where to ask and the turn's ticket come from the environment Symposium gives
the CLI (which hands it on to us) - never the command line. Standard library
only: it starts once per turn, and fast.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

URL_ENV, TICKET_ENV = "SYMPOSIUM_PERMIT_URL", "SYMPOSIUM_PERMIT_TICKET"
# Longer than anyone takes to read a prompt; Symposium answers "deny" first.
WAIT_SECONDS = 1800

TOOL = {
    "name": "permit",
    "description": "Asks the person in the Symposium window whether a tool may run.",
    "inputSchema": {"type": "object",
                    "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                                   "tool_use_id": {"type": "string"}},
                    "required": ["tool_name", "input"]},
}


def ask(tool_name: str, tool_input: Any, tool_use_id: str = "") -> dict[str, Any]:
    url, ticket = os.environ.get(URL_ENV, ""), os.environ.get(TICKET_ENV, "")
    if not url or not ticket:
        return {"behavior": "deny", "message": "Symposium did not say where to ask, so nothing was asked."}
    body = json.dumps({"ticket": ticket, "tool_name": tool_name, "input": tool_input,
                       "tool_use_id": tool_use_id}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=WAIT_SECONDS) as r:
            out = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as ex:
        return {"behavior": "deny", "message": f"Symposium could not be asked ({type(ex).__name__}); refused."}
    if out.get("behavior") == "allow":
        return {"behavior": "allow", "updatedInput": tool_input if isinstance(tool_input, dict) else {}}
    return {"behavior": "deny", "message": str(out.get("message") or "Refused in the Symposium window.")}


def handle(msg: dict[str, Any]) -> "dict[str, Any] | None":
    """One JSON-RPC message in, the reply out (None for a notification)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        result: Any = {"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "symposium", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "permit":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "no such tool"}}
        a = params.get("arguments") or {}
        decision = ask(str(a.get("tool_name") or ""), a.get("input") or {}, str(a.get("tool_use_id") or ""))
        result = {"content": [{"type": "text", "text": json.dumps(decision)}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
