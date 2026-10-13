#!/usr/bin/env python3
"""Spike 3: the same desktop task, driven once by GPT and once by Claude, through one pac-computerd.

  ANTHROPIC_API_KEY=... OPENAI_API_KEY=... \
  python3 spikes/03-two-computer-use/run.py --via "ssh root@<incus-host>" --vm pac-vm1 --adapter both

The daemon is started through `incus exec` (the Incus API, i.e. vsock into the VM). --via prefixes
that command when the gateway runs somewhere other than the Incus host.
"""

import argparse
import json
import os
import shlex
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "gateway"))

from pac_gateway.adapters.anthropic import AnthropicComputerAgent  # noqa: E402
from pac_gateway.adapters.openai import OpenAIComputerAgent  # noqa: E402
from pac_gateway.computer import Computer  # noqa: E402

TASK = """You are using a Linux (XFCE) desktop. Do this:
1. Open the text editor Mousepad (Applications menu, or a terminal running `mousepad`).
2. Type exactly these two lines:
   Personal Agent Computer spike 3 ({tag})
   你好，世界。こんにちは。
3. Save the file as /home/agent/Desktop/spike3-{tag}.txt (create the folder if the dialog needs it).
4. Close Mousepad.
Then reply with one sentence saying whether it worked."""
EXPECTED = "Personal Agent Computer spike 3 ({tag})\n你好，世界。こんにちは。"


def computer(via: str, project: str, vm: str) -> Computer:
    argv = ["incus", "exec", "--project", project, vm, "--mode", "non-interactive", "--",
            "runuser", "-u", "agent", "--", "env", "DISPLAY=:1", "pac-computerd"]
    if via:
        pre = shlex.split(via)
        if pre[0] == "ssh":  # ssh takes one command string; no tty so the JSON stream stays clean
            argv = [pre[0], "-T", *pre[1:], " ".join(shlex.quote(a) for a in argv)]
        else:
            argv = pre + argv
    return Computer(argv)


def reset(c: Computer, tag: str) -> None:
    c.run(["sh", "-c", f"pkill -x mousepad; rm -f /home/agent/Desktop/spike3-{tag}.txt; mkdir -p /home/agent/Desktop"])


def verify(c: Computer, tag: str) -> dict:
    r = c.run(["cat", f"/home/agent/Desktop/spike3-{tag}.txt"])
    got = r["stdout"].strip()
    still_open = c.run(["pgrep", "-x", "mousepad"])["code"] == 0
    return {"file_matches": got == EXPECTED.format(tag=tag), "file_content": got[:500],
            "mousepad_closed": not still_open}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--via", default="", help='prefix to reach the Incus host, e.g. "ssh root@host"')
    ap.add_argument("--project", default="pac")
    ap.add_argument("--vm", required=True)
    ap.add_argument("--adapter", choices=["anthropic", "openai", "both"], default="both")
    ap.add_argument("--anthropic-model", default="claude-opus-5-5")
    ap.add_argument("--openai-model", default="gpt-6.1-sol")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--openai-tool", choices=["native", "function"], default="native",
                    help="native = built-in computer tool; function = the same actions as a function tool")
    ap.add_argument("--out", default="spike3-results.json")
    args = ap.parse_args()

    runs = []
    for name in (["anthropic", "openai"] if args.adapter == "both" else [args.adapter]):
        tag = name
        c = computer(args.via, args.project, args.vm)
        try:
            reset(c, tag)
            print(f"== {name}")
            if name == "anthropic":
                agent = AnthropicComputerAgent(model=args.anthropic_model, effort=args.effort)
            else:
                agent = OpenAIComputerAgent(model=args.openai_model, effort=args.effort, tool_mode=args.openai_tool)
            t0 = time.time()
            try:
                out = agent.run(TASK.format(tag=tag), c)
            except Exception as e:  # record API errors as a result, don't lose the other run
                out = {"stop_reason": "error", "final": "", "error": str(e)[:3000], "steps": []}
            out["wall_s"] = round(time.time() - t0, 1)
            out["verify"] = verify(c, tag)
            out["adapter"], out["model"] = name, agent.model
            out["tool_mode"] = getattr(agent, "tool_mode", "native")
            print(json.dumps({k: out[k] for k in ("stop_reason", "final", "wall_s", "verify")}, ensure_ascii=False, indent=2))
            runs.append(out)
        finally:
            c.close()
    with open(args.out, "w") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)
    print(f"written {args.out}")
    return 0 if all(r["verify"]["file_matches"] for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
