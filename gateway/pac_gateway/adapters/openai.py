"""OpenAI Responses API adapter: the GA `computer` tool, or the same actions as a function tool.

Wire rules this follows (developers.openai.com computer-use guide, 2026-10):
  - tools entry is {"type": "computer"}: no display size; coordinates are in screenshot pixels.
  - the model returns `computer_call` items with a batched `actions` list (older models: a single
    `action`); run them in order, then answer with one `computer_call_output` holding a screenshot.
  - stateless mode (store=false): every output item, reasoning items with encrypted_content
    included, is appended to the input verbatim and the whole history is resent.
"""

import base64
import json
import os
import re
import time

from ..computer import Computer, ComputerError
from ..http import post_json

# The model emits upper-case key names; computerd takes xdotool names.
KEYS = {"ENTER": "Return", "RETURN": "Return", "ESC": "Escape", "ESCAPE": "Escape", "TAB": "Tab",
        "SPACE": "space", "BACKSPACE": "BackSpace", "DELETE": "Delete", "DEL": "Delete",
        "HOME": "Home", "END": "End", "PAGEUP": "Page_Up", "PAGEDOWN": "Page_Down",
        "UP": "Up", "ARROWUP": "Up", "DOWN": "Down", "ARROWDOWN": "Down", "LEFT": "Left",
        "ARROWLEFT": "Left", "RIGHT": "Right", "ARROWRIGHT": "Right", "CTRL": "ctrl", "CONTROL": "ctrl",
        "SHIFT": "shift", "ALT": "alt", "OPTION": "alt", "META": "super", "CMD": "super",
        "COMMAND": "super", "SUPER": "super", "WIN": "super", "INSERT": "Insert", "CAPSLOCK": "Caps_Lock"}


def _key(k: str) -> str:
    u = k.upper()
    if u in KEYS:
        return KEYS[u]
    if re.fullmatch(r"F\d{1,2}", u):
        return u
    return k.lower() if len(k) == 1 else k


_ACTION = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["click", "double_click", "drag", "move", "scroll", "keypress",
                                            "type", "wait", "screenshot"]},
        "x": {"type": "integer"}, "y": {"type": "integer"},
        "button": {"type": "string", "enum": ["left", "right", "wheel", "back", "forward"]},
        "keys": {"type": "array", "items": {"type": "string"},
                 "description": "keypress: the chord, e.g. [\"CTRL\", \"S\"]; mouse actions: modifiers held"},
        "text": {"type": "string"},
        "scroll_x": {"type": "integer"}, "scroll_y": {"type": "integer"},
        "path": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "integer"},
                                                                              "y": {"type": "integer"}},
                                            "required": ["x", "y"]}},
    },
    "required": ["type"],
}
FUNCTION_TOOL = {
    "type": "function",
    "name": "computer",
    "description": "Operate the Linux desktop. Runs the actions in order, then returns a screenshot of the "
                   "whole screen. Coordinates are pixels of that screenshot. Use [{\"type\": \"screenshot\"}] "
                   "to just look. scroll_x/scroll_y are in pixels (100 = one wheel notch).",
    "parameters": {"type": "object", "properties": {"actions": {"type": "array", "items": _ACTION}},
                   "required": ["actions"]},
}


def _clicks(delta) -> int:
    if not delta:
        return 0
    n = max(1, abs(round(delta / 100)))
    return n if delta > 0 else -n


def execute(computer: Computer, a: dict) -> str:
    t = a["type"]
    mods = [_key(k) for k in a.get("keys") or []] if t != "keypress" else []
    if t == "click":
        computer.call("click", x=a["x"], y=a["y"], button=a.get("button", "left"), modifiers=mods)
    elif t == "double_click":
        computer.call("click", x=a["x"], y=a["y"], count=2, modifiers=mods)
    elif t == "drag":
        computer.call("drag", path=[[p["x"], p["y"]] for p in a["path"]], modifiers=mods)
    elif t == "move":
        computer.call("move", x=a["x"], y=a["y"])
    elif t == "scroll":
        computer.call("scroll", x=a.get("x"), y=a.get("y"), dx=_clicks(a.get("scroll_x")),
                      dy=_clicks(a.get("scroll_y")), modifiers=mods)
    elif t == "keypress":
        computer.call("key", keys=[_key(k) for k in a["keys"]])
    elif t == "type":
        computer.call("type", text=a["text"])
    elif t == "wait":
        computer.call("wait", seconds=2)
    elif t == "screenshot":
        pass  # every call is answered with a fresh screenshot anyway
    else:
        raise ComputerError(f"unsupported computer action {t!r}")
    return describe(a)


def describe(a: dict) -> str:
    """One readable line per action, for the chat transcript."""
    t = a.get("type")
    mods = "+".join(_key(k) for k in a.get("keys") or []) if t != "keypress" else ""
    held = f" with {mods}" if mods else ""
    if t == "click":
        return f"click {a.get('button', 'left')} at ({a.get('x')}, {a.get('y')}){held}"
    if t == "double_click":
        return f"double-click at ({a.get('x')}, {a.get('y')}){held}"
    if t == "drag":
        p = a.get("path") or [{}]
        return f"drag ({p[0].get('x')}, {p[0].get('y')}) to ({p[-1].get('x')}, {p[-1].get('y')}){held}"
    if t == "move":
        return f"move to ({a.get('x')}, {a.get('y')})"
    if t == "scroll":
        return f"scroll ({a.get('scroll_x', 0)}, {a.get('scroll_y', 0)}) at ({a.get('x')}, {a.get('y')}){held}"
    if t == "keypress":
        return "key " + "+".join(_key(k) for k in a.get("keys") or [])
    if t == "type":
        return f"type {a.get('text', '')!r}"
    if t == "screenshot":
        return "look at the screen"
    return t or "?"


class OpenAIComputerAgent:
    def __init__(self, model: str = "gpt-6.1-sol", effort: str = "medium", api_key: str | None = None,
                 system: str = "", log=print, tool_mode: str = "native", on_event=None, should_stop=None,
                 base_url: str | None = None, tools: list[dict] | None = None, call_tool=None):
        self.model, self.effort, self.system, self.log = model, effort, system, log
        self.on_event, self.should_stop = on_event, should_stop
        self.tool_mode = tool_mode
        # Extra tools (connectors): [{"name", "description", "input_schema"}], run by call_tool(name, args) -> str.
        self.extra_tools, self.call_tool = tools or [], call_tool
        self.api = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/") + "/responses"
        self.cache_key = f"pac-{os.getpid()}-{int(time.time())}"
        self.api_key = api_key or os.environ["OPENAI_API_KEY"]
        self.items: list[dict] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

    def state(self) -> dict:
        return {"items": self.items, "usage": self.usage, "cache_key": self.cache_key}

    def load(self, state: dict) -> None:
        self.items = state.get("items", [])
        self.usage.update(state.get("usage", {}))
        self.cache_key = state.get("cache_key", self.cache_key)

    def _emit(self, kind: str, data) -> None:
        if self.on_event:
            self.on_event(kind, data)

    def _request(self) -> dict:
        body = {
            "model": self.model,
            "tools": ([{"type": "computer"}] if self.tool_mode == "native" else [FUNCTION_TOOL])
            + [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}
               for t in self.extra_tools],
            "input": self.items,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": self.effort, "summary": "auto"},
            # Stateless requests resend the whole history; a stable key lets the provider reuse its prefix cache.
            "prompt_cache_key": self.cache_key,
        }
        if self.system:
            body["instructions"] = self.system
        return post_json(self.api, {"authorization": f"Bearer {self.api_key}"}, body)

    def run(self, task: str, computer: Computer, max_steps: int = 40) -> dict:
        self.items.append({"role": "user", "content": [{"type": "input_text", "text": task}]})
        steps = []
        for step in range(1, max_steps + 1):
            if self.should_stop and self.should_stop():
                return {"stop_reason": "interrupted", "final": "", "steps": steps, "usage": self.usage}
            t0 = time.time()
            resp = self._request()
            dt = time.time() - t0
            u = resp.get("usage") or {}
            self.usage["input_tokens"] += u.get("input_tokens", 0)
            self.usage["output_tokens"] += u.get("output_tokens", 0)
            self.usage["cached_tokens"] += (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
            output = resp.get("output", [])
            self.items.extend(output)  # verbatim: reasoning (encrypted), messages, computer calls
            for o in output:
                if o["type"] == "reasoning":
                    for s in o.get("summary") or []:
                        self.log(f"  [{step}] reasoning: {s.get('text', '')[:200]}")
                        self._emit("thinking", s.get("text", ""))
                elif o["type"] == "message":
                    for c in o.get("content") or []:
                        if c.get("type") == "output_text":
                            self.log(f"  [{step}] text: {c['text'][:300]}")
                            self._emit("text", c["text"])
            calls = [o for o in output if o["type"] == "computer_call"
                     or (o["type"] == "function_call" and o.get("name") == "computer")]
            tool_calls = [o for o in output if o["type"] == "function_call" and o.get("name") != "computer"]
            if not calls and not tool_calls:
                final = "".join(c.get("text", "") for o in output if o["type"] == "message"
                                for c in o.get("content") or [] if c.get("type") == "output_text")
                return {"stop_reason": resp.get("status"), "final": final, "steps": steps, "usage": self.usage,
                        "model_used": resp.get("model")}
            t1 = time.time()
            actions = []
            for call in tool_calls:
                try:
                    args = json.loads(call.get("arguments") or "{}")
                    out = self.call_tool(call["name"], args) if self.call_tool else "error: no such tool"
                    actions.append(f"{call['name']} {json.dumps(args, ensure_ascii=False)[:200]}")
                except Exception as e:
                    out = f"error: {e}"
                    actions.append(f"{call['name']} failed: {e}")
                self.items.append({"type": "function_call_output", "call_id": call["call_id"], "output": str(out)})
            for call in calls:
                if call["type"] == "function_call":
                    try:
                        batch = json.loads(call.get("arguments") or "{}").get("actions") or []
                    except json.JSONDecodeError as e:
                        batch, actions = [], actions + [f"bad arguments: {e}"]
                else:
                    batch = call.get("actions") or ([call["action"]] if call.get("action") else [])
                for a in batch:
                    try:
                        actions.append(execute(computer, a))
                    except Exception as e:
                        # The tool has no error result; the screenshot below shows the model what happened.
                        actions.append(f"{describe(a)} failed: {e}")
                        break
                checks = call.get("pending_safety_checks") or []
                if checks:
                    # Spike only: a real gateway pauses here and asks the person before acknowledging.
                    self.log(f"  [{step}] safety checks auto-acknowledged: {[c.get('code') for c in checks]}")
                png = computer.screenshot()
                self._emit("screenshot", png)
                shot = base64.b64encode(png).decode()
                if call["type"] == "function_call":
                    self.items.append({"type": "function_call_output", "call_id": call["call_id"], "output": [
                        {"type": "input_text", "text": "Done. Screenshot after the actions:"},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{shot}"}]})
                    continue
                out = {"type": "computer_call_output", "call_id": call["call_id"],
                       "output": {"type": "computer_screenshot", "image_url": f"data:image/png;base64,{shot}",
                                  "detail": "original"}}
                if checks:
                    out["acknowledged_safety_checks"] = checks
                self.items.append(out)
            self._emit("actions", actions)
            steps.append({"step": step, "model_s": round(dt, 2), "exec_s": round(time.time() - t1, 2),
                          "actions": actions})
            self.log(f"  [{step}] model {dt:.1f}s, {len(actions)} action(s): " + " | ".join(x[:120] for x in actions))
        return {"stop_reason": "max_steps", "final": "", "steps": steps, "usage": self.usage}
