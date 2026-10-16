#!/usr/bin/env python3
"""Opt-in, billable Codex + real Incus desktop acceptance using one new disposable VM.

The supplied config selects the provider/Incus connection and public egress CA. Existing
gateway data, policy and services are never used or rewritten. Only this run's VM is deleted.
"""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gateway"))
from pac_gateway.agent import Runtime
from pac_gateway.config import load

MCP_QUESTION = "Allow Codex to use the PAC computer/connector tool?"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


async def phase(session, prompt, timeout, *, stop_question=None):
    first = await session.send(prompt)
    deadline = time.monotonic() + timeout
    seen, stopped = first - 1, False
    while session.worker and not session.worker.done():
        for event in session.rt.store.events(session.id, after=seen, limit=1000):
            seen = event["id"]
            if event["kind"] in ("actions", "notice", "error"):
                print(json.dumps({"event": event["kind"], "data": event["data"]}), flush=True)
        pending = session.pending
        if pending and not pending["done"].is_set():
            question = pending["question"]
            if question == MCP_QUESTION:
                # The operator explicitly opts into real desktop calls by running this harness.
                session.answer(pending["id"], "Allow")
            elif stop_question and question == stop_question:
                session.interrupt()
                stopped = True
            else:
                session.interrupt()
                await asyncio.wait_for(asyncio.shield(session.worker), 30)
                raise RuntimeError("Unexpected human question; interrupted without answering")
        if time.monotonic() >= deadline:
            session.interrupt()
            await asyncio.wait_for(asyncio.shield(session.worker), 30)
            raise TimeoutError("Live acceptance phase timed out")
        await asyncio.sleep(.1)
    if session.worker:
        await session.worker
    events = session.rt.store.events(session.id, after=first - 1, limit=100000)
    if any(e["kind"] == "error" for e in events):
        raise RuntimeError("PAC reported an error; inspect the local events receipt")
    if stop_question:
        assert stopped and session.pending is None and not session.busy, "Stop did not settle the question"
        assert any(e["kind"] == "notice" and e["data"].get("text") == "Stopped." for e in events)
    else:
        assert any(e["kind"] == "text" for e in events), "No completed root response"
    return events


async def close_runtime(rt):
    rt.reaper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await rt.reaper
    for session in rt.sessions.values():
        session.interrupt()
        if session.worker and not session.worker.done():
            await asyncio.wait_for(asyncio.shield(session.worker), 30)
        await asyncio.to_thread(session.close_computer)
    rt.hub.close()


async def cleanup(rt, session, output, receipt):
    """Preserve evidence even when shutdown or deletion fails; never claim a green cleanup."""
    try:
        if session is not None:
            save(output / "all-events.json", rt.store.events(session.id, limit=100000))
        await close_runtime(rt)
        if session is not None:
            # Provisioning may still be completing after an interruption. Do not race its launch.
            if session.row["status"] == "creating":
                raise RuntimeError("Provisioning still active; the receipt identifies the VM for cleanup")
            await asyncio.to_thread(rt.delete_agent, session.id)
            receipt["checks"]["disposable_vm_removed"] = (
                (await asyncio.to_thread(rt.manager.info, session.row["vm"])).state == "Missing")
            if not receipt["checks"]["disposable_vm_removed"]:
                raise RuntimeError("Disposable VM remains after cleanup")
    except BaseException as exc:
        receipt["passed"] = False
        receipt["cleanup_error_type"] = type(exc).__name__
        raise
    finally:
        receipt["finished_at"] = time.time()
        save(output / "receipt.json", receipt)


async def main(args):
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    cfg = load(str(args.config))
    model = cfg.models[cfg.default_model]
    if model.api != "codex":
        raise ValueError("default_model must use api=codex")
    model.api_key  # fail before provisioning if the key is absent
    # Isolate all mutable runtime files, regardless of paths in the supplied config.
    cfg.data_dir = str(output / "data")
    cfg.egress_policy_file = str(output / "test-policy.json")
    cfg.egress_base_policy = str(output / "empty-base.json")
    cfg.secrets, cfg.connectors = {}, {}
    cfg.idle_pause_minutes = cfg.idle_stop_minutes = 0
    cfg.max_steps = 40
    command = model.codex_command or ["codex", "app-server", "--listen", "stdio://"]
    version = subprocess.run([command[0], "--version"], check=True, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    receipt = {"codex_version": version, "model": model.model, "provider": model.base_url,
               "started_at": time.time(), "real_provider": True, "real_desktop": True,
               "existing_gateway_modified": False, "checks": {}, "passed": False}
    save(output / "receipt.json", receipt)
    rt = Runtime(cfg, asyncio.get_running_loop())
    session = None
    nonce = "PAC-LIVE-" + secrets.token_hex(6)
    desktop_file = "/home/agent/Desktop/pac-codex-acceptance.txt"
    try:
        session = rt.create_agent("codex-live-" + secrets.token_hex(4))
        receipt.update(agent_id=session.id, vm=session.row["vm"])
        save(output / "receipt.json", receipt)
        # Incus launch can take 600s, followed by wait_ready's 180s.
        deadline = time.monotonic() + 850
        while session.row["status"] == "creating" and time.monotonic() < deadline:
            await asyncio.sleep(1)
        assert session.row["status"] == "ready", "VM did not become ready"
        print("Real disposable desktop ready", flush=True)
        receipt["checks"]["provision"] = True
        events = await phase(session,
            "This is an authorized integration acceptance on a new disposable desktop. "
            "You are the main Codex session. Use your built-in spawn_agent to delegate exactly "
            "one child this task: use the inherited PAC computer MCP tool to look at the desktop, "
            "open a terminal using Ctrl+Alt+T, and type a shell command there that writes exactly "
            f"{nonce} followed by one newline into {desktop_file}, then displays it with cat. "
            "The child must use screen, keyboard and mouse actions, not host shell tools. "
            "Wait for the child using built-in wait_agent. When it completes, you must call the "
            "PAC computer tool yourself for a screenshot and report the visible nonce. "
            "Do not finish before the child finishes. This temporary file creation is authorized. "
            "Reply briefly in Chinese.", args.timeout)
        save(output / "delegation-events.json", events)
        root_id = session.adapter.thread_id
        children = {child for e in events if e["kind"] == "codex"
                    for child in e["data"].get("receiver_thread_ids", []) if child != root_id}
        assert children, "No Codex child delegation recorded"
        tool_threads = {e["data"].get("thread_id") for e in events if e["kind"] == "codex"
                        and e["data"].get("method") == "item/completed"
                        and e["data"].get("item_type") == "mcpToolCall"}
        assert children & tool_threads, "Child did not complete an inherited MCP call"
        assert root_id in tool_threads, "Root did not complete an MCP call"
        assert any(e["kind"] == "screenshot" for e in events), "No real desktop screenshot"
        texts = [e["data"] for e in events if e["kind"] == "text"]
        assert nonce in "\n".join(texts), "Root did not report the nonce"
        probe = await asyncio.to_thread(subprocess.run,
            rt.manager.exec_argv(session.row["vm"], "cat", desktop_file),
            capture_output=True, timeout=30)
        assert probe.returncode == 0 and probe.stdout == (nonce + "\n").encode(), "VM file readback mismatch"
        shutil.copyfile(rt.store.screen_path(session.id), output / "delegation-screen.png")
        receipt["checks"]["delegation_and_file_readback"] = True
        receipt.update(root_thread_id=root_id, child_thread_ids=sorted(children), nonce=nonce)
        save(output / "receipt.json", receipt)

        # Recreate the gateway Runtime from persisted disk, not just the adapter object.
        aid = session.id
        await close_runtime(rt)
        rt.store.db.close()
        rt = Runtime(cfg, asyncio.get_running_loop())
        session = rt.sessions[aid]
        assert session.adapter.thread_id == root_id, "Gateway restart changed the root thread"
        events = await phase(session,
            "The gateway has restarted. Recall the exact nonce written by your child in the "
            "previous turn without asking me for it. Take a fresh PAC desktop screenshot, "
            "then reply with that nonce and the window you can see. Do not create another child.", args.timeout)
        save(output / "restart-events.json", events)
        assert session.adapter.thread_id == root_id
        assert nonce in "\n".join(e["data"] for e in events if e["kind"] == "text")
        assert any(e["kind"] == "screenshot" for e in events)
        receipt["checks"]["runtime_restart_and_same_thread_resume"] = True
        save(output / "receipt.json", receipt)

        stop_question = "PAC_ACCEPTANCE_STOP_" + secrets.token_hex(4)
        events = await phase(session,
            "Test the Stop button now. Call PAC ask_person with exactly this question: "
            f"{stop_question} . Pass an empty options list. Wait for the answer; do not operate "
            "the desktop or spawn children. This is an intentional test question.",
            args.timeout, stop_question=stop_question)
        save(output / "stop-events.json", events)
        receipt["checks"]["interrupt_pending_question"] = True
        events = await phase(session,
            "The stop test is finished. Take a fresh screenshot of your desktop and reply "
            "with PAC_RESUMED_OK. Do not ask a question or spawn children.", args.timeout)
        save(output / "after-stop-events.json", events)
        assert session.adapter.thread_id == root_id
        assert any(e["kind"] == "screenshot" for e in events)
        assert "PAC_RESUMED_OK" in "\n".join(e["data"] for e in events if e["kind"] == "text")
        receipt["checks"]["turn_after_stop"] = True
        receipt["passed"] = True
    except BaseException as exc:
        receipt["failure_type"] = type(exc).__name__
        raise
    finally:
        # Only this run's ID exists in the private store; never clean up another gateway's agents.
        await cleanup(rt, session, output, receipt)
    assert receipt["checks"]["disposable_vm_removed"]
    print(json.dumps(receipt, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new, private evidence directory")
    parser.add_argument("--timeout", type=int, default=480, help="seconds per model phase")
    asyncio.run(main(parser.parse_args()))
