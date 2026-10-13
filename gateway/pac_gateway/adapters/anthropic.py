"""Anthropic Messages API adapter: the `computer_toolset_20260801` toolset.

Wire rules this follows (platform.claude.com computer-use-tool docs, 2026-10):
  - tools entry is {"type": "computer_toolset_20260801"}: no name, no display size; coordinates
    are in the pixel space of the screenshots we return (we return the screen at native size).
  - each call is a tool_use block whose `name` is the member (left_click, type, ...) with
    toolset_name "computer"; several may arrive in one turn. Run them in order, stop at the first
    failure, answer every block, and echo toolset_name on every tool_result.
  - messages are append-only and assistant content is replayed verbatim (thinking blocks carry
    signatures and are bound to the exact prefix), so nothing earlier is ever edited or pruned.
"""

import base64
import os
import time

from ..computer import Computer, ComputerError
from ..http import post_json

API = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."
BUTTON = {"left_click": "left", "right_click": "right", "middle_click": "middle",
          "double_click": "left", "triple_click": "left"}
COUNT = {"double_click": 2, "triple_click": 3}


def _mods(text):
    return [m for m in (text or "").split("+") if m]


def _image(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode()}}


def execute(computer: Computer, name: str, a: dict) -> list[dict]:
    """Run one toolset member; return tool_result content blocks."""
    xy = a.get("coordinate")
    pos = {"x": xy[0], "y": xy[1]} if xy else {}
    if name == "screenshot":
        return [_image(computer.screenshot())]
    if name == "zoom":
        return [_image(computer.zoom(a["region"]))]
    if name in BUTTON:
        computer.call("click", **pos, button=BUTTON[name], count=COUNT.get(name, 1), modifiers=_mods(a.get("text")))
    elif name == "left_click_drag":
        computer.call("drag", path=[a["start_coordinate"], a["coordinate"]], modifiers=_mods(a.get("text")))
    elif name == "mouse_move":
        computer.call("move", **pos)
    elif name == "left_mouse_down":
        computer.call("mouse_down", button="left")
    elif name == "left_mouse_up":
        computer.call("mouse_up", button="left")
    elif name == "cursor_position":
        c = computer.call("cursor_position")
        return [{"type": "text", "text": f"X={c['x']}, Y={c['y']}"}]
    elif name == "scroll":
        n = int(a.get("scroll_amount", 3))
        d = a["scroll_direction"]
        computer.call("scroll", **pos, dy=n if d == "down" else -n if d == "up" else 0,
                      dx=n if d == "right" else -n if d == "left" else 0, modifiers=_mods(a.get("text")))
    elif name == "type":
        computer.call("type", text=a["text"])
    elif name == "key":
        computer.call("key", keys=_mods(a["text"]), repeat=int(a.get("repeat", 1)))
    elif name == "hold_key":
        computer.call("hold_key", keys=_mods(a["text"]), seconds=a.get("duration", 1))
    elif name == "wait":
        computer.call("wait", seconds=a.get("duration", 1))
    else:
        raise ComputerError(f"unsupported computer action {name!r}")
    return [{"type": "text", "text": "OK"}]


class AnthropicComputerAgent:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", api_key: str | None = None,
                 system: str = "", log=print):
        self.model, self.effort, self.system, self.log = model, effort, system, log
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.messages: list[dict] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

    def _request(self) -> dict:
        body = {
            "model": self.model,
            "max_tokens": 16000,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.effort},
            "tools": [{"type": "computer_toolset_20260801"}],
            "messages": self.messages,
            # Route a safety-classifier refusal to the API's default fallback instead of failing the turn.
            "fallbacks": "default",
            "cache_control": {"type": "ephemeral"},
        }
        if self.system:
            body["system"] = self.system
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "anthropic-beta": "server-side-fallback-2026-07-01"}
        return post_json(API, headers, body)

    def run(self, task: str, computer: Computer, max_steps: int = 40) -> dict:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": task}]})
        steps = []
        for step in range(1, max_steps + 1):
            t0 = time.time()
            resp = self._request()
            dt = time.time() - t0
            for k in self.usage:
                self.usage[k] += resp.get("usage", {}).get(k) or 0
            content = resp["content"]
            self.messages.append({"role": "assistant", "content": content})  # verbatim, thinking included
            for b in content:
                if b["type"] == "thinking" and b.get("thinking"):
                    self.log(f"  [{step}] thinking: {b['thinking'][:200]}")
                elif b["type"] == "text":
                    self.log(f"  [{step}] text: {b['text'][:300]}")
            calls = [b for b in content if b["type"] == "tool_use"]
            stop = resp.get("stop_reason")
            if stop != "tool_use" or not calls:
                final = "".join(b.get("text", "") for b in content if b["type"] == "text")
                return {"stop_reason": stop, "final": final, "steps": steps, "usage": self.usage,
                        "model_used": resp.get("model")}
            results, failed, actions = [], False, []
            t1 = time.time()
            for b in calls:
                if failed:
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "toolset_name": "computer",
                                    "is_error": True, "content": NOT_EXECUTED})
                    continue
                try:
                    out = execute(computer, b["name"], b.get("input", {}))
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "toolset_name": "computer",
                                    "content": out})
                    actions.append(f"{b['name']} {b.get('input', {})}")
                except Exception as e:
                    failed = True
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "toolset_name": "computer",
                                    "is_error": True, "content": f"Error: {e}"})
                    actions.append(f"{b['name']} FAILED: {e}")
            # A batch that doesn't end with a screenshot gets one attached, so the model can verify.
            if not failed and calls[-1]["name"] not in ("screenshot", "zoom"):
                results[-1]["content"] = results[-1]["content"] + [_image(computer.screenshot())]
            self.messages.append({"role": "user", "content": results})
            steps.append({"step": step, "model_s": round(dt, 2), "exec_s": round(time.time() - t1, 2),
                          "actions": actions})
            self.log(f"  [{step}] model {dt:.1f}s, {len(calls)} action(s): " + " | ".join(a[:120] for a in actions))
        return {"stop_reason": "max_steps", "final": "", "steps": steps, "usage": self.usage}
