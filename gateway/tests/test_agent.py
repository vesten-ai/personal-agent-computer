import asyncio
import os
import tempfile
import unittest
from unittest import mock

from pac_gateway.agent import Runtime
from pac_gateway.config import Config, ModelConfig


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


if __name__ == "__main__":
    unittest.main()
