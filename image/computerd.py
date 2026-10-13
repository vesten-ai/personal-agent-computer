#!/usr/bin/env python3
"""pac-computerd: the agent VM's computer-use daemon.

One small, model-agnostic action vocabulary. The gateway's model adapters (OpenAI Responses
`computer`, Anthropic `computer_toolset_20260801`, ...) translate their own formats into these
operations, so the VM never needs to know which model is driving it.

Transport: JSON lines on stdin/stdout. The gateway starts it through the Incus API
(`incus exec <vm> -- runuser -u agent -- pac-computerd`), which rides the VM's vsock: no port
is opened on the network.

Request:  {"id": 1, "op": "click", "x": 10, "y": 20, "button": "left", "count": 1, "modifiers": ["ctrl"]}
Response: {"id": 1, "ok": true, "result": {...}}  or  {"id": 1, "ok": false, "error": "..."}

Operations
  screenshot                                   -> {"png_b64", "width", "height"}
  zoom        region=[x0,y0,x1,y1]             -> {"png_b64", ...}  region scaled to screen size, aspect kept
  click       x?, y?, button=left|middle|right|back|forward, count=1..3, modifiers=[]
  move        x, y
  drag        path=[[x,y],...], button=left, modifiers=[]
  mouse_down  button=left          mouse_up  button=left
  scroll      x?, y?, dx=0, dy=0 (wheel clicks; +dy = down, +dx = right), modifiers=[]
  type        text
  key         keys=["ctrl","s"] (one chord, xdotool key names), repeat=1
  hold_key    keys=[...], seconds
  wait        seconds
  cursor_position                              -> {"x", "y"}
  run         argv=[...], timeout=60           -> {"code", "stdout", "stderr"}  (as the agent user)
"""

import base64
import json
import os
import subprocess
import sys
import time

ENV = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1"))
BUTTONS = {"left": 1, "middle": 2, "wheel": 2, "right": 3, "back": 8, "forward": 9}
MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt",
             "super": "super", "meta": "super", "cmd": "super", "command": "super", "win": "super"}


def sh(argv, data=None, timeout=30):
    p = subprocess.run(argv, input=data, env=ENV, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed: {p.stderr.decode(errors='replace').strip()[:300]}")
    return p.stdout


def xdo(*args):
    return sh(["xdotool", *[str(a) for a in args]])


def screen_size():
    w, h = sh(["xdotool", "getdisplaygeometry"]).split()
    return int(w), int(h)


def png_result(png):
    w, h = screen_size()
    return {"png_b64": base64.b64encode(png).decode(), "width": w, "height": h}


class held:
    """Hold modifier keys for the duration of a mouse action."""

    def __init__(self, modifiers):
        self.keys = [MODIFIERS.get(m.lower(), m) for m in (modifiers or [])]

    def __enter__(self):
        for k in self.keys:
            xdo("keydown", k)

    def __exit__(self, *exc):
        for k in reversed(self.keys):
            xdo("keyup", k)


def op_screenshot(_):
    return png_result(sh(["import", "-silent", "-window", "root", "png:-"]))


def op_zoom(a):
    x0, y0, x1, y1 = (int(v) for v in a["region"])
    if x1 <= x0 or y1 <= y0:
        raise ValueError("zoom region must have x1 > x0 and y1 > y0")
    w, h = screen_size()
    full = sh(["import", "-silent", "-window", "root", "png:-"])
    out = sh(["convert", "png:-", "-crop", f"{x1 - x0}x{y1 - y0}+{x0}+{y0}", "+repage",
              "-resize", f"{w}x{h}", "png:-"], data=full)
    return {"png_b64": base64.b64encode(out).decode(), "width": w, "height": h}


def op_move(a):
    xdo("mousemove", "--sync", int(a["x"]), int(a["y"]))


def op_click(a):
    if a.get("x") is not None and a.get("y") is not None:
        op_move(a)
    with held(a.get("modifiers")):
        xdo("click", "--repeat", int(a.get("count", 1)), "--delay", 80, BUTTONS[a.get("button", "left")])


def op_drag(a):
    path = a["path"]
    if len(path) < 2:
        raise ValueError("drag needs at least two points")
    b = BUTTONS[a.get("button", "left")]
    with held(a.get("modifiers")):
        xdo("mousemove", "--sync", *path[0])
        xdo("mousedown", b)
        for x, y in path[1:]:
            time.sleep(0.03)
            xdo("mousemove", "--sync", int(x), int(y))
        xdo("mouseup", b)


def op_mouse_down(a):
    xdo("mousedown", BUTTONS[a.get("button", "left")])


def op_mouse_up(a):
    xdo("mouseup", BUTTONS[a.get("button", "left")])


def op_scroll(a):
    if a.get("x") is not None and a.get("y") is not None:
        op_move(a)
    dx, dy = int(a.get("dx", 0)), int(a.get("dy", 0))
    with held(a.get("modifiers")):
        if dy:
            xdo("click", "--repeat", abs(dy), "--delay", 30, 5 if dy > 0 else 4)
        if dx:
            xdo("click", "--repeat", abs(dx), "--delay", 30, 7 if dx > 0 else 6)


def op_type(a):
    text = a["text"]
    if text.isascii():
        xdo("type", "--delay", 8, "--", text)
    else:
        # xdotool's keysym remapping drops or reorders CJK characters in some toolkits;
        # paste through the clipboard instead (the agent owns the clipboard).
        # xclip stays behind to serve the selection; give it no pipes to hold open.
        subprocess.run(["xclip", "-selection", "clipboard", "-in"], input=text.encode(), env=ENV,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
        xdo("key", "--clearmodifiers", "ctrl+v")


def op_key(a):
    chord = "+".join(MODIFIERS.get(k.lower(), k) for k in a["keys"])
    for _ in range(max(1, min(int(a.get("repeat", 1)), 100))):
        xdo("key", "--clearmodifiers", chord)


def op_hold_key(a):
    keys = [MODIFIERS.get(k.lower(), k) for k in a["keys"]]
    for k in keys:
        xdo("keydown", k)
    time.sleep(min(float(a.get("seconds", 1)), 300))
    for k in reversed(keys):
        xdo("keyup", k)


def op_wait(a):
    time.sleep(min(float(a.get("seconds", 1)), 300))


def op_cursor_position(_):
    out = sh(["xdotool", "getmouselocation", "--shell"]).decode()
    vals = dict(line.split("=", 1) for line in out.split() if "=" in line)
    return {"x": int(vals["X"]), "y": int(vals["Y"])}


def op_run(a):
    p = subprocess.run(a["argv"], env=ENV, capture_output=True, timeout=float(a.get("timeout", 60)))
    return {"code": p.returncode, "stdout": p.stdout.decode(errors="replace")[-20000:],
            "stderr": p.stderr.decode(errors="replace")[-20000:]}


OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_")}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = {}
        try:
            req = json.loads(line)
            result = OPS[req["op"]](req)
            resp = {"id": req.get("id"), "ok": True, "result": result or {}}
        except KeyError as e:
            resp = {"id": req.get("id"), "ok": False, "error": f"unknown op or missing field: {e}"}
        except Exception as e:  # report every failure to the gateway, never die mid-session
            resp = {"id": req.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
