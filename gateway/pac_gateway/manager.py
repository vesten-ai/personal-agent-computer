"""Computer manager: creates and drives the agents' desktop VMs.

The gateway only talks to this interface, so Incus can be swapped for another backend later
(Firecracker, Docker for laptops). v1 has one implementation, IncusManager, which shells out to the
`incus` CLI: the CLI already speaks the Incus API, including exec over websockets, and can point at a
remote host with a client certificate restricted to one project.
"""

import json
import shlex
import subprocess
import time
from dataclasses import dataclass


class ManagerError(RuntimeError):
    pass


@dataclass
class ComputerInfo:
    name: str
    state: str            # Running, Stopped, Frozen, Missing
    address: str | None


class IncusManager:
    def __init__(self, command: list[str] | None = None, remote: str = "", project: str = "pac",
                 image: str = "pac-desktop/dev", cpu: int = 2, memory: str = "4GiB", disk: str = "20GiB"):
        self.command = command or ["incus"]
        self.remote, self.project, self.image = remote, project, image
        self.cpu, self.memory, self.disk = cpu, memory, disk

    # -- plumbing ----------------------------------------------------------------------------
    def _target(self, name: str) -> str:
        return f"{self.remote}:{name}" if self.remote else name

    def argv(self, *args: str) -> list[str]:
        """Full argv for an incus subcommand. Over ssh, the remote side gets one quoted string."""
        args = [*args, "--project", self.project] if args and args[0] != "exec" else [*args]
        if self.command[0] == "ssh":
            return [*self.command[:-1], "-T", shlex.join([self.command[-1], *args])]
        return [*self.command, *args]

    def _run(self, *args: str, timeout: float = 300, input: bytes | None = None) -> str:
        p = subprocess.run(self.argv(*args), input=input, capture_output=True, timeout=timeout)
        if p.returncode != 0:
            raise ManagerError(f"incus {' '.join(args[:3])}: {p.stderr.decode(errors='replace').strip()[:500]}")
        return p.stdout.decode(errors="replace")

    def exec_argv(self, name: str, *cmd: str, user: str | None = "agent") -> list[str]:
        inner = ["runuser", "-u", user, "--", "env", "DISPLAY=:1", *cmd] if user else list(cmd)
        return self.argv("exec", "--project", self.project, self._target(name), "--mode", "non-interactive",
                         "--", *inner)

    # -- lifecycle ---------------------------------------------------------------------------
    def info(self, name: str) -> ComputerInfo:
        where = [f"{self.remote}:"] if self.remote else []
        out = self._run("list", *where, name, "--format", "json")
        for inst in json.loads(out or "[]"):
            if inst["name"] != name:
                continue
            addr = None
            for nic in (inst.get("state") or {}).get("network", {}).values():
                for a in nic.get("addresses", []):
                    if a["family"] == "inet" and a["scope"] == "global":
                        addr = a["address"]
            return ComputerInfo(name, inst["status"], addr)
        return ComputerInfo(name, "Missing", None)

    def create(self, name: str, ca_pem: str) -> None:
        """Launch a desktop VM whose TLS clients trust the gateway's egress CA (via cloud-init)."""
        user_data = "#cloud-config\n" + json.dumps({
            "write_files": [{"path": "/usr/local/share/pac/gateway-ca.pem", "content": ca_pem,
                             "permissions": "0644"}],
            "runcmd": [["sh", "-c", "/usr/local/sbin/pac-trust-ca < /usr/local/share/pac/gateway-ca.pem"]],
        })
        self._run("launch", self.image, self._target(name), "--vm",
                  "-c", f"limits.cpu={self.cpu}", "-c", f"limits.memory={self.memory}",
                  "-c", "security.secureboot=false", "-c", "migration.stateful=true",
                  "-c", f"cloud-init.user-data={user_data}",
                  "-d", f"root,size={self.disk}", "-d", f"root,size.state={self.memory}", timeout=600)

    def wait_ready(self, name: str, timeout: float = 180) -> None:
        """Wait until the desktop answers and first-boot setup (CA trust) has finished."""
        deadline = time.time() + timeout
        probe = self.exec_argv(name, "sh", "-c",
                               "xdpyinfo >/dev/null 2>&1 && pgrep -x xfdesktop >/dev/null "
                               "&& test -f /usr/local/share/ca-certificates/pac-gateway.crt")
        while time.time() < deadline:
            if subprocess.run(probe, capture_output=True, timeout=30).returncode == 0:
                return
            time.sleep(2)
        raise ManagerError(f"{name} did not become ready within {timeout:.0f}s")

    def start(self, name: str) -> None:
        state = self.info(name).state
        if state in ("Frozen", "Stopped"):  # `incus start` also resumes a paused VM
            self._run("start", self._target(name))

    def pause(self, name: str) -> None:
        if self.info(name).state == "Running":
            self._run("pause", self._target(name))

    def stop(self, name: str, stateful: bool = False) -> None:
        if self.info(name).state in ("Running", "Frozen"):
            self._run("stop", self._target(name), *(["--stateful"] if stateful else []))

    def delete(self, name: str) -> None:
        if self.info(name).state != "Missing":
            self._run("delete", "--force", self._target(name))

    # -- channels into the VM ----------------------------------------------------------------
    def computerd_argv(self, name: str) -> list[str]:
        return self.exec_argv(name, "pac-computerd")

    def vnc_argv(self, name: str) -> list[str]:
        """stdio <-> the VM's x11vnc on 127.0.0.1:5900."""
        return self.exec_argv(name, "socat", "STDIO", "TCP:127.0.0.1:5900", user=None)
