import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pac_gateway import cli


class FakeGateway:
    """Replays a scripted event stream; records what the CLI posts."""

    def __init__(self, events):
        self.events, self.posted = events, []

    def call(self, method, path, body=None):
        if method == "GET" and "/events?after=" in path:
            after = int(path.rsplit("=", 1)[1])
            return {"events": [e for e in self.events if e["id"] > after][:2]}
        self.posted.append((method, path, body))
        return {"ok": True}


def ev(i, kind, data):
    return {"id": i, "kind": kind, "data": data}


class Follow(unittest.TestCase):
    def test_prints_the_turn_and_answers_an_approval(self):
        gw = FakeGateway([
            ev(1, "user", {"text": "tidy up"}),
            ev(2, "status", {"status": "working"}),
            ev(3, "actions", ["look at the screen"]),
            ev(4, "approval", {"id": "q1", "question": "Delete temp.txt?", "options": ["Yes", "No"]}),
            ev(5, "approval_answered", {"id": "q1", "answer": "Yes"}),
            ev(6, "text", "Deleted."),
            ev(7, "status", {"status": "ready"}),
        ])
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("builtins.input", return_value="1"), \
                mock.patch.object(cli.time, "sleep"):
            code = cli.follow(gw, {"id": "a1"}, 1)
        self.assertEqual(code, 0)
        self.assertEqual(gw.posted, [("POST", "/api/agents/a1/answer", {"id": "q1", "text": "Yes"})])
        self.assertIn("  · look at the screen", out.getvalue())
        self.assertIn("Deleted.", out.getvalue())

    def test_a_queued_message_ends_when_the_running_turn_does(self):
        gw = FakeGateway([ev(9, "text", "done"), ev(10, "status", {"status": "ready"})])
        with redirect_stdout(io.StringIO()), mock.patch.object(cli.time, "sleep"):
            self.assertEqual(cli.follow(gw, {"id": "a1", "busy": True}, 8), 0)


class Connection(unittest.TestCase):
    def test_token_from_the_gateway_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, "gateway.env")
            with open(env, "w") as f:
                f.write("PAC_MODEL_GPT_KEY=sk-x\nPAC_ADMIN_TOKEN=abc123\n")
            with mock.patch.object(cli, "ENV_FILE", env), mock.patch.object(cli, "CONFIG", os.path.join(d, "none")), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(cli.connection(), ("http://127.0.0.1:8000", "abc123"))

    def test_environment_wins(self):
        with mock.patch.dict(os.environ, {"PAC_URL": "http://127.0.0.1:8091/", "PAC_TOKEN": "t"}, clear=True):
            self.assertEqual(cli.connection(), ("http://127.0.0.1:8091", "t"))


if __name__ == "__main__":
    unittest.main()
