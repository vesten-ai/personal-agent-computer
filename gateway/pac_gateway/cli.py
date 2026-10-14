"""`pac`: manage agents from a terminal, through the gateway's HTTP API.

  pac list                         agents and their state
  pac create NAME [--model M]      new agent; waits until its computer is ready
  pac send NAME TEXT...            give the agent a task and follow it until it is done
  pac log NAME [-n 20]             the last messages
  pac wake NAME | pause NAME | stop NAME
  pac delete NAME

Run it in the gateway container (install.sh puts a `pac` wrapper on the host that does that), or anywhere
that reaches the gateway with PAC_URL and PAC_TOKEN set. Standard library only.
"""

import argparse
import json
import os
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request

from . import __version__

CONFIG = "/etc/pac/gateway.toml"
ENV_FILE = "/etc/pac/gateway.env"


class CLIError(Exception):
    pass


def _env_file_value(name: str) -> str:
    try:
        with open(ENV_FILE) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == name:
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def connection() -> tuple[str, str]:
    url, token = os.environ.get("PAC_URL", ""), os.environ.get("PAC_TOKEN", "")
    if not url or not token:
        cfg = {}
        try:
            with open(CONFIG, "rb") as f:
                cfg = tomllib.load(f)
        except OSError:
            pass
        url = url or f"http://{cfg.get('listen_host', '127.0.0.1')}:{cfg.get('listen_port', 8000)}"
        token_env = cfg.get("admin_token_env", "PAC_ADMIN_TOKEN")
        token = token or os.environ.get(token_env, "") or _env_file_value(token_env)
    if not token:
        raise CLIError(f"no admin token: set PAC_TOKEN, or run where {ENV_FILE} is readable")
    return url.rstrip("/"), token


class Gateway:
    def __init__(self, url: str, token: str):
        self.url, self.token = url, token

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "authorization": f"Bearer {self.token}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get("error", e.reason)
            except Exception:
                msg = e.reason
            raise CLIError(f"{method} {path}: {msg}") from None
        except urllib.error.URLError as e:
            raise CLIError(f"can't reach the gateway at {self.url}: {e.reason}") from None

    def agents(self) -> list[dict]:
        return self.call("GET", "/api/agents")["agents"]

    def find(self, name: str) -> dict:
        for a in self.agents():
            if name in (a["name"], a["id"]):
                return a
        raise CLIError(f"no agent named {name!r} (see `pac list`)")


def pad(text: str, width: int) -> str:
    """Left-justify for a terminal, where CJK characters take two columns."""
    used = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(1, width - used)


def show(ev: dict, out=None) -> None:
    d = ev["data"]
    line = {
        "user": lambda: f"> {d['text']}",
        "text": lambda: d,
        "actions": lambda: "\n".join(f"  · {a}" for a in d or []),
        "notice": lambda: f"  [{d['text']}]",
        "error": lambda: f"  error: {d['text']}",
        "approval_answered": lambda: f"  [answered: {d['answer']}]",
    }.get(ev["kind"])
    if line:
        print(line(), file=out or sys.stdout, flush=True)


def follow(gw: Gateway, agent: dict, after: int) -> int:
    """Print the agent's events after `after` until its turn is over. Approvals are asked here."""
    path, working = f"/api/agents/{agent['id']}", agent.get("busy", False)
    while True:
        for ev in gw.call("GET", f"{path}/events?after={after}")["events"]:
            after = ev["id"]
            show(ev)
            if ev["kind"] == "approval":
                d = ev["data"]
                print(f"\n  The agent asks: {d['question']}")
                for i, o in enumerate(d["options"], 1):
                    print(f"    {i}. {o}")
                answer = input("  Your answer (number or text): ").strip()
                if answer.isdigit() and 1 <= int(answer) <= len(d["options"]):
                    answer = d["options"][int(answer) - 1]
                gw.call("POST", f"{path}/answer", {"id": d["id"], "text": answer})
            elif ev["kind"] == "status":
                status = ev["data"]["status"]
                if status == "working":
                    working = True
                elif working and status in ("ready", "taken_over", "error"):
                    return 0 if status != "error" else 1
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pac", description="Manage Personal Agent Computer agents.")
    ap.add_argument("--version", action="version", version=f"pac {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="agents and their state")
    p = sub.add_parser("create", help="create an agent and wait for its computer")
    p.add_argument("name")
    p.add_argument("--model")
    p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("send", help="give an agent a task and follow it")
    p.add_argument("name")
    p.add_argument("text", nargs="+")
    p.add_argument("--no-follow", action="store_true")
    p = sub.add_parser("log", help="an agent's last messages")
    p.add_argument("name")
    p.add_argument("-n", type=int, default=20)
    for cmd, what in (("wake", "start or resume the computer"), ("pause", "pause the computer (keeps memory)"),
                      ("stop", "shut the computer down"), ("delete", "delete the agent and its computer")):
        sub.add_parser(cmd, help=what).add_argument("name")
    args = ap.parse_args(argv)

    try:
        gw = Gateway(*connection())
        if args.cmd == "list":
            agents = gw.agents()
            if not agents:
                print("no agents yet: pac create NAME")
            for a in agents:
                print(f"{pad(a['name'], 20)}{pad(a['status'], 13)}{pad(a['model'], 13)}{a['id']}")
        elif args.cmd == "create":
            a = gw.call("POST", "/api/agents", {"name": args.name, "model": args.model})
            print(f"creating {a['name']} ({a['id']})", flush=True)
            while not args.no_wait and a["status"] == "creating":
                time.sleep(3)
                a = gw.find(a["id"])
            if a["status"] == "error":
                for ev in gw.call("GET", f"/api/agents/{a['id']}/events?last=3")["events"]:
                    show(ev, sys.stderr)
                return 1
            print(a["status"])
        elif args.cmd == "send":
            a = gw.find(args.name)
            eid = gw.call("POST", f"/api/agents/{a['id']}/messages", {"text": " ".join(args.text)})["event_id"]
            if a["taken_over"]:
                print("You have this agent's computer on the web page; it reads this when you hand it back.")
            elif not args.no_follow:
                try:
                    return follow(gw, a, eid)
                except KeyboardInterrupt:
                    gw.call("POST", f"/api/agents/{a['id']}/interrupt", {})
                    print("\nasked the agent to stop after its current step", file=sys.stderr)
                    return 130
        elif args.cmd == "log":
            a = gw.find(args.name)
            for ev in gw.call("GET", f"/api/agents/{a['id']}/events?last={args.n}")["events"]:
                show(ev)
        elif args.cmd in ("wake", "pause", "stop"):
            a = gw.find(args.name)
            if args.cmd == "wake":
                a = gw.call("POST", f"/api/agents/{a['id']}/wake", {})
            else:
                a = gw.call("POST", f"/api/agents/{a['id']}/sleep",
                            {"how": "paused" if args.cmd == "pause" else "stopped"})
            print(f"{a['name']}: {a['status']}")
        elif args.cmd == "delete":
            a = gw.find(args.name)
            gw.call("DELETE", f"/api/agents/{a['id']}")
            print(f"deleted {a['name']}")
    except CLIError as e:
        print(f"pac: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
