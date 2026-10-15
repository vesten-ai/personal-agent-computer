"""Process-level app-server bridge checks, without credentials, Incus or model requests."""
import os
import sys
import tempfile
import unittest
from unittest import mock

from pac_gateway.adapters.codex import AppServer, CodexComputerAgent, CodexError

FAKE = r'''
import json, sys
thread = 'codex-root'
def send(m):
    print(json.dumps(m), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    method = m.get('method')
    if method == 'initialize':
        assert m['params']['capabilities']['experimentalApi']
        send({'id': m['id'], 'result': {}})
    elif method == 'thread/start':
        assert m['params']['environments'] == []
        assert 'dynamicTools' not in m['params']
        send({'id': m['id'], 'result': {'thread': {'id': thread}}})
    elif method == 'thread/resume':
        assert m['params']['threadId'] == thread
        send({'id': m['id'], 'result': {'thread': {'id': thread}}})
    elif method == 'turn/start':
        send({'id': m['id'], 'result': {'turn': {'id': 'turn-1'}}})
        send({'method': 'item/completed', 'params': {'threadId': 'child-1',
              'item': {'type': 'agentMessage', 'text': 'child result'}}})
        send({'id': 'tool-1', 'method': 'item/tool/call', 'params': {
              'threadId': thread, 'turnId': 'turn-1', 'callId': 'call-1',
              'tool': 'pac_computer', 'arguments': {'type': 'keypress', 'keys': ['CTRL', 'S']}}})
    elif m.get('id') == 'tool-1':
        assert m['result']['success']
        assert m['result']['contentItems'][0]['imageUrl'] == 'data:image/png;base64,cG5n'
        send({'method': 'item/completed', 'params': {'threadId': thread,
              'item': {'type': 'agentMessage', 'text': 'saved'}}})
        send({'method': 'turn/completed', 'params': {'threadId': thread,
              'turn': {'id': 'turn-1', 'status': 'completed'}}})
'''


class CodexBridge(unittest.TestCase):
    def adapter(self, directory, events):
        return CodexComputerAgent(model='test', api_key='inert-test-key', system='test',
                                  home=directory, command=[sys.executable, '-u', '-c', FAKE],
                                  on_event=lambda k, v: events.append((k, v)))

    def test_process_tools_images_events_and_saved_thread_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            computer = mock.Mock()
            computer.screenshot.return_value = b'png'
            agent = self.adapter(directory, events)
            self.assertEqual(agent.run('save', computer)['stop_reason'], 'end_turn')
            computer.call.assert_called_once_with('key', keys=['ctrl', 's'])
            self.assertIn(('text', 'saved'), events)
            self.assertNotIn(('text', 'child result'), events)
            self.assertTrue(any(k == 'codex' and v['thread_id'] == 'child-1' for k, v in events))
            resumed = self.adapter(directory, [])
            resumed.load(agent.state())
            self.assertEqual(resumed.run('save again', computer)['stop_reason'], 'end_turn')
            self.assertEqual(agent.state(), {'engine': 'codex', 'thread_id': 'codex-root'})

    def test_old_model_history_is_not_silently_reinterpreted(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CodexError):
                self.adapter(directory, []).load({'items': []})

    def test_host_requests_and_daemon_run_are_not_auto_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.adapter(directory, [])
            agent.computer = mock.Mock()
            agent.steps, agent.max_steps = 0, 5
            with self.assertRaises(CodexError):
                agent._tool('item/commandExecution/requestApproval', {})
            with self.assertRaises(CodexError):
                agent._tool('item/tool/call', {'tool': 'pac_computer', 'arguments': {'type': 'run'}})
            agent.computer.call.assert_not_called()

    def test_codex_mcp_approval_uses_existing_human_question_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.adapter(directory, [])
            agent.call_tool = mock.Mock(return_value="The person answered: Allow")
            request = {'serverName': 'pac', 'mode': 'form',
                       '_meta': {'codex_approval_kind': 'mcp_tool_call'}}
            self.assertEqual(agent._tool('mcpServer/elicitation/request', request),
                             {'action': 'accept', 'content': {}})
            agent.call_tool.return_value = "The person answered: Don't allow"
            self.assertEqual(agent._tool('mcpServer/elicitation/request', request)['action'], 'decline')

    def test_native_user_question_is_answered_without_rewriting_its_id(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.adapter(directory, [])
            agent.call_tool = mock.Mock(return_value="The person answered: A")
            result = agent._tool('tool/requestUserInput', {'questions': [
                {'id': 'question-1', 'question': 'Choose?', 'options': [{'label': 'A'}]}]})
            self.assertEqual(result, {'answers': {'question-1': {'answers': ['A']}}})

    def test_app_server_exits_without_leaking_a_process(self):
        rpc = AppServer([sys.executable, '-c', 'pass'], dict(os.environ), os.getcwd(),
                        lambda *_: None, lambda *_: None, timeout=1)
        try:
            with self.assertRaises(CodexError):
                rpc.request('initialize', {})
        finally:
            rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)
        self.assertFalse(rpc.reader.is_alive())

    def test_interruption_reaps_app_server(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.adapter(directory, [])
            agent.should_stop = lambda: True
            self.assertEqual(agent.run('stop', mock.Mock())['stop_reason'], 'interrupted')
            self.assertIsNone(agent.computer)


if __name__ == '__main__':
    unittest.main()
