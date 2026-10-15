"""Process-level app-server bridge checks, without credentials, Incus or model requests."""
import asyncio
import json
import socket
import threading
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from pac_gateway.adapters.codex import AppServer, CodexComputerAgent, CodexError, ToolSocket
from pac_gateway.agent import Runtime
from pac_gateway.config import Config, ModelConfig

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
        assert m['params']['environments'] == []
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


PENDING_CHILD = r'''
import json, os, socket, sys, threading, time
marker = sys.argv[1]
setting = next(a for a in sys.argv if a.startswith('mcp_servers.pac.env.PAC_CODEX_TOOL_SOCKET='))
path = json.loads(setting.split('=', 1)[1])
def send(message):
    print(json.dumps(message), flush=True)
def question():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        with client.makefile('rw') as stream:
            stream.write(json.dumps({'method': 'tools/call', 'params': {
                'name': 'ask_person', 'arguments': {'question': 'Unfinished child question?', 'options': ['Yes']}}}) + '\n')
            stream.flush()
            stream.readline()
            open(marker + '.answered', 'w').close()
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method in ('initialize', 'thread/start', 'thread/resume'):
        send({'id': message['id'], 'result': {'thread': {'id': 'codex-root'}}})
    elif method == 'turn/start':
        assert message['params']['environments'] == []
        send({'id': message['id'], 'result': {'turn': {'id': 'turn-1'}}})
        threading.Thread(target=question, daemon=True).start()
        deadline = time.monotonic() + 2
        while not os.path.exists(marker) and time.monotonic() < deadline:
            time.sleep(.01)
        assert os.path.exists(marker), 'test child never reached the human question'
        if sys.argv[2] != 'completed':
            while not os.path.exists(marker + '.answered') and time.monotonic() < deadline:
                time.sleep(.01)
        send({'method': 'turn/completed', 'params': {'threadId': 'codex-root',
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

    def test_root_completion_cancels_pending_child_question_and_next_turn_recovers(self):
        async def scenario(directory, mode):
            marker = Path(directory) / 'question-open'
            cfg = Config(data_dir=directory, default_model='codex', models={
                'codex': ModelConfig(name='codex', api='codex', model='test', key_env='PAC_TEST_KEY')})
            with mock.patch.dict(os.environ, {'PAC_TEST_KEY': 'inert-test-key'}), \
                    mock.patch.object(Runtime, '_startup_sync'), mock.patch.object(Runtime, '_provision'):
                runtime = Runtime(cfg, asyncio.get_running_loop())
                session = runtime.create_agent('pending-child')
            computer = mock.Mock()
            computer.screenshot.return_value = b'png'
            session._ensure_computer = mock.Mock(return_value=computer)
            session.adapter.command = [sys.executable, '-u', '-c', PENDING_CHILD, str(marker), mode]
            emit = session.emit

            def capture(kind, data):
                event = emit(kind, data)
                if kind == 'approval':
                    marker.touch()
                return event
            session.emit = capture
            try:
                await session.send('Start child question, then finish the root turn')
                if mode != 'completed':
                    for _ in range(100):
                        if session.pending is not None:
                            break
                        await asyncio.sleep(.01)
                    self.assertIsNotNone(session.pending)
                    if mode == 'answered':
                        session.answer(session.pending['id'], 'Yes')
                    else:
                        session.interrupt()
                await asyncio.wait_for(asyncio.shield(session.worker), timeout=3)
                self.assertTrue(marker.exists())
                self.assertIsNone(session.pending)
                self.assertFalse(session.busy)
                self.assertEqual(session.stop_requested, mode == 'interrupted')
                self.assertEqual(session.row['status'], 'ready')
                events = runtime.store.events(session.id)
                self.assertTrue(any(e['kind'] == 'approval_answered' and
                                    ('Yes' if mode == 'answered' else 'Cancelled') in e['data']['answer']
                                    for e in events))
                # Internal cancellation must not poison the resumed agent's next normal turn.
                session.adapter.command = [sys.executable, '-u', '-c', FAKE]
                await session.send('Save after cancellation')
                await asyncio.wait_for(asyncio.shield(session.worker), timeout=3)
                self.assertEqual(session.row['status'], 'ready')
                computer.call.assert_called_once_with('key', keys=['ctrl', 's'])
            finally:
                session.interrupt()  # release old-code repro on assertion timeout, not the success path
                if session.worker and not session.worker.done():
                    await asyncio.wait_for(asyncio.shield(session.worker), timeout=3)
                runtime.reaper.cancel()
                await asyncio.gather(runtime.reaper, return_exceptions=True)
                runtime.hub.close()
                runtime.store.db.close()

        for mode in ('completed', 'answered', 'interrupted'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                asyncio.run(scenario(directory, mode))

    def test_old_turn_socket_does_not_adopt_next_turn_cancellation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(directory, [])
            adapter.computer = mock.Mock()
            adapter.steps, adapter.max_steps = 0, 5
            tools = ToolSocket(adapter)
            try:
                adapter.cancelled.set()
                adapter.cancelled = threading.Event()  # simulate a subsequent live turn
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(tools.path)
                    with client.makefile('rw') as stream:
                        stream.write(json.dumps({'method': 'tools/call', 'params': {
                            'name': 'pac_computer', 'arguments': {'type': 'screenshot'}}}) + '\n')
                        stream.flush()
                        self.assertTrue(json.loads(stream.readline())['isError'])
                adapter.computer.screenshot.assert_not_called()
            finally:
                tools.close()

    def test_interruption_reaps_app_server(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.adapter(directory, [])
            agent.should_stop = lambda: True
            self.assertEqual(agent.run('stop', mock.Mock())['stop_reason'], 'interrupted')
            self.assertIsNone(agent.computer)


if __name__ == '__main__':
    unittest.main()
