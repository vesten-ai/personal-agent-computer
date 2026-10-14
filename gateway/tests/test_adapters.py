"""Offline checks for the two computer-use adapters: canned API responses in, the same
pac-computerd operations out, and replies shaped the way each API requires."""

import base64
import unittest
from unittest import mock

from pac_gateway.adapters import anthropic as ant
from pac_gateway.adapters import openai as oai

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class FakeComputer:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def call(self, op, **a):
        if op == self.fail_on:
            raise RuntimeError("boom")
        self.calls.append((op, a))
        return {"x": 3, "y": 4} if op == "cursor_position" else {}

    def screenshot(self):
        self.calls.append(("screenshot", {}))
        return PNG

    def zoom(self, region):
        self.calls.append(("zoom", {"region": region}))
        return PNG


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "toolset_name": "computer", "input": inp}


class AnthropicAdapter(unittest.TestCase):
    def run_turns(self, turns, computer):
        agent = ant.AnthropicComputerAgent(api_key="x", log=lambda *_: None)
        with mock.patch.object(ant, "post_json", side_effect=turns) as post:
            out = agent.run("task", computer)
        return agent, out, post

    def test_batch_maps_to_computerd_and_replies_with_toolset_name(self):
        c = FakeComputer()
        turns = [
            {"stop_reason": "tool_use", "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                tool_use(1, "left_click", {"coordinate": [10, 20], "text": "ctrl+shift"}),
                tool_use(2, "type", {"text": "你好 hello"}),
                tool_use(3, "key", {"text": "ctrl+s", "repeat": 2}),
                tool_use(4, "scroll", {"coordinate": [5, 6], "scroll_direction": "up", "scroll_amount": 3}),
                tool_use(5, "left_click_drag", {"start_coordinate": [1, 2], "coordinate": [3, 4]}),
                tool_use(6, "triple_click", {"coordinate": [7, 8]}),
                tool_use(7, "cursor_position", {}),
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
        ]
        agent, out, post = self.run_turns(turns, c)
        self.assertEqual(out["final"], "done")
        ops = c.calls
        self.assertEqual(ops[0], ("click", {"x": 10, "y": 20, "button": "left", "count": 1, "modifiers": ["ctrl", "shift"]}))
        self.assertEqual(ops[1], ("type", {"text": "你好 hello"}))
        self.assertEqual(ops[2], ("key", {"keys": ["ctrl", "s"], "repeat": 2}))
        self.assertEqual(ops[3], ("scroll", {"x": 5, "y": 6, "dy": -3, "dx": 0, "modifiers": []}))
        self.assertEqual(ops[4], ("drag", {"path": [[1, 2], [3, 4]], "modifiers": []}))
        self.assertEqual(ops[5][1]["count"], 3)
        self.assertEqual(ops[-1][0], "screenshot")  # trailing screenshot for a batch without one
        # The thinking block is replayed untouched, and every result echoes toolset_name.
        second_body = post.call_args_list[1].args[2]
        self.assertEqual(second_body["messages"][1]["content"][0], {"type": "thinking", "thinking": "", "signature": "sig"})
        results = second_body["messages"][2]["content"]
        self.assertEqual(len(results), 7)
        self.assertTrue(all(r["toolset_name"] == "computer" for r in results))
        self.assertEqual(results[6]["content"][0]["text"], "X=3, Y=4")
        self.assertEqual(results[6]["content"][-1]["type"], "image")
        self.assertEqual(second_body["tools"], [{"type": "computer_toolset_20260801"}])

    def test_workspace_header_and_refusal(self):
        agent = ant.AnthropicComputerAgent(api_key="x", log=lambda *_: None, workspace_id="wrkspc_1")
        events = []
        agent.on_event = lambda kind, data: events.append((kind, data))
        refusal = {"stop_reason": "refusal", "content": [],
                   "stop_details": {"type": "refusal", "category": "cyber", "explanation": None}}
        with mock.patch.object(ant, "post_json", return_value=refusal) as post:
            out = agent.run("task", FakeComputer())
        self.assertEqual(post.call_args.args[1]["anthropic-workspace-id"], "wrkspc_1")
        self.assertEqual(out["stop_reason"], "refusal")
        self.assertIn(("error", {"text": "The model declined to continue (cyber)."}), events)

    def test_failure_stops_the_batch_and_marks_the_rest(self):
        c = FakeComputer(fail_on="type")
        turns = [
            {"stop_reason": "tool_use", "content": [
                tool_use(1, "mouse_move", {"coordinate": [1, 1]}),
                tool_use(2, "type", {"text": "x"}),
                tool_use(3, "key", {"text": "Return"}),
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]},
        ]
        _, _, post = self.run_turns(turns, c)
        results = post.call_args_list[1].args[2]["messages"][2]["content"]
        self.assertNotIn("is_error", results[0])
        self.assertTrue(results[1]["is_error"])
        self.assertEqual(results[2]["content"], ant.NOT_EXECUTED)
        self.assertEqual([op for op, _ in c.calls], ["move"])

    def test_connector_tool_alongside_computer_actions(self):
        c = FakeComputer()
        calls = []
        agent = ant.AnthropicComputerAgent(api_key="x", log=lambda *_: None,
                                           tools=[{"name": "notes__add", "description": "d",
                                                   "input_schema": {"type": "object"}}],
                                           call_tool=lambda n, a: calls.append((n, a)) or "saved")
        turns = [
            {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "toolu_n", "name": "notes__add", "input": {"text": "hi"}},
                tool_use(2, "type", {"text": "x"}),
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]},
        ]
        with mock.patch.object(ant, "post_json", side_effect=turns) as post:
            agent.run("task", c)
        self.assertEqual(calls, [("notes__add", {"text": "hi"})])
        body = post.call_args_list[0].args[2]
        self.assertEqual(body["tools"][1]["name"], "notes__add")
        results = post.call_args_list[1].args[2]["messages"][2]["content"]
        self.assertEqual(results[0], {"type": "tool_result", "tool_use_id": "toolu_n", "content": "saved"})
        self.assertEqual(results[1]["toolset_name"], "computer")
        self.assertEqual(results[1]["content"][-1]["type"], "image")  # trailing screenshot on the computer result


class OpenAIAdapter(unittest.TestCase):
    def test_batched_actions_and_stateless_replay(self):
        c = FakeComputer()
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"}
        turns = [
            {"status": "completed", "output": [reasoning, {
                "type": "computer_call", "call_id": "call_1", "status": "completed",
                "pending_safety_checks": [],
                "actions": [
                    {"type": "click", "button": "right", "x": 1, "y": 2, "keys": ["CTRL"]},
                    {"type": "double_click", "x": 3, "y": 4},
                    {"type": "keypress", "keys": ["CTRL", "S"]},
                    {"type": "keypress", "keys": ["ENTER"]},
                    {"type": "scroll", "x": 5, "y": 6, "scroll_x": 0, "scroll_y": -250},
                    {"type": "drag", "path": [{"x": 1, "y": 1}, {"x": 2, "y": 2}, {"x": 3, "y": 3}]},
                    {"type": "type", "text": "你好"},
                    {"type": "screenshot"},
                ]}]},
            {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "done"}]}]},
        ]
        agent = oai.OpenAIComputerAgent(api_key="x", log=lambda *_: None)
        with mock.patch.object(oai, "post_json", side_effect=turns) as post:
            out = agent.run("task", c)
        self.assertEqual(out["final"], "done")
        ops = c.calls
        self.assertEqual(ops[0], ("click", {"x": 1, "y": 2, "button": "right", "modifiers": ["ctrl"]}))
        self.assertEqual(ops[1], ("click", {"x": 3, "y": 4, "count": 2, "modifiers": []}))
        self.assertEqual(ops[2], ("key", {"keys": ["ctrl", "s"]}))
        self.assertEqual(ops[3], ("key", {"keys": ["Return"]}))
        self.assertEqual(ops[4], ("scroll", {"x": 5, "y": 6, "dx": 0, "dy": -2, "modifiers": []}))
        self.assertEqual(ops[5], ("drag", {"path": [[1, 1], [2, 2], [3, 3]], "modifiers": []}))
        self.assertEqual(ops[6], ("type", {"text": "你好"}))
        self.assertEqual(ops[7][0], "screenshot")
        body = post.call_args_list[1].args[2]
        self.assertIs(body["store"], False)
        self.assertEqual(body["input"][1], reasoning)  # encrypted reasoning replayed verbatim
        cco = body["input"][3]
        self.assertEqual(cco["type"], "computer_call_output")
        self.assertEqual(cco["call_id"], "call_1")
        self.assertTrue(cco["output"]["image_url"].startswith("data:image/png;base64,"))

    def test_function_tool_mode(self):
        c = FakeComputer()
        turns = [
            {"status": "completed", "output": [{
                "type": "function_call", "name": "computer", "call_id": "fc_1",
                "arguments": '{"actions":[{"type":"click","x":7,"y":8},{"type":"type","text":"hi"}]}'}]},
            {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]},
        ]
        agent = oai.OpenAIComputerAgent(api_key="x", log=lambda *_: None, tool_mode="function")
        with mock.patch.object(oai, "post_json", side_effect=turns) as post:
            agent.run("task", c)
        self.assertEqual(c.calls[0], ("click", {"x": 7, "y": 8, "button": "left", "modifiers": []}))
        body = post.call_args_list[1].args[2]
        self.assertEqual(body["tools"][0]["name"], "computer")
        out = body["input"][2]
        self.assertEqual((out["type"], out["call_id"]), ("function_call_output", "fc_1"))
        self.assertEqual(out["output"][1]["type"], "input_image")

    def test_connector_function_call(self):
        c = FakeComputer()
        turns = [
            {"status": "completed", "output": [{"type": "function_call", "name": "notes__list", "call_id": "fc_2",
                                                "arguments": "{}"}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]},
        ]
        agent = oai.OpenAIComputerAgent(api_key="x", log=lambda *_: None, tool_mode="function",
                                        tools=[{"name": "notes__list", "input_schema": {"type": "object"}}],
                                        call_tool=lambda n, a: "[]")
        with mock.patch.object(oai, "post_json", side_effect=turns) as post:
            agent.run("task", c)
        body = post.call_args_list[1].args[2]
        self.assertEqual([t["name"] for t in body["tools"]], ["computer", "notes__list"])
        self.assertEqual(body["input"][2], {"type": "function_call_output", "call_id": "fc_2", "output": "[]"})
        self.assertEqual(c.calls, [])  # no computer action, no screenshot

    def test_safety_check_goes_to_the_person(self):
        flagged = {"type": "computer_call", "call_id": "c1", "status": "completed",
                   "pending_safety_checks": [{"id": "sc1", "code": "malicious_instructions", "message": "odd page"}],
                   "actions": [{"type": "click", "button": "left", "x": 1, "y": 1}]}
        done = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        for allow in (False, True):
            c, asked = FakeComputer(), []
            agent = oai.OpenAIComputerAgent(api_key="x", log=lambda *_: None,
                                            confirm=lambda q: asked.append(q) or allow)
            with mock.patch.object(oai, "post_json", side_effect=[{"status": "completed", "output": [flagged]}, done]) as post:
                agent.run("task", c)
            self.assertIn("odd page", asked[0])
            second = post.call_args_list[1].args[2]["input"]
            if allow:
                self.assertEqual(c.calls[0][0], "click")
                cco = next(i for i in second if i.get("type") == "computer_call_output")
                self.assertEqual(cco["acknowledged_safety_checks"][0]["id"], "sc1")
            else:
                self.assertEqual(c.calls, [])  # nothing ran
                self.assertNotIn(flagged, second)  # the unanswered call was dropped, never sent back
                users = [i["content"][0]["text"] for i in second if i.get("role") == "user"]
                self.assertIn("did not allow", users[-1])


if __name__ == "__main__":
    unittest.main()
