"""Agents: one desktop VM, one model conversation and one chat transcript each.

The model loop runs in a worker thread per turn (the adapters are synchronous); everything it reports
comes back through `emit`, which persists it and fans it out to the browsers watching that agent.
"""

import asyncio
import json
import os
import secrets as pysecrets
import threading
import time

from .adapters.anthropic import AnthropicComputerAgent
from .adapters.openai import OpenAIComputerAgent
from .adapters.codex import CodexComputerAgent
from .computer import Computer
from .config import Config
from .manager import IncusManager
from .mcp import ConnectorHub
from .store import Store

SYSTEM = """You are an AI agent with your own computer: a Linux desktop (XFCE, 1280x800) that is yours to use.
The person you work for talks to you in this chat. Do what they ask on your computer with the screen,
mouse and keyboard. Chromium, Mousepad (text editor), a terminal and LibreOffice are installed.
Before anything that spends money, sends a message on the person's behalf or deletes their data, get their go-ahead
with the ask_person tool, then act on the answer.
Credentials on this computer are placeholders; the network swaps in the real ones. If a site needs the person
to sign in or solve a challenge, ask them to take over the screen.
Reply in the person's language, briefly."""


def _quiet(*_):
    pass


ASK_TOOL = {
    "name": "ask_person",
    "description": "Ask the person you work for a question and wait for the answer. Use it for a go-ahead before "
                   "spending money, sending a message on their behalf or deleting their data, and when you are "
                   "stuck on a choice only they can make. Keep the question short; list options when there are "
                   "only a few sensible answers.",
    "input_schema": {"type": "object", "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]},
}


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
        self.last_active = time.time()
        self.sleep_state: str | None = None   # "paused", "saved" or "stopped" while the computer sleeps
        self.viewers = 0                      # live screens open in a browser keep the computer awake
        self.pending: dict | None = None      # a question the agent is waiting on
        self.adapter = self._make_adapter()

    # -- model ---------------------------------------------------------------------------------
    def _make_adapter(self):
        mc = self.rt.cfg.models[self.row["model"]]
        common = dict(model=mc.model, effort=mc.effort, api_key=mc.api_key, system=self.rt.system_prompt(),
                      log=_quiet, on_event=self.emit, should_stop=lambda: self.stop_requested,
                      base_url=mc.base_url, tools=[ASK_TOOL, *self.rt.hub.tools()], call_tool=self._call_tool)
        if mc.api == "codex":
            adapter = CodexComputerAgent(**common, home=os.path.join(self.rt.store.agent_dir(self.id), "codex-runtime"),
                                         command=mc.codex_command or None, ask_person=self._ask)
        elif mc.api == "anthropic":
            adapter = AnthropicComputerAgent(**common, workspace_id=mc.workspace_id)
        else:
            adapter = OpenAIComputerAgent(**common, tool_mode=mc.tool_mode, confirm=self._confirm)
        state = self.rt.store.load_history(self.id)
        if state:
            adapter.load(state)
        return adapter

    # -- tools the gateway answers itself (called from the worker thread) ----------------------
    def _call_tool(self, name: str, args: dict) -> str:
        if name == "ask_person":
            return self._ask(str(args.get("question", "")), [str(o) for o in args.get("options") or []])
        return self.rt.hub.call_tool(name, args)

    def _ask(self, question: str, options: list[str], *, cancel_event=None) -> str:
        """Put the question in front of the person and block this turn until they answer or stop it."""
        qid = pysecrets.token_hex(6)
        done = threading.Event()
        self.pending = {"id": qid, "question": question, "options": options, "done": done, "answer": None}
        self.emit("approval", {"id": qid, "question": question, "options": options or ["Yes", "No"]})
        self.rt.loop.call_soon_threadsafe(self.set_status, "waiting")
        try:
            while not done.wait(.1 if cancel_event is not None else 1):
                if self.stop_requested or (cancel_event is not None and cancel_event.is_set()):
                    if cancel_event is not None:
                        self.emit("approval_answered", {"id": qid, "answer": "Cancelled: Codex turn ended."})
                    return "The person stopped the task without answering."
            answer = self.pending["answer"]
        finally:
            self.pending = None
        self.rt.loop.call_soon_threadsafe(self.set_status, "working")
        return f"The person answered: {answer}"

    def _confirm(self, question: str) -> bool:
        return self._ask(question, ["Allow", "Don't allow"]).endswith(": Allow")

    def answer(self, qid: str, text: str) -> None:
        p = self.pending
        if p and p["id"] == qid and not p["done"].is_set():
            p["answer"] = text
            self.emit("approval_answered", {"id": qid, "answer": text})
            p["done"].set()
            self.touch()

    # -- events (may be called from the worker thread) -----------------------------------------
    def emit(self, kind: str, data) -> dict:
        if kind == "screenshot":
            self.rt.store.save_screen(self.id, data)
            data = {}
        elif kind == "actions":
            # A step finished: persist the model history so a restart resumes exactly here.
            self.rt.store.save_history(self.id, self.adapter.state())
        ev = self.rt.store.add_event(self.id, kind, data)
        self.rt.loop.call_soon_threadsafe(self._fanout, ev)
        return ev

    def _fanout(self, ev: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def set_status(self, status: str) -> None:
        self.row["status"] = status
        self.rt.store.set_status(self.id, status)
        self.emit("status", {"status": status})

    # -- turns -----------------------------------------------------------------------------------
    def touch(self) -> None:
        self.last_active = time.time()

    async def send(self, text: str) -> int:
        """Queue a message for the agent; returns its event id."""
        self.touch()
        ev = self.emit("user", {"text": text})
        await self.queue.put(text)
        if self.taken_over:
            self.emit("notice", {"text": "The agent will read this when you hand the computer back."})
        elif self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._work())
        return ev["id"]

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
                self.touch()
                self.set_status("taken_over" if self.taken_over else "ready")

    def wake(self) -> None:
        """Bring the computer back from sleep (blocking; call from a worker thread)."""
        mgr, vm = self.rt.manager, self.row["vm"]
        if mgr.info(vm).state != "Running":
            self.emit("notice", {"text": "Waking the computer..."})
            t0 = time.time()
            how = mgr.start(vm)
            mgr.wait_ready(vm)
            mgr.sync_clock(vm)
            self.close_computer()
            self.rt.write_egress_policy()  # in case the VM came back on another address
            if how == "restored":
                threading.Thread(target=self._reclaim, daemon=True).start()
            note = {"restored": "; its windows are as it left them",
                    "booted": "; it started fresh" if self.sleep_state == "saved" else ""}.get(how, "")
            self.emit("notice", {"text": f"The computer is awake ({time.time() - t0:.0f} s){note}."})
        if self.sleep_state:
            self.sleep_state = None
            self.emit("status", {"status": self.row["status"]})
        self.touch()

    def _reclaim(self) -> None:
        try:
            self.rt.manager.reclaim_memory(self.row["vm"])
        except Exception:
            pass  # only costs memory

    def sleep(self, how: str, idle: bool = True) -> None:
        """Pause or stop the computer (blocking; call from a worker thread). A stop saves the session
        when idle_stop_stateful is on and the save works; sleep_state then says "saved"."""
        self.close_computer()
        why = "The computer was idle and has" if idle else "The computer has"
        if how == "stopped":
            mgr = self.rt.manager
            if self.rt.cfg.idle_stop_stateful:
                try:
                    mgr.stop(self.row["vm"], stateful=True)
                    how = "saved"
                except Exception as e:
                    self.emit("notice", {"text": f"Saving the open windows failed ({e}); shutting down instead."[:500]})
            if how == "saved":
                self.emit("notice", {"text": f"{why} been put to sleep with its windows saved. "
                                             "A new message wakes it."})
            else:
                mgr.stop(self.row["vm"])
                self.emit("notice", {"text": f"{why} been shut down. A new message wakes it."})
        else:
            self.rt.manager.pause(self.row["vm"])
            self.emit("notice", {"text": f"{why} been paused. A new message wakes it."})
        self.sleep_state = how
        self.emit("status", {"status": "asleep"})

    def _ensure_computer(self) -> Computer:
        mgr = self.rt.manager
        self.wake()
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
            if row["status"] in ("working", "waiting"):  # the gateway restarted mid-turn
                self.store.set_status(row["id"], "ready")
                row["status"] = "ready"
            self.sessions[row["id"]] = AgentSession(self, row)
        threading.Thread(target=self._startup_sync, daemon=True).start()
        self.reaper = loop.create_task(self._reap_idle())

    def _startup_sync(self) -> None:
        for session in list(self.sessions.values()):
            if session.row["status"] in ("creating", "error"):
                continue
            try:
                info = self.manager.info(session.row["vm"])
            except Exception:
                continue
            session.sleep_state = {"Frozen": "paused", "Stopped": "saved" if info.saved else "stopped"}.get(info.state)
        self.write_egress_policy()

    async def _reap_idle(self) -> None:
        """Put idle computers to sleep: pause first, stop later."""
        pause_s, stop_s = self.cfg.idle_pause_minutes * 60, self.cfg.idle_stop_minutes * 60
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for session in list(self.sessions.values()):
                if (session.busy or session.taken_over or session.viewers or not session.queue.empty()
                        or session.row["status"] != "ready"):
                    continue
                idle = now - session.last_active
                how = None
                if stop_s and idle >= stop_s and session.sleep_state not in ("saved", "stopped"):
                    how = "stopped"
                elif pause_s and idle >= pause_s and session.sleep_state is None:
                    how = "paused"
                if how:
                    try:
                        await asyncio.to_thread(session.sleep, how)
                    except Exception as e:
                        session.emit("error", {"text": f"Putting the computer to sleep failed: {e}"[:500]})

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
                    current = self.manager.info(session.row["vm"]).address
                except Exception:
                    current = None
                addr = self.store.address(aid, current)
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
        # Reject an unconfigured model before persisting a row. Otherwise session construction
        # fails, leaving a 'creating' agent that also prevents the next gateway startup.
        try:
            self.cfg.models[model].api_key
        except RuntimeError as e:
            raise ValueError(str(e)) from None
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
        session = self.sessions[aid]
        session.interrupt()
        session.close_computer()
        self.manager.delete(session.row["vm"])
        self.store.delete_agent(aid)
        # Keep a failed deletion addressable for retry, including its persisted diagnostics.
        self.sessions.pop(aid)
        self.write_egress_policy()
