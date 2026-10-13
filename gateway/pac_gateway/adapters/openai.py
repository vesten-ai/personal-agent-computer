"""OpenAI Responses API adapter: the GA `computer` tool.

Wire rules this follows (developers.openai.com computer-use guide, 2026-10):
  - tools entry is {"type": "computer"}: no display size; coordinates are in screenshot pixels.
  - the model returns `computer_call` items with a batched `actions` list (older models: a single
    `action`); run them in order, then answer with one `computer_call_output` holding a screenshot.
  - stateless mode (store=false): every output item, reasoning items with encrypted_content
    included, is appended to the input verbatim and the whole history is resent.
"""

import base64
import os
import re
import time

from ..computer import Computer, ComputerError
from ..http import post_json

API = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses"

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
    return f"{t} {({k: v for k, v in a.items() if k != 'type'})}"


class OpenAIComputerAgent:
    def __init__(self, model: str = "gpt-6.1-sol", effort: str = "medium", api_key: str | None = None,
                 system: str = "", log=print):
        self.model, self.effort, self.system, self.log = model, effort, system, log
        self.api_key = api_key or os.environ["OPENAI_API_KEY"]
        self.items: list[dict] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

    def _request(self) -> dict:
        body = {
            "model": self.model,
            "tools": [{"type": "computer"}],
            "input": self.items,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": self.effort, "summary": "auto"},
        }
        if self.system:
            body["instructions"] = self.system
        return post_json(API, {"authorization": f"Bearer {self.api_key}"}, body)

    def run(self, task: str, computer: Computer, max_steps: int = 40) -> dict:
        self.items.append({"role": "user", "content": [{"type": "input_text", "text": task}]})
        steps = []
        for step in range(1, max_steps + 1):
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
                elif o["type"] == "message":
                    for c in o.get("content") or []:
                        if c.get("type") == "output_text":
                            self.log(f"  [{step}] text: {c['text'][:300]}")
            calls = [o for o in output if o["type"] == "computer_call"]
            if not calls:
                final = "".join(c.get("text", "") for o in output if o["type"] == "message"
                                for c in o.get("content") or [] if c.get("type") == "output_text")
                return {"stop_reason": resp.get("status"), "final": final, "steps": steps, "usage": self.usage,
                        "model_used": resp.get("model")}
            t1 = time.time()
            actions = []
            for call in calls:
                for a in call.get("actions") or ([call["action"]] if call.get("action") else []):
                    try:
                        actions.append(execute(computer, a))
                    except Exception as e:
                        # The tool has no error result; the screenshot below shows the model what happened.
                        actions.append(f"{a.get('type')} FAILED: {e}")
                        break
                checks = call.get("pending_safety_checks") or []
                if checks:
                    # Spike only: a real gateway pauses here and asks the person before acknowledging.
                    self.log(f"  [{step}] safety checks auto-acknowledged: {[c.get('code') for c in checks]}")
                shot = base64.b64encode(computer.screenshot()).decode()
                out = {"type": "computer_call_output", "call_id": call["call_id"],
                       "output": {"type": "computer_screenshot", "image_url": f"data:image/png;base64,{shot}",
                                  "detail": "original"}}
                if checks:
                    out["acknowledged_safety_checks"] = checks
                self.items.append(out)
            steps.append({"step": step, "model_s": round(dt, 2), "exec_s": round(time.time() - t1, 2),
                          "actions": actions})
            self.log(f"  [{step}] model {dt:.1f}s, {len(actions)} action(s): " + " | ".join(x[:120] for x in actions))
        return {"stop_reason": "max_steps", "final": "", "steps": steps, "usage": self.usage}
