"""Experimental Codex-owned agent loop over app-server stdio (protocol 0.147.0).

PAC answers computer/connector requests; Codex owns model calls, history and delegation.
No Codex installation, login, config from the operator's home, or migration is performed.
"""
import base64
import json
import os
from pathlib import Path
import queue
import socketserver
import sys
import tempfile
import subprocess
import threading
import time

from .openai import _ACTION, execute


class CodexError(RuntimeError):
    pass


class AppServer:
    def __init__(self, command, env, cwd, on_event, on_request, should_stop=lambda: False, timeout=600):
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.messages = queue.Queue()
        self.on_event, self.on_request = on_event, on_request
        self.should_stop, self.timeout = should_stop, timeout
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.messages.put(json.loads(line))
        except Exception:
            pass
        finally:
            self.messages.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + '\n')
        self.proc.stdin.flush()

    def next(self, deadline):
        while True:
            if self.should_stop():
                raise InterruptedError('Codex turn interrupted')
            if time.monotonic() >= deadline:
                raise CodexError('Codex app-server timed out')
            try:
                message = self.messages.get(timeout=min(.2, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if message is None:
                raise CodexError('Codex app-server exited')
            if 'method' in message:
                if 'id' in message:
                    try:
                        result = self.on_request(message['method'], message.get('params') or {})
                        self.send({'id': message['id'], 'result': result})
                    except Exception:
                        # Do not echo arbitrary backend errors, which can include credentials.
                        self.send({'id': message['id'], 'error': {'code': -32603,
                                   'message': 'PAC could not fulfill this request'}})
                else:
                    self.on_event(message['method'], message.get('params') or {})
            return message

    def request(self, method, params):
        self.counter += 1
        rid = self.counter
        self.send({'id': rid, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self.next(deadline)
            if 'method' not in message and message.get('id') == rid:
                if 'error' in message:
                    raise CodexError(f'Codex rejected {method}')
                return message.get('result') or {}

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reader.join(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()


class ToolSocket:
    """Inherited MCP tools, serialized across the root and all of its child threads."""
    def __init__(self, adapter):
        self.directory = tempfile.TemporaryDirectory(prefix='pac-tools-', dir='/tmp')
        os.chmod(self.directory.name, 0o700)
        self.path = self.directory.name + '/tools.sock'

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                try:
                    if request['method'] == 'tools/list':
                        result = {'tools': [{'name': 'pac_computer',
                                  'description': 'Operate the shared PAC desktop and receive a screenshot.',
                                  'inputSchema': _ACTION},
                                  *[{'name': t['name'], 'description': t['description'],
                                     'inputSchema': t['input_schema']} for t in adapter.tools]]}
                    else:
                        params = request['params']
                        answer = adapter._tool('item/tool/call', {'tool': params['name'],
                                               'arguments': params.get('arguments') or {}})
                        content = []
                        for item in answer['contentItems']:
                            if item['type'] == 'inputImage':
                                content.append({'type': 'image', 'mimeType': 'image/png',
                                                'data': item['imageUrl'].split(',', 1)[1]})
                            else:
                                content.append({'type': 'text', 'text': item['text']})
                        result = {'content': content, 'isError': not answer['success']}
                except Exception:
                    result = {'content': [{'type': 'text', 'text': 'PAC tool failed'}], 'isError': True}
                try:
                    self.wfile.write((json.dumps(result) + '\n').encode())
                except OSError:
                    pass  # the app-server may already have closed this child request

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            block_on_close = False

            def process_request_thread(self, request, client_address):
                with self.active_lock:
                    self.active.add(threading.current_thread())
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with self.active_lock:
                        self.active.discard(threading.current_thread())

        self.server = Server(self.path, Handler)
        self.server.active, self.server.active_lock = set(), threading.Lock()
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.05), daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        # Human questions cooperate with cancellation. Never wait indefinitely for a tool.
        deadline = time.monotonic() + 1.5
        with self.server.active_lock:
            handlers = list(self.server.active)
        for handler in handlers:
            handler.join(timeout=max(0, deadline - time.monotonic()))
        self.directory.cleanup()


class CodexComputerAgent:
    def __init__(self, *, model, api_key, system, home, base_url=None, effort='medium',
                 tools=(), call_tool=None, on_event=lambda *_: None, should_stop=lambda: False,
                 command=None, ask_person=None, **_):
        self.model, self.api_key, self.system = model, api_key, system
        self.home = Path(home).resolve()
        self.base_url = (base_url or 'https://api.openai.com/v1').rstrip('/')
        self.effort, self.tools, self.call_tool = effort, list(tools), call_tool
        self.emit, self.should_stop = on_event, should_stop
        self.command = command or ['codex', 'app-server', '--listen', 'stdio://']
        self.thread_id = None
        self.computer = None
        self.tool_lock = threading.RLock()
        self.cancelled = threading.Event()
        self.ask_person = ask_person

    def state(self):
        return {'engine': 'codex', 'thread_id': self.thread_id}

    def load(self, state):
        if state.get('engine') != 'codex':
            raise CodexError('Existing history belongs to another engine; create a new agent')
        self.thread_id = state.get('thread_id')

    def _event(self, method, params):
        # Preserve thread identity for child-agent activity; do not flatten it into the root reply.
        if method == 'item/agentMessage/delta' and params.get('threadId') == self.thread_id:
            self.emit('text_delta', {'text': params.get('delta', '')})
        elif method == 'item/completed' and params.get('threadId') == self.thread_id:
            item = params.get('item') or {}
            if item.get('type') == 'agentMessage':
                self.emit('text', item.get('text', ''))
        item = params.get('item') or {}
        if item.get('type') == 'collabAgentToolCall' and method in ('item/started', 'item/completed'):
            self.emit('notice', {'text': f"Codex subagent: {item.get('tool', 'task')} ({item.get('status', 'running')})"})
        # Only forward safe event metadata, not arbitrary provider payloads/configs.
        self.emit('codex', {'method': method, 'thread_id': params.get('threadId'),
                            'turn_id': params.get('turnId'),
                            'item_type': item.get('type'),
                            'receiver_thread_ids': item.get('receiverThreadIds', [])})

    def _tool(self, method, params):
        # A root and its children share one desktop and one human-question slot.
        cancelled = self.cancelled
        with self.tool_lock:
            return self._dispatch(method, params, cancelled)

    def _human(self, args, cancelled):
        if self.ask_person is not None:
            return self.ask_person(args["question"], args.get("options") or [], cancel_event=cancelled)
        return self.call_tool("ask_person", args)

    def _dispatch(self, method, params, cancelled):
        if cancelled.is_set() or self.should_stop():
            raise InterruptedError('Codex turn interrupted')
        if method == 'tool/requestUserInput' and self.call_tool is not None:
            answers = {}
            for question in params.get('questions') or []:
                if question.get('isSecret'):
                    raise CodexError('Secret input is not supported in chat')
                result = self._human({'question': question['question'],
                    'options': [o['label'] for o in question.get('options') or []]}, cancelled)
                answer = str(result).removeprefix('The person answered: ')
                answers[question['id']] = {'answers': [answer]}
            return {'answers': answers}
        if (method == 'mcpServer/elicitation/request' and params.get('serverName') == 'pac'
                and params.get('mode') == 'form'
                and (params.get('_meta') or {}).get('codex_approval_kind') == 'mcp_tool_call'):
            if self.call_tool is None and self.ask_person is None:
                return {'action': 'decline', 'content': None}
            answer = self._human({
                'question': 'Allow Codex to use the PAC computer/connector tool?',
                'options': ['Allow', "Don't allow"]}, cancelled)
            allowed = str(answer).endswith(': Allow')
            return {'action': 'accept' if allowed else 'decline', 'content': {} if allowed else None}
        if method != 'item/tool/call':
            raise CodexError('Unsupported Codex approval/request; no automatic approval')
        name, args = params['tool'], params.get('arguments') or {}
        if name == 'pac_computer':
            op = args.get('type')
            if self.steps >= self.max_steps:
                raise CodexError('PAC computer action limit reached')
            if op not in {'click', 'double_click', 'drag', 'move', 'scroll', 'keypress', 'type', 'wait', 'screenshot'}:
                raise CodexError('Unknown computer action')
            self.steps += 1
            execute(self.computer, args)
            self.emit('actions', [f'Codex computer action: {op}'])
            screenshot = self.computer.screenshot()
            self.emit('screenshot', screenshot)
            image = base64.b64encode(screenshot).decode()
            return {'success': True, 'contentItems': [
                {'type': 'inputImage', 'imageUrl': 'data:image/png;base64,' + image}]}
        if name not in {t['name'] for t in self.tools}:
            raise CodexError('Unknown PAC tool')
        result = self._human(args, cancelled) if name == 'ask_person' else self.call_tool(name, args)
        return {'success': True, 'contentItems': [{'type': 'inputText', 'text': str(result)}]}

    def run(self, text, computer, max_steps=60):
        self.cancelled = threading.Event()
        self.computer = computer
        self.steps, self.max_steps = 0, max_steps
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.home / 'codex').mkdir(exist_ok=True, mode=0o700)
        workspace = self.home / 'workspace'
        workspace.mkdir(exist_ok=True)
        # Dedicated home and minimal environment: never load the operator's Codex account/config.
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TMPDIR', 'LANG') if k in os.environ}
        env.update(HOME=str(self.home), CODEX_HOME=str(self.home / 'codex'), PAC_CODEX_MODEL_KEY=self.api_key)
        socket = ToolSocket(self)
        config = {'mcp_servers.pac.command': sys.executable,
                  'mcp_servers.pac.args': [str(Path(__file__).resolve().parents[1] / 'codex_tools.py')],
                  'mcp_servers.pac.env.PAC_CODEX_TOOL_SOCKET': socket.path,
                  'mcp_servers.pac.required': True,
                  'model_provider': 'pac', 'model_providers.pac.name': 'PAC',
                  'model_providers.pac.base_url': self.base_url,
                  'model_providers.pac.env_key': 'PAC_CODEX_MODEL_KEY',
                  'model_providers.pac.wire_api': 'responses',
                  'model_providers.pac.requires_openai_auth': False,
                  'model_providers.pac.supports_websockets': False}
        command = list(self.command)
        for key, value in config.items():
            command += ['-c', f'{key}={json.dumps(value)}']
        rpc = None
        try:
            rpc = AppServer(command, env, str(workspace), self._event, self._tool, self.should_stop)
            rpc.request('initialize', {'clientInfo': {'name': 'pac', 'version': '0.1.0'},
                                      'capabilities': {'experimentalApi': True}})
            rpc.send({'method': 'initialized', 'params': {}})
            params = {'model': self.model, 'cwd': str(workspace), 'sandbox': 'read-only',
                      'approvalPolicy': 'untrusted'}
            if self.thread_id:
                params.update(threadId=self.thread_id, excludeTurns=True)
                rpc.request('thread/resume', params)
            else:
                params.update(environments=[], developerInstructions=self.system)
                result = rpc.request('thread/start', params)
                self.thread_id = result['thread']['id']
                self.emit('actions', ['Codex session created'])  # persist ID before starting inference
            result = rpc.request('turn/start', {'threadId': self.thread_id, 'effort': self.effort,
                                               'environments': [],
                                               'input': [{'type': 'text', 'text': text}]})
            turn_id = result['turn']['id']
            deadline = time.monotonic() + rpc.timeout
            while True:
                event = rpc.next(deadline)
                p = event.get('params') or {}
                if event.get('method') == 'turn/completed' and p.get('threadId') == self.thread_id:
                    turn = p.get('turn') or {}
                    if turn.get('id') == turn_id:
                        if turn.get('status') == 'failed':
                            raise CodexError('Codex turn failed')
                        return {'stop_reason': 'interrupted' if turn.get('status') == 'interrupted' else 'end_turn'}
        except InterruptedError:
            return {'stop_reason': 'interrupted'}
        finally:
            # Cancel before shutting down processes/sockets so child questions release themselves.
            self.cancelled.set()
            if rpc is not None:
                rpc.close()
            socket.close()
            self.computer = None
