"""Connectors: MCP servers that run on the gateway, outside every agent VM.

The agent's model sees each connector tool as an ordinary tool named `<connector>__<tool>`. Calls go
from the gateway to the MCP server, so connector credentials (OAuth tokens, API keys) live with the
gateway and never enter a VM. v1 speaks MCP over stdio; the client is deliberately small.

Config (gateway.toml):

  [connectors.notes]
  command = ["python3", "-m", "pac_connectors.notes"]
  env = { NOTES_FILE = "/var/lib/pac/notes.json" }      # literal values
  secret_env = { NOTES_TOKEN = "PAC_NOTES_TOKEN" }     # copied from the gateway's environment
"""

import itertools
import json
import os
import re
import subprocess
import threading

PROTOCOL = "2025-06-18"


class MCPError(RuntimeError):
    pass


class StdioMCPClient:
    def __init__(self, name: str, command: list[str], env: dict[str, str] | None = None):
        self.name, self.command, self.env = name, command, env or {}
        self.proc: subprocess.Popen | None = None
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def _start(self) -> None:
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                     env={**os.environ, **self.env})
        self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "pac-gateway", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict) -> dict:
        rid = next(self.ids)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise MCPError(f"connector {self.name} exited")
            msg = json.loads(line)
            if msg.get("id") != rid:
                continue  # notifications and anything else we don't wait for
            if "error" in msg:
                raise MCPError(f"{self.name}: {msg['error'].get('message')}")
            return msg.get("result") or {}

    def call(self, method: str, params: dict) -> dict:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            return self._request(method, params)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class ConnectorHub:
    """All configured connectors, their tools listed once and cached."""

    def __init__(self, connectors: dict[str, dict]):
        self.clients: dict[str, StdioMCPClient] = {}
        for name, c in connectors.items():
            env = dict(c.get("env", {}))
            for var, source in c.get("secret_env", {}).items():
                env[var] = os.environ.get(source, "")
            self.clients[name] = StdioMCPClient(name, c["command"], env)
        self._tools: list[dict] | None = None
        self._route: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _tool_name(connector: str, tool: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", f"{connector}__{tool}")[:64]

    def tools(self) -> list[dict]:
        """Neutral tool definitions: [{"name", "description", "input_schema"}]."""
        if self._tools is None:
            tools = []
            for cname, client in self.clients.items():
                try:
                    listed = client.call("tools/list", {}).get("tools", [])
                except Exception:
                    continue  # a broken connector shouldn't take the agent down
                for t in listed:
                    name = self._tool_name(cname, t["name"])
                    self._route[name] = (cname, t["name"])
                    tools.append({"name": name, "description": f"[{cname}] {t.get('description', '')}",
                                  "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}})
            self._tools = tools
        return self._tools

    def call_tool(self, name: str, args: dict) -> str:
        if name not in self._route:
            raise MCPError(f"no connector tool {name}")
        cname, tool = self._route[name]
        result = self.clients[cname].call("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise MCPError(text or "tool reported an error")
        return text

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
