#!/usr/bin/env python3
"""Offline checks for live-harness cleanup receipts; no Codex, credentials or Incus calls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("live_acceptance", Path(__file__).with_name("check-codex-live.py"))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class CleanupReceipts(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        rt = SimpleNamespace(reaper=asyncio.create_task(asyncio.sleep(60)), sessions={},
            hub=SimpleNamespace(close=Mock()), store=SimpleNamespace(events=Mock(return_value=[])),
            delete_agent=Mock(), manager=SimpleNamespace(info=Mock(return_value=SimpleNamespace(state="Missing"))))
        session = SimpleNamespace(id="only-this-test", row={"vm": "pac-only-this-test", "status": "ready"})
        return rt, session, {"passed": True, "checks": {}}

    async def test_success_deletes_only_created_id_and_confirms_missing(self):
        rt, session, receipt = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            await live.cleanup(rt, session, Path(directory), receipt)
            saved = json.loads((Path(directory) / "receipt.json").read_text())
        rt.delete_agent.assert_called_once_with("only-this-test")
        rt.manager.info.assert_called_once_with("pac-only-this-test")
        self.assertTrue(saved["passed"])
        self.assertTrue(saved["checks"]["disposable_vm_removed"])

    async def test_delete_failure_is_recorded_without_private_error_payload(self):
        rt, session, receipt = self.fixture()
        rt.delete_agent.side_effect = RuntimeError("private backend diagnostic")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                await live.cleanup(rt, session, Path(directory), receipt)
            saved = (Path(directory) / "receipt.json").read_text()
            self.assertTrue((Path(directory) / "all-events.json").exists())
        self.assertFalse(json.loads(saved)["passed"])
        self.assertEqual(json.loads(saved)["cleanup_error_type"], "RuntimeError")
        self.assertNotIn("private backend diagnostic", saved)

    async def test_inflight_provisioning_is_not_raced_by_deletion(self):
        rt, session, receipt = self.fixture()
        session.row["status"] = "creating"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                await live.cleanup(rt, session, Path(directory), receipt)
            self.assertFalse(json.loads((Path(directory) / "receipt.json").read_text())["passed"])
        rt.delete_agent.assert_not_called()

    async def test_vm_still_present_prevents_green_receipt(self):
        rt, session, receipt = self.fixture()
        rt.manager.info.return_value.state = "Running"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                await live.cleanup(rt, session, Path(directory), receipt)
            saved = json.loads((Path(directory) / "receipt.json").read_text())
        self.assertFalse(saved["passed"])
        self.assertFalse(saved["checks"]["disposable_vm_removed"])


if __name__ == "__main__":
    unittest.main()
