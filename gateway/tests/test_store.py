import tempfile
import unittest

from pac_gateway.store import Store


class StoreTest(unittest.TestCase):
    def test_agents_events_history(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d)
            a = s.create_agent("alice", "gpt")
            self.assertEqual(s.agent(a["id"])["vm"], f"pac-{a['id']}")
            s.add_event(a["id"], "user", {"text": "你好"})
            e2 = s.add_event(a["id"], "text", "hi")
            self.assertEqual([e["kind"] for e in s.events(a["id"])], ["user", "text"])
            self.assertEqual(s.events(a["id"], after=e2["id"]), [])
            s.save_history(a["id"], {"items": [{"x": 1}]})
            self.assertEqual(s.load_history(a["id"]), {"items": [{"x": 1}]})
            s.delete_agent(a["id"])
            self.assertIsNone(s.agent(a["id"]))


if __name__ == "__main__":
    unittest.main()
