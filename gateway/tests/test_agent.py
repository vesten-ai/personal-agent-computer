import asyncio
import os
import tempfile
import unittest
from unittest import mock

from pac_gateway.agent import Runtime
from pac_gateway.config import Config, ModelConfig
from pac_gateway.manager import ManagerError


class Creation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.threads = mock.patch("pac_gateway.agent.threading.Thread")
        self.thread = self.threads.start()
        self.addCleanup(self.threads.stop)
        self.cfg = Config(data_dir=self.temp.name, default_model="gpt", models={
            "gpt": ModelConfig(name="gpt", api="openai", model="test", key_env="PAC_TEST_MODEL_KEY")})
        self.runtimes = []
        self.loop = asyncio.get_running_loop()
        self.rt = self.runtime()

    def runtime(self):
        rt = Runtime(self.cfg, self.loop)
        self.runtimes.append(rt)
        return rt

    async def asyncTearDown(self):
        for rt in self.runtimes:
            rt.reaper.cancel()
        await asyncio.gather(*(rt.reaper for rt in self.runtimes), return_exceptions=True)
        for rt in self.runtimes:
            rt.hub.close()
            rt.store.db.close()

    def test_missing_model_key_leaves_no_agent_and_restart_succeeds(self):
        with self.assertRaisesRegex(ValueError, r"model gpt: \$PAC_TEST_MODEL_KEY is not set"):
            self.rt.create_agent("assistant")
        self.assertEqual(self.rt.store.agents(), [])
        self.assertEqual(self.rt.sessions, {})
        self.assertEqual(os.listdir(os.path.join(self.temp.name, "agents")), [])
        # A failed first-agent request must not leave a row that prevents the next startup.
        restarted = self.runtime()
        self.assertEqual(restarted.sessions, {})

    def test_same_name_can_be_retried_after_configuring_the_model(self):
        with self.assertRaises(ValueError):
            self.rt.create_agent("assistant")
        os.environ["PAC_TEST_MODEL_KEY"] = "inert-test-key"
        session = self.rt.create_agent("assistant")
        self.assertEqual(self.rt.store.agent(session.id)["name"], "assistant")
        self.assertIs(self.rt.sessions[session.id], session)
        self.thread.assert_called_with(target=self.rt._provision, args=(session,), daemon=True)

    def test_explicit_codex_model_selects_codex_owned_runtime(self):
        os.environ["PAC_TEST_MODEL_KEY"] = "inert-test-key"
        self.cfg.models['gpt'].api = 'codex'
        session = self.rt.create_agent("codex-assistant")
        self.assertEqual(session.adapter.state(), {'engine': 'codex', 'thread_id': None})
        self.assertEqual(session.adapter.home.parent.name, session.id)

    def test_failed_provision_cleanup_can_be_retried_after_incus_error(self):
        os.environ["PAC_TEST_MODEL_KEY"] = "inert-test-key"
        session = self.rt.create_agent("assistant")
        manager = mock.Mock()
        self.rt.manager = manager
        manager.wait_ready.side_effect = ManagerError("desktop failed to initialize")
        with mock.patch.object(self.rt, "ca_pem", return_value="inert-ca"):
            self.rt._provision(session)
        self.assertEqual(self.rt.store.agent(session.id)["status"], "error")
        manager.create.assert_called_once()
        # Incus may fail transiently while removing a partially initialized VM.
        # Keep the agent visible, with its diagnostics, so DELETE can be retried.
        manager.delete.side_effect = [ManagerError("Incus temporarily unavailable"), None]
        events = self.rt.store.events(session.id)
        with mock.patch.object(self.rt, "write_egress_policy") as policy:
            with self.assertRaisesRegex(ManagerError, "temporarily unavailable"):
                self.rt.delete_agent(session.id)
            self.assertIs(self.rt.sessions.get(session.id), session)
            self.assertEqual(self.rt.store.agent(session.id)["status"], "error")
            self.assertEqual(self.rt.store.events(session.id), events)
            policy.assert_not_called()
            self.rt.delete_agent(session.id)
            self.assertNotIn(session.id, self.rt.sessions)
            self.assertIsNone(self.rt.store.agent(session.id))
            self.assertEqual(self.rt.store.events(session.id), [])
            policy.assert_called_once()
        self.assertEqual(manager.delete.call_count, 2)
        # After successful cleanup, the original agent name is usable again.
        replacement = self.rt.create_agent("assistant")
        self.assertNotEqual(replacement.id, session.id)


if __name__ == "__main__":
    unittest.main()
