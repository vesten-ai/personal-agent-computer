"""Computer manager: creates and drives the agents' desktop VMs.

The gateway only talks to this interface, so Incus can be swapped for another backend later
(Firecracker, Docker for laptops). v1 has one implementation, IncusManager, which shells out to the
`incus` CLI: the CLI already speaks the Incus API, including exec over websockets, and can point at a
remote host with a client certificate restricted to one project.
"""

import json
import re
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
    saved: bool = False   # stopped with its memory saved: the next start restores the session


def mib(size: str) -> int:
    """'4GiB' -> 4096."""
    m = re.fullmatch(r"(\d+)\s*(MiB|GiB|MB|GB)", size.strip())
    if not m:
        raise ValueError(f"can't read memory size {size!r}")
    n, unit = int(m[1]), m[2]
    return n * {"MiB": 1, "GiB": 1024, "MB": 1, "GB": 1000}[unit]


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
            for nic in ((inst.get("state") or {}).get("network") or {}).values():  # null while stopped
                for a in nic.get("addresses", []):
                    if a["family"] == "inet" and a["scope"] == "global":
                        addr = a["address"]
            return ComputerInfo(name, inst["status"], addr, bool(inst.get("stateful")))
        return ComputerInfo(name, "Missing", None)

    def create(self, name: str, ca_pem: str, files: list[dict] | None = None,
               runcmd: list[list[str]] | None = None) -> None:
        """Launch a desktop VM whose TLS clients trust the gateway's egress CA (via cloud-init).

        `files` are extra cloud-init write_files entries; `runcmd` extra first-boot commands, run before
        the CA is trusted (wait_ready keys on the CA, so they have finished when the VM counts as ready)."""
        user_data = "#cloud-config\n" + json.dumps({
            "write_files": [{"path": "/usr/local/share/pac/gateway-ca.pem", "content": ca_pem,
                             "permissions": "0644"}, *(files or [])],
            "runcmd": [*(runcmd or []),
                       ["sh", "-c", "/usr/local/sbin/pac-trust-ca < /usr/local/share/pac/gateway-ca.pem"]],
        })
        image = f"{self.remote}:{self.image}" if self.remote and ":" not in self.image else self.image
        self._run("launch", image, self._target(name), "--vm",
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

    def start(self, name: str) -> str:
        """Start, resume or restore the VM. Returns how: running, resumed, restored or booted."""
        info = self.info(name)
        if info.state == "Frozen":  # `incus start` also resumes a paused VM
            self._run("start", self._target(name))
            return "resumed"
        if info.state != "Stopped":
            return "running"
        try:
            self._run("start", self._target(name))
            return "restored" if info.saved else "booted"
        except ManagerError:
            if not info.saved:
                raise
            self._run("start", self._target(name), "--stateless")  # the saved session is unusable
            return "booted"

    def pause(self, name: str) -> None:
        if self.info(name).state == "Running":
            self._run("pause", self._target(name))

    def stop(self, name: str, stateful: bool = False, timeout: int = 60) -> None:
        """Shut the VM down cleanly; force it if the guest doesn't finish within `timeout` seconds.
        (A guest just unpaused can sit on the ACPI request for minutes.)"""
        if self.info(name).state not in ("Running", "Frozen"):
            return
        try:
            self._run("stop", self._target(name), *(["--stateful"] if stateful else []),
                      "--timeout", str(timeout), timeout=timeout + 60)
        except (ManagerError, subprocess.TimeoutExpired):
            if stateful:
                raise
            self._run("stop", self._target(name), "--force")

    def sync_clock(self, name: str) -> None:
        """Set the guest's clock from ours. A paused or restored VM's clock is behind by however long it
        slept; NTP from the gateway would also fix it, but only at its next poll."""
        cmd = self.exec_argv(name, "date", "-u", "-s", f"@{time.time():.3f}", user=None)
        subprocess.run(cmd, capture_output=True, timeout=30)

    def reclaim_memory(self, name: str) -> None:
        """Hand memory back to the host after a restore.

        Restoring a saved VM writes every page of its memory, and free page reporting won't report those
        pages again, so the VM would hold its whole limit. Shrinking the balloon to a little above what
        the guest uses and growing it back returns the rest to the host."""
        out = subprocess.run(self.exec_argv(name, "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches; "
                                            "awk '/^MemTotal|^MemAvailable/{print $2}' /proc/meminfo", user=None),
                             capture_output=True, text=True, timeout=60).stdout.split()
        if len(out) != 2:
            return
        used = (int(out[0]) - int(out[1])) // 1024
        limit = mib(self.memory)
        target = max(1024, -(-(used + 768) // 256) * 256)
        if target >= limit:
            return
        try:
            self._run("config", "set", self._target(name), f"limits.memory={target}MiB", timeout=120)
        except ManagerError:
            pass  # the guest couldn't give that much back in time; what it gave is still returned
        # Grow back. After a failed shrink the stored limit is unchanged and setting it again would do
        # nothing, so go through one MiB less.
        self._run("config", "set", self._target(name), f"limits.memory={limit - 1}MiB")
        self._run("config", "set", self._target(name), f"limits.memory={self.memory}")

    def delete(self, name: str) -> None:
        if self.info(name).state != "Missing":
            self._run("delete", "--force", self._target(name))

    # -- channels into the VM ----------------------------------------------------------------
    def computerd_argv(self, name: str) -> list[str]:
        return self.exec_argv(name, "pac-computerd")

    def vnc_argv(self, name: str) -> list[str]:
        """stdio <-> the VM's x11vnc on 127.0.0.1:5900."""
        return self.exec_argv(name, "socat", "STDIO", "TCP:127.0.0.1:5900", user=None)
