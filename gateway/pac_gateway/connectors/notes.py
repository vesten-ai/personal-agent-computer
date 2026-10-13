"""Example connector: a notebook the agent can write to and read back, kept on the gateway.

It exists to show the connector interface end to end: an MCP server (stdio, stdlib only) that the
gateway runs outside the VM. A real connector (mail, calendar, a CRM) has the same shape and keeps
its credentials in the gateway's environment.

  [connectors.notes]
  command = ["/opt/pac/gateway-venv/bin/python", "-m", "pac_gateway.connectors.notes"]
  env = { NOTES_FILE = "/var/lib/pac/notes.json" }
"""

import json
import os
import sys
import time

FILE = os.environ.get("NOTES_FILE", "notes.json")
TOOLS = [
    {"name": "add_note", "description": "Save a short note for later.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "list_notes", "description": "List all saved notes, oldest first.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def load() -> list[dict]:
    try:
        with open(FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save(notes: list[dict]) -> None:
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(notes, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def handle(method: str, params: dict):
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "pac-notes", "version": "0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        if name == "add_note":
            notes = load()
            notes.append({"at": time.strftime("%Y-%m-%d %H:%M"), "text": str(args["text"])})
            save(notes)
            return {"content": [{"type": "text", "text": f"Saved note {len(notes)}."}]}
        if name == "list_notes":
            lines = [f"{i}. [{n['at']}] {n['text']}" for i, n in enumerate(load(), 1)] or ["No notes yet."]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    raise KeyError(method)


def main() -> None:
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notifications
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg["method"], msg.get("params") or {})}
        except KeyError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"unknown method {e}"}}
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
