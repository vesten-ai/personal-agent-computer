"""Client for pac-computerd, the computer-use daemon inside an agent VM (see image/computerd.py)."""

import base64
import itertools
import json
import subprocess


class ComputerError(RuntimeError):
    pass


class Computer:
    """Talks JSON lines to one pac-computerd process.

    `argv` starts the daemon, normally through the Incus API, e.g.
    ["incus", "exec", "--project", "pac", "pac-vm1", "--mode", "non-interactive", "--",
     "runuser", "-u", "agent", "--", "pac-computerd"].
    """

    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.ids = itertools.count(1)

    def call(self, op: str, **args) -> dict:
        rid = next(self.ids)
        self.proc.stdin.write(json.dumps({"id": rid, "op": op, **args}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise ComputerError(f"pac-computerd exited (code {self.proc.poll()})")
        resp = json.loads(line)
        if resp.get("id") != rid:
            raise ComputerError(f"out-of-order reply: {resp}")
        if not resp["ok"]:
            raise ComputerError(resp["error"])
        return resp["result"]

    def screenshot(self) -> bytes:
        return base64.b64decode(self.call("screenshot")["png_b64"])

    def zoom(self, region) -> bytes:
        return base64.b64decode(self.call("zoom", region=list(region))["png_b64"])

    def run(self, argv: list[str], timeout: float = 60) -> dict:
        return self.call("run", argv=argv, timeout=timeout)

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
