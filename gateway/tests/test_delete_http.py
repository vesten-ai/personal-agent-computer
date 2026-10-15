"""Real ASGI requests and CLI parsing against fake Incus + temporary persisted state."""
import contextlib
import io
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import urllib.error

from pac_gateway.agent import Runtime
from pac_gateway import cli
from pac_gateway.config import Config
from pac_gateway.manager import ManagerError
from pac_gateway.server import build_app
from pac_gateway.store import Store


class DeletionHTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=self.temp.name, admin_token_env='PAC_FIXTURE_ADMIN')
        self.env = mock.patch.dict(os.environ, {'PAC_FIXTURE_ADMIN': 'inert-admin'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.rt = object.__new__(Runtime)
        self.rt.store = Store(self.temp.name)
        self.rt.manager = mock.Mock()
        self.rt.write_egress_policy = mock.Mock()
        self.row = self.rt.store.create_agent('assistant', 'test')
        self.rt.store.set_status(self.row['id'], 'error')
        self.session = SimpleNamespace(row={**self.row, 'status': 'error'}, sleep_state=None,
            busy=False, taken_over=False, interrupt=mock.Mock(), close_computer=mock.Mock())
        self.rt.sessions = {self.row['id']: self.session}
        self.rt.store.add_event(self.row['id'], 'notice', {'text': 'diagnostic fixture'})
        self.rt.store.save_history(self.row['id'], {'items': ['archive']})
        self.rt.store.save_screen(self.row['id'], b'archived-screen')
        self.app = build_app(self.cfg)
        self.lifespan = self.app.router.lifespan_context(self.app)
        with mock.patch('pac_gateway.server.Runtime', return_value=self.rt):
            await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)
        self.rt.store.db.close()

    async def request(self, method, path, authorized=True):
        sent = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'scheme': 'http', 'method': method, 'path': path, 'raw_path': path.encode(),
            'root_path': '', 'query_string': b'', 'server': ('test', 80), 'client': ('test', 1),
            'headers': [(b'authorization', b'Bearer inert-admin')] if authorized else []}
        await self.app(scope, receive, send)
        status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
        body = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
        return status, body

    async def test_failed_delete_is_json_retained_and_retry_succeeds_without_pruning_archives(self):
        secret = 'private-manager-stderr-fixture'
        self.rt.manager.delete.side_effect = [ManagerError(secret), None]
        path = f"/api/agents/{self.row['id']}"
        status, raw = await self.request('DELETE', path)
        self.assertEqual(status, 503)
        error = json.loads(raw)
        self.assertTrue(error['retryable'])
        self.assertTrue(error['agent_retained'])
        self.assertEqual(error['code'], 'agent_delete_incomplete')
        self.assertNotIn(secret, raw.decode())
        status, raw = await self.request('GET', '/api/agents')
        self.assertEqual(json.loads(raw)['agents'][0]['id'], self.row['id'])
        self.assertIsNotNone(self.rt.store.agent(self.row['id']))
        self.assertEqual(len(self.rt.store.events(self.row['id'])), 1)
        status, raw = await self.request('DELETE', path)
        self.assertEqual((status, json.loads(raw)), (200, {'ok': True}))
        self.assertIsNone(self.rt.store.agent(self.row['id']))
        self.assertNotIn(self.row['id'], self.rt.sessions)
        self.assertEqual(self.rt.store.load_history(self.row['id']), {'items': ['archive']})
        self.assertEqual(Path(self.rt.store.agent_dir(self.row['id']), 'screen.png').read_bytes(), b'archived-screen')
        status, raw = await self.request('DELETE', path)
        self.assertEqual((status, json.loads(raw)), (404, {'error': 'not found'}))

    async def test_post_delete_policy_failure_does_not_claim_record_is_retained(self):
        self.rt.write_egress_policy.side_effect = OSError('private-policy-fixture')
        status, raw = await self.request('DELETE', f"/api/agents/{self.row['id']}")
        self.assertEqual(status, 500)
        data = json.loads(raw)
        self.assertFalse(data['retryable'])
        self.assertFalse(data['agent_retained'])
        self.assertNotIn('private-policy-fixture', raw.decode())

    async def test_unauthorized_delete_does_not_touch_manager(self):
        status, raw = await self.request('DELETE', f"/api/agents/{self.row['id']}", authorized=False)
        self.assertEqual((status, json.loads(raw)), (401, {'error': 'unauthorized'}))
        self.rt.manager.delete.assert_not_called()


class DeletionCLI(unittest.TestCase):
    def test_structured_retry_error_never_prints_deleted_and_retry_prints_success(self):
        agent = {'id': 'test-id', 'name': 'assistant'}
        body = {'error': 'Deletion did not complete. The agent record is retained.',
                'code': 'agent_delete_incomplete', 'retryable': True, 'agent_retained': True}
        failure = urllib.error.HTTPError('http://fixture/api/agents/test-id', 503, 'Unavailable', {},
                                         io.BytesIO(json.dumps(body).encode()))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, 'connection', return_value=('http://fixture', 'inert-token')), \
                mock.patch.object(cli.Gateway, 'find', return_value=agent), \
                mock.patch.object(cli.urllib.request, 'urlopen', side_effect=failure), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['delete', 'assistant']), 1)
        self.assertNotIn('deleted assistant', out.getvalue())
        self.assertIn('record is retained', err.getvalue())
        self.assertIn('Retry: pac delete -- assistant', err.getvalue())
        with mock.patch.object(cli, 'connection', return_value=('http://fixture', 'inert-token')), \
                mock.patch.object(cli.Gateway, 'find', return_value=agent), \
                mock.patch.object(cli.Gateway, 'call', return_value={'ok': True}), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['delete', 'assistant']), 0)
        self.assertIn('deleted assistant', out.getvalue())

    def test_printed_retry_command_reparses_and_deletes_the_exact_name(self):
        for name in ('assistant', 'two words', "agent's \"notes\"", '--help', '--model'):
            with self.subTest(name=name):
                body = {'error': 'Record retained; retry deletion.',
                        'retryable': True, 'agent_retained': True}
                failure = urllib.error.HTTPError('http://fixture/api/agents/test-id', 503,
                    'Unavailable', {}, io.BytesIO(json.dumps(body).encode()))
                output, errors = io.StringIO(), io.StringIO()
                agent = {'id': 'test-id', 'name': name}
                with mock.patch.object(cli, 'connection', return_value=('http://fixture', 'inert-token')), \
                        mock.patch.object(cli.Gateway, 'find', return_value=agent), \
                        mock.patch.object(cli.urllib.request, 'urlopen', side_effect=failure), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(cli.main(['delete', '--', name]), 1)
                retry = next(line.removeprefix('Retry: ') for line in errors.getvalue().splitlines()
                             if line.startswith('Retry: '))
                argv = shlex.split(retry)
                self.assertEqual(argv[0], 'pac')
                output = io.StringIO()
                with mock.patch.object(cli, 'connection', return_value=('http://fixture', 'inert-token')), \
                        mock.patch.object(cli.Gateway, 'find', return_value=agent) as find, \
                        mock.patch.object(cli.Gateway, 'call', return_value={'ok': True}) as call, \
                        contextlib.redirect_stdout(output):
                    # Feed the printed command back through the real argparse parser and command handler.
                    self.assertEqual(cli.main(argv[1:]), 0)
                find.assert_called_once_with(name)
                call.assert_called_once_with('DELETE', '/api/agents/test-id')
                self.assertIn('deleted ' + name, output.getvalue())


if __name__ == '__main__':
    unittest.main()
