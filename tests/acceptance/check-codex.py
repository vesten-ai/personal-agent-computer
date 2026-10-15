#!/usr/bin/env python3
"""Real installed Codex + local canned Responses server; no external model or Incus.

Run with PYTHONPATH=gateway python3 tests/acceptance/check-codex.py.
Requires an installed Codex app-server compatible with 0.147.0.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import tempfile
import threading
import time
from unittest.mock import Mock

from pac_gateway.adapters.codex import CodexComputerAgent

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')


def check():
    requests, stages, children, failures = [], {}, [], []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(data)
                if len(requests) > 12:
                    raise ValueError('unexpected provider retry')
                child = any(x.get('role') == 'user' and 'PAC_CHILD_FIXTURE' in json.dumps(x.get('content'))
                            for x in data['input'])
                key = 'child' if child else 'root'
                stage = stages.get(key, 0)
                stages[key] = stage + 1

                def function(name, args, namespace):
                    return {'type': 'function_call', 'id': f'fc_{len(requests)}',
                            'call_id': f'call_{len(requests)}', 'name': name,
                            'namespace': namespace, 'arguments': json.dumps(args)}

                def message(text):
                    return {'type': 'message', 'id': f'msg_{len(requests)}', 'role': 'assistant',
                            'status': 'completed', 'content': [
                                {'type': 'output_text', 'text': text, 'annotations': []}]}

                outputs = [x.get('output') for x in data['input'] if x.get('type') == 'function_call_output']
                if not child and stage == 0:
                    item = function('spawn_agent', {'message': 'PAC_CHILD_FIXTURE: take a PAC screenshot and report.'},
                                    'multi_agent_v1')
                elif not child and stage == 1:
                    ids = re.findall(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(outputs))
                    if not ids:
                        raise ValueError('Codex did not return a child ID')
                    children.append(ids[-1])
                    item = function('wait_agent', {'targets': [ids[-1]], 'timeout_ms': 10000}, 'multi_agent_v1')
                elif child and stage == 0:
                    assert any(t.get('name') == 'mcp__pac' for t in data['tools']), 'child did not inherit PAC tools'
                    item = function('pac_computer', {'type': 'screenshot'}, 'mcp__pac')
                elif child:
                    # The provider must receive the returned screenshot, not merely a successful RPC.
                    assert 'input_image' in json.dumps(data['input']), 'child did not receive screenshot image'
                    item = message('Child received PAC screenshot.')
                else:
                    assert 'Child received PAC screenshot.' in str(outputs), 'parent did not receive child result'
                    item = message('Main received child result.')
                response = {'id': f'resp_{len(requests)}', 'object': 'response', 'created_at': 0,
                            'model': 'gpt-6.1-sol', 'status': 'completed', 'output': [item],
                            'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20,
                                      'input_tokens_details': {'cached_tokens': 0},
                                      'output_tokens_details': {'reasoning_tokens': 0}}}
                events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                          {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                          {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                          {'type': 'response.completed', 'response': response}]
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for event in events:
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()
            except Exception as error:
                failures.append(str(error))
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='pac-codex-smoke-') as directory:
            events = []
            computer = Mock()
            computer.screenshot.return_value = PNG
            deadline = time.monotonic() + 45
            adapter = CodexComputerAgent(model='gpt-6.1-sol', api_key='inert-test-key',
                system='Use PAC tools for desktop tasks.', home=directory,
                base_url=f'http://127.0.0.1:{server.server_port}/v1',
                on_event=lambda kind, data: events.append((kind, data)),
                # Explicit fixture-only approval, not used by the product adapter.
                call_tool=lambda *_: 'The person answered: Allow',
                should_stop=lambda: bool(failures) or time.monotonic() > deadline)
            result = adapter.run('Delegate a screenshot to a subagent, wait, and report its result.', computer)
            assert not failures, failures
            assert result['stop_reason'] == 'end_turn'
            assert len(children) == 1 and computer.screenshot.call_count == 1
            assert ('text', 'Main received child result.') in events
            saved = adapter.state()
            resumed = CodexComputerAgent(model='gpt-6.1-sol', api_key='inert-test-key',
                system='Use PAC tools for desktop tasks.', home=directory,
                base_url=f'http://127.0.0.1:{server.server_port}/v1',
                should_stop=lambda: bool(failures) or time.monotonic() > deadline)
            resumed.load(saved)
            assert resumed.run('Summarize the result again.', computer)['stop_reason'] == 'end_turn'
            assert resumed.state() == saved
            print('PASS: real Codex root -> child -> inherited PAC MCP -> image -> child result -> root reply -> resumed thread')
            print(f'Local provider requests: {len(requests)}; real model requests: 0; Incus calls: 0')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    check()
