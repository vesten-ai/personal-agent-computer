"""Agents: one desktop VM, one model conversation and one chat transcript each.

The model loop runs in a worker thread per turn (the adapters are synchronous); everything it reports
comes back through `emit`, which persists it and fans it out to the browsers watching that agent.
"""

import asyncio
import json
import os
import secrets as pysecrets
import threading

from .adapters.anthropic import AnthropicComputerAgent
from .adapters.openai import OpenAIComputerAgent
from .computer import Computer
from .config import Config
from .manager import IncusManager
from .mcp import ConnectorHub
from .store import Store

SYSTEM = """You are an AI agent with your own computer: a Linux desktop (XFCE, 1280x800) that is yours to use.
The person you work for talks to you in this chat. Do what they ask on your computer with the screen,
mouse and keyboard. Chromium, Mousepad (text editor), a terminal and LibreOffice are installed.
Before anything that spends money, sends a message on the person's behalf or deletes their data, stop and ask.
Credentials on this computer are placeholders; the network swaps in the real ones. If a site needs the person
to sign in or solve a challenge, ask them to take over the screen.
Reply in the person's language, briefly."""


def _quiet(*_):
    pass


class AgentSession:
    def __init__(self, runtime: "Runtime", row: dict):
        self.rt, self.row = runtime, row
        self.id = row["id"]
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self.worker: asyncio.Task | None = None
        self.stop_requested = False
        self.busy = False
        self.taken_over = False      # the person is driving the computer; the agent waits
        self.pending_note = ""       # told to the model on its next turn
        self.computer: Computer | None = None
        self.adapter = self._make_adapter()

    # -- model ---------------------------------------------------------------------------------
    def _make_adapter(self):
        mc = self.rt.cfg.models[self.row["model"]]
        common = dict(model=mc.model, effort=mc.effort, api_key=mc.api_key, system=self.rt.system_prompt(),
                      log=_quiet, on_event=self.emit, should_stop=lambda: self.stop_requested,
                      base_url=mc.base_url, tools=self.rt.hub.tools(), call_tool=self.rt.hub.call_tool)
        if mc.api == "anthropic":
            adapter = AnthropicComputerAgent(**common)
        else:
            adapter = OpenAIComputerAgent(**common, tool_mode=mc.tool_mode)
        state = self.rt.store.load_history(self.id)
        if state:
            adapter.load(state)
        return adapter

    # -- events (may be called from the worker thread) -----------------------------------------
    def emit(self, kind: str, data) -> None:
        if kind == "screenshot":
            self.rt.store.save_screen(self.id, data)
            data = {}
        elif kind == "actions":
            # A step finished: persist the model history so a restart resumes exactly here.
            self.rt.store.save_history(self.id, self.adapter.state())
        ev = self.rt.store.add_event(self.id, kind, data)
        self.rt.loop.call_soon_threadsafe(self._fanout, ev)

    def _fanout(self, ev: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def set_status(self, status: str) -> None:
        self.row["status"] = status
        self.rt.store.set_status(self.id, status)
        self.emit("status", {"status": status})

    # -- turns -----------------------------------------------------------------------------------
    async def send(self, text: str) -> None:
        self.emit("user", {"text": text})
        await self.queue.put(text)
        if self.taken_over:
            self.emit("notice", {"text": "The agent will read this when you hand the computer back."})
        elif self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._work())

    def interrupt(self) -> None:
        self.stop_requested = True

    def take_over(self) -> None:
        """The person takes the mouse and keyboard; the agent stops after its current step and waits."""
        if self.taken_over:
            return
        self.taken_over = True
        self.interrupt()
        self.emit("notice", {"text": "You have the computer. The agent waits until you hand it back."})
        if not self.busy:
            self.set_status("taken_over")

    def hand_back(self) -> None:
        if not self.taken_over:
            return
        self.taken_over = False
        self.pending_note = ("(The person took over your computer for a while and has handed it back. "
                             "Things on screen may have changed: look before you continue.)")
        self.emit("notice", {"text": "Handed back to the agent."})
        self.set_status("ready")
        if not self.queue.empty() and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self._work())

    async def _work(self) -> None:
        while not self.queue.empty() and not self.taken_over:
            text = await self.queue.get()
            if self.pending_note:
                text, self.pending_note = f"{self.pending_note}\n\n{text}", ""
            self.busy, self.stop_requested = True, False
            self.set_status("working")
            try:
                result = await asyncio.to_thread(self._turn, text)
                if result.get("stop_reason") == "interrupted":
                    self.emit("notice", {"text": "Stopped."})
                elif result.get("stop_reason") == "max_steps":
                    self.emit("notice", {"text": "Stopped after the step limit."})
            except Exception as e:  # report and keep the agent usable
                self.emit("error", {"text": f"{type(e).__name__}: {e}"[:2000]})
            finally:
                self.rt.store.save_history(self.id, self.adapter.state())
                self.busy = False
                self.set_status("taken_over" if self.taken_over else "ready")

    def _ensure_computer(self) -> Computer:
        mgr = self.rt.manager
        if mgr.info(self.row["vm"]).state != "Running":
            self.emit("notice", {"text": "Waking the computer..."})
            mgr.start(self.row["vm"])
            mgr.wait_ready(self.row["vm"])
            self.close_computer()
        if self.computer is None or self.computer.proc.poll() is not None:
            self.computer = Computer(mgr.computerd_argv(self.row["vm"]))
        return self.computer

    def _turn(self, text: str) -> dict:
        computer = self._ensure_computer()
        return self.adapter.run(text, computer, max_steps=self.rt.cfg.max_steps)

    def close_computer(self) -> None:
        if self.computer is not None:
            self.computer.close()
            self.computer = None


class Runtime:
    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop):
        self.cfg, self.loop = cfg, loop
        self.store = Store(cfg.data_dir)
        self.manager = IncusManager(**cfg.incus)
        self.hub = ConnectorHub(cfg.connectors)
        self.policy_lock = threading.Lock()
        self.sessions: dict[str, AgentSession] = {}
        for row in self.store.agents():
            if row["status"] == "working":  # the gateway restarted mid-turn
                self.store.set_status(row["id"], "ready")
                row["status"] = "ready"
            self.sessions[row["id"]] = AgentSession(self, row)
        threading.Thread(target=self.write_egress_policy, daemon=True).start()

    def system_prompt(self) -> str:
        if not self.cfg.secrets:
            return SYSTEM
        names = ", ".join(f"${s.vm_env} ({name})" for name, s in self.cfg.secrets.items())
        return (SYSTEM + f"\nCredentials you can use, already set in your shell environment: {names}. "
                "They are placeholders; use them exactly as if they were the real values.")

    # -- secrets ---------------------------------------------------------------------------------
    def placeholders(self, aid: str) -> dict[str, str]:
        return {name: self.store.placeholder(aid, name, lambda s=sec: s.prefix + pysecrets.token_hex(20))
                for name, sec in self.cfg.secrets.items()}

    def vm_files(self, aid: str) -> list[dict]:
        """First-boot files that give the agent its placeholders, for login shells and desktop apps alike."""
        if not self.cfg.secrets:
            return []
        ph = self.placeholders(aid)
        pairs = [(self.cfg.secrets[n].vm_env, v) for n, v in ph.items()]
        return [
            # Login shells, and (via bash.bashrc) the terminal windows the agent opens on its desktop.
            {"path": "/etc/profile.d/pac-secrets.sh", "permissions": "0644",
             "content": "".join(f"export {k}={v}\n" for k, v in pairs)},
            {"path": "/etc/bash.bashrc", "append": True,
             "content": "[ -r /etc/profile.d/pac-secrets.sh ] && . /etc/profile.d/pac-secrets.sh\n"},
            # Desktop apps started by the agent's systemd user manager (see vm_runcmd).
            {"path": "/home/agent/.config/environment.d/pac-secrets.conf", "permissions": "0644",
             "owner": "agent:agent", "defer": True, "content": "".join(f"{k}={v}\n" for k, v in pairs)},
        ]

    def vm_runcmd(self) -> list[list[str]]:
        """The agent's user manager started before cloud-init wrote environment.d: load it and restart the
        desktop once, so every app on it sees the placeholders."""
        if not self.cfg.secrets:
            return []
        return [["runuser", "-u", "agent", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000", "sh", "-c",
                 "systemctl --user set-environment $(cat /home/agent/.config/environment.d/pac-secrets.conf) "
                 "&& systemctl --user restart pac-xfce.service"]]

    def write_egress_policy(self) -> None:
        """Rebuild the egress proxy's policy: each agent's placeholders, bound to its VM's address."""
        with self.policy_lock:
            try:
                with open(self.cfg.egress_base_policy) as f:
                    policy = json.load(f)
            except FileNotFoundError:
                policy = {"default": "allow"}
            entries = []
            for aid, session in list(self.sessions.items()):
                if not self.cfg.secrets or session.row["status"] in ("creating", "error"):
                    continue
                try:
                    addr = self.manager.info(session.row["vm"]).address
                except Exception:
                    addr = None
                if not addr:
                    continue
                for name, value in self.placeholders(aid).items():
                    sec = self.cfg.secrets[name]
                    entries.append({"name": f"{name}@{aid}", "placeholder": value, "env": sec.value_env,
                                    "hosts": sec.hosts, "clients": [addr]})
            policy["secrets"] = policy.get("secrets", []) + entries
            path = self.cfg.egress_policy_file
            with open(path + ".tmp", "w") as f:
                json.dump(policy, f, indent=1)
            os.replace(path + ".tmp", path)

    def ca_pem(self) -> str:
        with open(self.cfg.egress_ca_file) as f:
            return f.read()

    def create_agent(self, name: str, model: str | None = None) -> AgentSession:
        model = model or self.cfg.default_model
        if model not in self.cfg.models:
            raise ValueError(f"unknown model {model!r}")
        row = self.store.create_agent(name, model)
        session = AgentSession(self, row)
        self.sessions[row["id"]] = session
        threading.Thread(target=self._provision, args=(session,), daemon=True).start()
        return session

    def _provision(self, session: AgentSession) -> None:
        try:
            session.emit("notice", {"text": "Setting up the computer..."})
            self.manager.create(session.row["vm"], self.ca_pem(), files=self.vm_files(session.id),
                                runcmd=self.vm_runcmd())
            self.manager.wait_ready(session.row["vm"])
            session.set_status("ready")
            self.write_egress_policy()
            session.emit("notice", {"text": "The computer is ready."})
        except Exception as e:
            session.emit("error", {"text": f"Setting up the computer failed: {e}"[:2000]})
            session.set_status("error")

    def delete_agent(self, aid: str) -> None:
        session = self.sessions.pop(aid)
        session.interrupt()
        session.close_computer()
        self.manager.delete(session.row["vm"])
        self.store.delete_agent(aid)
        self.write_egress_policy()
